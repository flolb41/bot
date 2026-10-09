"""Tests pour les helpers de résolution de pools dans app/trading/executor.py
(liquidité comparée on-chain, PancakeSwap V3 / Aerodrome Slipstream vs leurs
équivalents classiques). Les fakes ci-dessous sont volontairement plus
permissifs que `tests/test_flashloan.py::_FakeW3` (qui ne gère qu'UNE valeur
de retour par contrat) car plusieurs fonctions testées ici font plusieurs
appels distincts (`getPool` par fee tier, `balanceOf` par pool...)."""
from __future__ import annotations

import pytest

from app.trading import executor

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"


class _FakeFunction:
    def __init__(self, value):
        self._value = value

    def call(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


class _FakeFunctions:
    """`functions_map` associe un nom de fonction ABI à soit une valeur
    statique, soit un callable(*args) -> valeur (pour varier la réponse selon
    les arguments, ex: balanceOf(pool) différent par pool)."""

    def __init__(self, functions_map):
        self._functions_map = functions_map

    def __getattr__(self, name):
        if name not in self._functions_map:
            raise AttributeError(f"Aucun fake configuré pour la fonction '{name}'.")
        configured = self._functions_map[name]

        def _call(*args, **kwargs):  # noqa: ARG001
            value = configured(*args) if callable(configured) else configured
            return _FakeFunction(value)

        return _call


class _FakeContract:
    def __init__(self, functions_map):
        self.functions = _FakeFunctions(functions_map)


class _FakeEth:
    def __init__(self, contracts_by_address, raw_calls_by_address=None):
        self._contracts = {addr.lower(): contract for addr, contract in contracts_by_address.items()}
        # Appels bruts `eth_call` (slot0/liquidity des pools V3, voir
        # `executor._v3_style_quote_depth`) : {pool: {selector: bytes | Exception}}.
        self._raw_calls = {addr.lower(): calls for addr, calls in (raw_calls_by_address or {}).items()}

    def contract(self, address, abi):  # noqa: ARG002 - signature imposée par web3
        return self._contracts[address.lower()]

    def call(self, tx):
        value = self._raw_calls[tx["to"].lower()][tx["data"]]
        if isinstance(value, Exception):
            raise value
        return value


class _FakeW3:
    def __init__(self, contracts_by_address, raw_calls_by_address=None):
        self.eth = _FakeEth(contracts_by_address, raw_calls_by_address)


POOL_A = "0x" + "a" * 40
POOL_B = "0x" + "b" * 40
ZERO_POOL = "0x" + "0" * 40


def _v3_pool_state(liquidity, sqrt_price_x96=2 ** 96):
    """Réponses brutes slot0()/liquidity() d'une pool V3 : avec √P = 2^96
    (prix 1:1), la profondeur virtuelle vaut exactement `liquidity`, quel que
    soit le côté (token0/token1) du quote."""
    return {
        executor._SLOT0_SELECTOR: sqrt_price_x96.to_bytes(32, "big") + b"\x00" * 32 * 6,
        executor._LIQUIDITY_SELECTOR: liquidity.to_bytes(32, "big"),
    }


def test_quote_reserve_in_pool_reads_balance():
    w3 = _FakeW3({WETH: _FakeContract({"balanceOf": 12345})})
    assert executor._quote_reserve_in_pool(w3, POOL_A, WETH) == 12345


def test_quote_reserve_in_pool_raises_on_rpc_error():
    # Une erreur RPC ne doit JAMAIS être convertie en "réserve nulle / pool
    # absente" : sous rate-limit (429), cela faisait gagner une pool quasi vide
    # (flashloan BRETT/WETH simulé à -62 % le 09/10/2026). L'erreur remonte, la
    # tentative est refusée et retentée au cycle suivant.
    w3 = _FakeW3({WETH: _FakeContract({"balanceOf": RuntimeError("rpc down")})})
    with pytest.raises(RuntimeError):
        executor._quote_reserve_in_pool(w3, POOL_A, WETH)


def test_best_v3_style_pool_propagates_rpc_error_instead_of_picking_wrong_pool():
    # Scénario du bug : la grosse pool (fee 500) échoue en lecture, la petite
    # (fee 10000) répond → l'ancienne version choisissait la petite.
    factory_addr = "0x" + "2" * 40

    def get_pool(_token, _quote, fee):
        return {500: POOL_A, 10000: POOL_B}.get(fee, ZERO_POOL)

    w3 = _FakeW3(
        {factory_addr: _FakeContract({"getPool": get_pool})},
        raw_calls_by_address={
            POOL_A: {executor._SLOT0_SELECTOR: RuntimeError("rpc down"),
                     executor._LIQUIDITY_SELECTOR: RuntimeError("rpc down")},
            POOL_B: _v3_pool_state(liquidity=10),
        },
    )
    with pytest.raises(RuntimeError):
        executor._best_v3_style_pool(w3, factory_addr, [], (500, 3000, 10000), USDC, WETH)
    # Et rien n'est mis en cache pour cette paire (hors adresses de pool, immuables).
    assert all(key[0] != "v3_style_pool" for key in executor._route_cache)


def test_v3_style_quote_depth_uses_active_liquidity_not_token_balance():
    # Bug BRETT/WETH PancakeSwap V3 (09/10/2026) : gros solde WETH dans le
    # contrat mais liquidité active nulle → profondeur 0, pool écartée.
    w3 = _FakeW3({}, raw_calls_by_address={POOL_A: _v3_pool_state(liquidity=0)})
    assert executor._v3_style_quote_depth(w3, POOL_A, USDC, WETH) == 0
    # √P = 2·2^96 (prix token1/token0 = 4) : WETH est token0 (adresse plus
    # petite) → réserve virtuelle WETH = L/√P = L/2 ; USDC (token1) = L·√P = 2L.
    w3 = _FakeW3({}, raw_calls_by_address={POOL_A: _v3_pool_state(liquidity=1000, sqrt_price_x96=2 * 2 ** 96)})
    assert executor._v3_style_quote_depth(w3, POOL_A, USDC, WETH) == 500
    assert executor._v3_style_quote_depth(w3, POOL_A, WETH, USDC) == 2000


def test_best_v3_style_pool_picks_highest_reserve():
    factory_addr = "0x" + "2" * 40

    def get_pool(_token, _quote, fee):
        return {500: POOL_A, 3000: ZERO_POOL, 10000: POOL_B}.get(fee, ZERO_POOL)

    w3 = _FakeW3(
        {factory_addr: _FakeContract({"getPool": get_pool})},
        raw_calls_by_address={POOL_A: _v3_pool_state(liquidity=500), POOL_B: _v3_pool_state(liquidity=2000)},
    )
    result = executor._best_v3_style_pool(w3, factory_addr, [], (500, 3000, 10000), USDC, WETH)
    assert result == (10000, POOL_B)


def test_best_v3_style_pool_returns_none_when_no_pool_exists():
    factory_addr = "0x" + "2" * 40
    w3 = _FakeW3({factory_addr: _FakeContract({"getPool": lambda *_: ZERO_POOL})})
    assert executor._best_v3_style_pool(w3, factory_addr, [], (500, 3000, 10000), USDC, WETH) is None


def test_resolve_pancakeswap_route_prefers_v3_when_more_liquid(monkeypatch):
    monkeypatch.setattr(
        executor, "_best_pancakeswap_v3_pool", lambda w3, token, quote: (500, POOL_B)
    )
    factory_addr = "0x" + "3" * 40
    router_addr = executor.ROUTER_ADDRESSES["pancakeswap"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"factory": factory_addr}),
        factory_addr: _FakeContract({"getPair": lambda *_: POOL_A}),
        USDC: _FakeContract({"balanceOf": lambda pool_addr: 100 if pool_addr.lower() == POOL_A.lower() else 0}),
    }, raw_calls_by_address={POOL_B: _v3_pool_state(liquidity=999)})
    route = executor._resolve_pancakeswap_route(w3, USDC, WETH)
    assert route == {"is_v3": True, "router": executor.PANCAKESWAP_V3_ROUTER_ADDRESS, "fee": 500, "pool": POOL_B}


