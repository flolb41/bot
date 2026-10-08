"""Tests pour les helpers de résolution de pools dans app/trading/executor.py
(liquidité comparée on-chain, PancakeSwap V3 / Aerodrome Slipstream vs leurs
équivalents classiques). Les fakes ci-dessous sont volontairement plus
permissifs que `tests/test_flashloan.py::_FakeW3` (qui ne gère qu'UNE valeur
de retour par contrat) car plusieurs fonctions testées ici font plusieurs
appels distincts (`getPool` par fee tier, `balanceOf` par pool...)."""
from __future__ import annotations

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
    def __init__(self, contracts_by_address):
        self._contracts = {addr.lower(): contract for addr, contract in contracts_by_address.items()}

    def contract(self, address, abi):  # noqa: ARG002 - signature imposée par web3
        return self._contracts[address.lower()]


class _FakeW3:
    def __init__(self, contracts_by_address):
        self.eth = _FakeEth(contracts_by_address)


POOL_A = "0x" + "a" * 40
POOL_B = "0x" + "b" * 40
ZERO_POOL = "0x" + "0" * 40


def test_quote_reserve_in_pool_reads_balance():
    w3 = _FakeW3({WETH: _FakeContract({"balanceOf": 12345})})
    assert executor._quote_reserve_in_pool(w3, POOL_A, WETH) == 12345


def test_quote_reserve_in_pool_returns_none_on_rpc_error():
    w3 = _FakeW3({WETH: _FakeContract({"balanceOf": RuntimeError("rpc down")})})
    assert executor._quote_reserve_in_pool(w3, POOL_A, WETH) is None


def test_best_v3_style_pool_picks_highest_reserve():
    factory_addr = "0x" + "2" * 40

    def get_pool(_token, _quote, fee):
        return {500: POOL_A, 3000: ZERO_POOL, 10000: POOL_B}.get(fee, ZERO_POOL)

    def balance_of(pool_addr):
        return {POOL_A.lower(): 500, POOL_B.lower(): 2000}.get(pool_addr.lower(), 0)

    w3 = _FakeW3({
        factory_addr: _FakeContract({"getPool": get_pool}),
        WETH: _FakeContract({"balanceOf": balance_of}),
    })
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

    def balance_of(pool_addr):
        return {POOL_A.lower(): 100, POOL_B.lower(): 999}.get(pool_addr.lower(), 0)

    factory_addr = "0x" + "3" * 40
    router_addr = executor.ROUTER_ADDRESSES["pancakeswap"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"factory": factory_addr}),
        factory_addr: _FakeContract({"getPair": lambda *_: POOL_A}),
        USDC: _FakeContract({"balanceOf": balance_of}),
    })
    route = executor._resolve_pancakeswap_route(w3, USDC, WETH)
    assert route == {"is_v3": True, "router": executor.PANCAKESWAP_V3_ROUTER_ADDRESS, "fee": 500, "pool": POOL_B}


def test_resolve_pancakeswap_route_prefers_classic_when_more_liquid(monkeypatch):
    monkeypatch.setattr(
        executor, "_best_pancakeswap_v3_pool", lambda w3, token, quote: (500, POOL_B)
    )

    def balance_of(pool_addr):
        return {POOL_A.lower(): 999, POOL_B.lower(): 100}.get(pool_addr.lower(), 0)

    factory_addr = "0x" + "3" * 40
    router_addr = executor.ROUTER_ADDRESSES["pancakeswap"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"factory": factory_addr}),
        factory_addr: _FakeContract({"getPair": lambda *_: POOL_A}),
        USDC: _FakeContract({"balanceOf": balance_of}),
    })
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

    def balance_of(pool_addr):
        return {POOL_A.lower(): 100, POOL_B.lower(): 999}.get(pool_addr.lower(), 0)

    default_factory = "0x" + "5" * 40
    router_addr = executor.ROUTER_ADDRESSES["aerodrome"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"defaultFactory": default_factory, "poolFor": lambda *_: POOL_A}),
        USDC: _FakeContract({"balanceOf": balance_of}),
    })
    route = executor._resolve_aerodrome_route(w3, USDC, WETH)
    assert route == {"is_slipstream": True, "router": slipstream_router, "tick_spacing": 100, "pool": POOL_B}


def test_resolve_aerodrome_route_prefers_classic_when_more_liquid(monkeypatch):
    slipstream_router = "0x" + "4" * 40
    monkeypatch.setattr(
        executor, "_best_aerodrome_slipstream_pool",
        lambda w3, token, quote: (slipstream_router, 100, POOL_B),
    )

    def balance_of(pool_addr):
        return {POOL_A.lower(): 999, POOL_B.lower(): 100}.get(pool_addr.lower(), 0)

    default_factory = "0x" + "5" * 40
    router_addr = executor.ROUTER_ADDRESSES["aerodrome"]
    w3 = _FakeW3({
        router_addr: _FakeContract({"defaultFactory": default_factory, "poolFor": lambda *_: POOL_A}),
        USDC: _FakeContract({"balanceOf": balance_of}),
    })
    route = executor._resolve_aerodrome_route(w3, USDC, WETH)
    assert route == {"is_slipstream": False, "router": router_addr, "default_factory": default_factory, "pool": POOL_A}