def test_resolve_pancakeswap_route_prefers_classic_when_more_liquid(monkeypatch):
    monkeypatch.setattr(
        executor, "_best_pancakeswap_v3_pool", lambda w3, token, quote: (500, POOL_B)
    )
    factory_addr = "0x" + "3" * 40
    router_addr = executor.ROUTER_ADDRESSES["pancakeswap"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"factory": factory_addr}),
        factory_addr: _FakeContract({"getPair": lambda *_: POOL_A}),
        USDC: _FakeContract({"balanceOf": lambda pool_addr: 999 if pool_addr.lower() == POOL_A.lower() else 0}),
    }, raw_calls_by_address={POOL_B: _v3_pool_state(liquidity=100)})
    route = executor._resolve_pancakeswap_route(w3, USDC, WETH)
    assert route == {"is_v3": False, "router": router_addr, "pool": POOL_A}


def test_resolve_pancakeswap_route_none_when_no_pool_anywhere(monkeypatch):
    monkeypatch.setattr(executor, "_best_pancakeswap_v3_pool", lambda w3, token, quote: None)
    factory_addr = "0x" + "3" * 40
    router_addr = executor.ROUTER_ADDRESSES["pancakeswap"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"factory": factory_addr}),
        factory_addr: _FakeContract({"getPair": lambda *_: ZERO_POOL}),
    })
    assert executor._resolve_pancakeswap_route(w3, USDC, WETH) is None


def test_resolve_aerodrome_route_prefers_slipstream_when_more_liquid(monkeypatch):
    slipstream_router = "0x" + "4" * 40
    monkeypatch.setattr(
        executor, "_best_aerodrome_slipstream_pool",
        lambda w3, token, quote: (slipstream_router, 100, POOL_B),
    )
    default_factory = "0x" + "5" * 40
    router_addr = executor.ROUTER_ADDRESSES["aerodrome"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"defaultFactory": default_factory, "poolFor": lambda *_: POOL_A}),
        USDC: _FakeContract({"balanceOf": lambda pool_addr: 100 if pool_addr.lower() == POOL_A.lower() else 0}),
    }, raw_calls_by_address={POOL_B: _v3_pool_state(liquidity=999)})
    route = executor._resolve_aerodrome_route(w3, USDC, WETH)
    assert route == {"is_slipstream": True, "router": slipstream_router, "tick_spacing": 100, "pool": POOL_B}


def test_resolve_aerodrome_route_prefers_classic_when_more_liquid(monkeypatch):
    slipstream_router = "0x" + "4" * 40
    monkeypatch.setattr(
        executor, "_best_aerodrome_slipstream_pool",
        lambda w3, token, quote: (slipstream_router, 100, POOL_B),
    )
    default_factory = "0x" + "5" * 40
    router_addr = executor.ROUTER_ADDRESSES["aerodrome"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"defaultFactory": default_factory, "poolFor": lambda *_: POOL_A}),
        USDC: _FakeContract({"balanceOf": lambda pool_addr: 999 if pool_addr.lower() == POOL_A.lower() else 0}),
    }, raw_calls_by_address={POOL_B: _v3_pool_state(liquidity=100)})
    route = executor._resolve_aerodrome_route(w3, USDC, WETH)
    assert route == {"is_slipstream": False, "router": router_addr, "default_factory": default_factory, "pool": POOL_A}


def test_v2_fork_pool_confirmed_absent_when_no_pair(monkeypatch):
    # Bug reproduit en prod le 2026-10-08 : AlienBase n'a AUCUNE pool directe
    # WETH/EURC (`getPair` renvoie l'adresse zéro) alors que DexScreener
    # indiquait ce DEX comme rentable — ce garde-fou doit le détecter.
    factory_addr = "0x" + "6" * 40
    router_addr = executor.ROUTER_ADDRESSES["alien-base"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"factory": factory_addr}),
        factory_addr: _FakeContract({"getPair": lambda *_: ZERO_POOL}),
    })
    assert executor._v2_fork_pool_confirmed_absent(w3, "alien-base", USDC, WETH) is True


def test_v2_fork_pool_confirmed_absent_false_when_pair_exists():
    factory_addr = "0x" + "7" * 40
    router_addr = executor.ROUTER_ADDRESSES["alien-base"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"factory": factory_addr}),
        factory_addr: _FakeContract({"getPair": lambda *_: POOL_A}),
    })
    assert executor._v2_fork_pool_confirmed_absent(w3, "alien-base", USDC, WETH) is False


def test_v2_fork_pool_confirmed_absent_false_on_rpc_error():
    # Panne RPC : on ne peut pas CONFIRMER l'absence, donc comportement
    # permissif (pas de blocage) — voir docstring de la fonction.
    router_addr = executor.ROUTER_ADDRESSES["alien-base"]
    w3 = _FakeW3({router_addr: _FakeContract({"factory": RuntimeError("rpc down")})})
    assert executor._v2_fork_pool_confirmed_absent(w3, "alien-base", USDC, WETH) is False
