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


def test_newly_acquired_token_amount_excludes_preexisting_wallet_balance():
    existing_balance = 2_280_000_000_000_000
    bought_amount = 1_500_000_000_000_000

    assert executor._newly_acquired_token_amount(
        existing_balance, existing_balance + bought_amount,
    ) == bought_amount
    assert executor._newly_acquired_token_amount(existing_balance, existing_balance - 1) == 0


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


def test_swap_allowance_uses_the_router_selected_for_the_swap(monkeypatch):
    slipstream_router = executor.AERODROME_SLIPSTREAM_FACTORIES[1][2]
    approval_calls = []
    swap_call = (slipstream_router, [], "exactInputSingle", ())
    monkeypatch.setattr(executor, "_build_swap_call", lambda *_args, **_kwargs: swap_call)
    monkeypatch.setattr(
        executor,
        "_ensure_allowance",
        lambda *_args, **kwargs: approval_calls.append(kwargs) or {"error": None},
    )

    built_call = executor._build_swap_call_and_ensure_allowance(
        None,
        None,
        dex="aerodrome",
        token_in=USDC,
        token_out=WETH,
        amount_in=100,
        min_amount_out=90,
        recipient=USDC,
        deadline=1,
        quote_address=USDC,
        allowance_token_address=USDC,
        owner=WETH,
        allowance_amount=100,
    )

    assert built_call == (*swap_call, {"error": None})
    assert approval_calls[0]["spender"] == slipstream_router


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


def test_quote_v2_triangular_output_uses_live_router_quote():
    router_addr = executor.ROUTER_ADDRESSES["sushiswap"]
    path = [USDC, WETH, "0x" + "1" * 40, USDC]
    w3 = _FakeW3({
        router_addr: _FakeContract({
            "getAmountsOut": lambda amount_in, _path: [amount_in, 20, 30, 1_010_000],
        }),
    })
    assert executor._quote_v2_triangular_output(w3, "sushiswap", path, 1_000_000) == 1_010_000


def test_triangular_swap_builder_rejects_ambiguous_router_dex():
    from app.trading.guardrails import TradingRefused

    with pytest.raises(TradingRefused, match="routeurs V2"):
        executor._build_triangular_swap_call(
            "pancakeswap", None, token_a=USDC, token_b=WETH, token_c="0x" + "1" * 40,
            amount_in=1, min_amount_out=1, recipient=USDC, deadline=1,
        )


# --- Garde-fous de `execute_signal` / `recover_open_positions` ----------------

WALLET = "0x" + "f" * 40
ICNT = "0x" + "e" * 40
TRANSFER_TOPIC = "0x" + __import__("web3").Web3.keccak(text="Transfer(address,address,uint256)").hex().removeprefix("0x")


def _transfer_log(token: str, sender: str, recipient: str, amount: int) -> dict:
    """Entrée `logs` d'un reçu, au format hexa tel que renvoyé par un nœud JSON-RPC."""
    def _topic(address: str) -> str:
        return "0x" + address.lower().removeprefix("0x").rjust(64, "0")
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, _topic(sender), _topic(recipient)],
        "data": "0x" + amount.to_bytes(32, "big").hex(),
    }


def _pair_signal(db, **overrides) -> dict:
    signal = {
        "chain": "base", "token_symbol": "ICNT", "token_address": ICNT,
        "quote_symbol": "USDC", "quote_address": USDC.lower(),
        "buy_dex": "uniswap", "buy_price_usd": 0.38, "buy_liquidity_usd": 100_000.0,
        "sell_dex": "pancakeswap", "sell_price_usd": 0.398, "sell_liquidity_usd": 100_000.0,
        "spread_pct": 4.5, "trade_size_usd": 0.54, "gross_profit_usd": 0.02, "fees_usd": 0.003,
        "slippage_buffer_usd": 0.001, "gas_cost_usd": 0.004, "net_profit_usd": 0.016,
        "would_execute": True, "would_execute_classic": True, "mode": "live",
    }
    signal.update(overrides)
    signal["id"] = db.add_arbitrage_signal(signal)
    return signal


class _FakeSigner:
    address = WALLET


def _stub_live_execution_environment(monkeypatch, *, quote_balance_units: int = 10 ** 9):
    """Neutralise RPC/signature/prix pour tester la logique d'`execute_signal`
    jusqu'aux garde-fous précédant l'envoi de la première transaction."""
    token_contract = _FakeContract({"balanceOf": quote_balance_units})
    monkeypatch.setattr(executor, "is_trading_live", lambda _config: True)
    monkeypatch.setattr(executor, "make_w3", lambda **_kwargs: _FakeW3({ICNT: token_contract, USDC: token_contract}))
    monkeypatch.setattr(executor, "_is_connected_with_retry", lambda _w3: True)
    monkeypatch.setattr("app.trading.guardrails.trading_keystore_path", lambda: "keystore.json")
    monkeypatch.setattr("app.wallet.signer.load_signer", lambda **_kwargs: _FakeSigner())
    monkeypatch.setattr(executor, "slippage_pct", lambda: 1.0)
    monkeypatch.setattr(executor, "_get_decimals", lambda _w3, address: 6 if address.lower() == USDC.lower() else 18)
    monkeypatch.setattr(executor, "_quote_token_usd_price", lambda _address: 1.0)
    monkeypatch.setattr(executor, "_balance_of_with_retry", lambda _contract, _address: quote_balance_units)


def test_execute_signal_refuses_before_buying_when_fresh_spread_is_below_fees(tmp_path, monkeypatch):
    from app.database import Database

    db = Database(tmp_path / "pre-buy.db")
    signal = _pair_signal(db)
    _stub_live_execution_environment(monkeypatch)
    # Spread DexScreener de 4,5 % au scan, mais on-chain l'écart réel n'est plus
    # que de 0,1 % : inférieur aux frais cumulés des deux DEX.
    prices = {"uniswap": 1.000, "pancakeswap": 1.001}
    monkeypatch.setattr(
        executor, "_refresh_price_usd",
        lambda _chain, _token, _quote, dex, reference_price_usd=None: prices[dex],
    )
    monkeypatch.setattr(
        executor, "_build_swap_call_and_ensure_allowance",
        lambda *_args, **_kwargs: pytest.fail("aucun approve ne doit être envoyé"),
    )
    monkeypatch.setattr(
        executor, "send_trading_transaction",
        lambda *_args, **_kwargs: pytest.fail("aucune transaction ne doit être envoyée"),
    )

    result = executor.execute_signal(db, {"trading": {}}, signal)

    assert result["executed"] is False
    assert result["tx_hash_buy"] is None
    assert "avant la jambe d'achat" in result["error"]
    assert "aucune transaction envoyée" in result["error"]
    stored = db.fetch_one("SELECT executed, execution_error FROM arbitrage_signals WHERE id = ?", (signal["id"],))
    assert stored["executed"] == 0
    assert stored["execution_error"] is None


def test_execute_signal_refuses_before_buying_when_sell_price_cannot_be_refreshed(tmp_path, monkeypatch):
    from app.database import Database

    db = Database(tmp_path / "pre-buy-no-sell-price.db")
    signal = _pair_signal(db)
    _stub_live_execution_environment(monkeypatch)
    prices = {"uniswap": 1.0, "pancakeswap": None}
    monkeypatch.setattr(
        executor, "_refresh_price_usd",
        lambda _chain, _token, _quote, dex, reference_price_usd=None: prices[dex],
    )
    monkeypatch.setattr(
        executor, "send_trading_transaction",
        lambda *_args, **_kwargs: pytest.fail("aucune transaction ne doit être envoyée"),
    )

    result = executor.execute_signal(db, {"trading": {}}, signal)

    assert result["executed"] is False
    assert "prix de vente avant la jambe 1" in result["error"]


def test_recovery_metrics_prorate_sale_to_units_acquired_by_the_buy_leg(monkeypatch):
    monkeypatch.setattr("app.wallet.pricing.get_price_usd", lambda _coin: 2_000.0)
    position = {"token_address": ICNT, "quote_address": USDC.lower(), "actual_gas_cost_wei": None}
    # Achat : 0,54 USDC dépensés pour 1 ICNT ; le wallet détenait déjà 4 ICNT.
    buy_receipt = {
        "gasUsed": 100_000, "effectiveGasPrice": 10 ** 10,  # 0,001 ETH = 2$ de gas
        "logs": [
            _transfer_log(USDC, WALLET, "0x" + "1" * 40, 540_000),
            _transfer_log(ICNT, "0x" + "1" * 40, WALLET, 10 ** 18),
        ],
    }
    # Récupération : les 5 ICNT sont revendus 2,70 USDC (0,54 USDC l'unité).
    sell_receipt = {
        "logs": [
            _transfer_log(ICNT, WALLET, "0x" + "2" * 40, 5 * 10 ** 18),
            _transfer_log(USDC, "0x" + "2" * 40, WALLET, 2_700_000),
        ],
    }

    metrics = executor._recovery_execution_metrics(
        position, WALLET, buy_receipt, sell_receipt,
        quote_decimals=6, quote_usd_price=1.0, recovery_gas_cost_wei=5 * 10 ** 14,  # 1$ de gas
    )

    # 1/5 du produit de la vente (0,54 USDC) est imputable à l'achat (0,54 USDC) :
    # profit brut nul, P&L net = -(gas achat 2$ + gas récupération 1$).
    assert metrics["actual_profit_quote"] == pytest.approx(0.0)
    assert metrics["actual_gas_cost_wei"] == 10 ** 15 + 5 * 10 ** 14
    assert metrics["realized_pnl_usd"] == pytest.approx(-3.0)


def test_recovery_metrics_stay_unmeasured_without_buy_receipt(monkeypatch):
    monkeypatch.setattr("app.wallet.pricing.get_price_usd", lambda _coin: 2_000.0)
    position = {"token_address": ICNT, "quote_address": USDC.lower(), "actual_gas_cost_wei": 10 ** 15}
    sell_receipt = {"logs": [
        _transfer_log(ICNT, WALLET, "0x" + "2" * 40, 5 * 10 ** 18),
        _transfer_log(USDC, "0x" + "2" * 40, WALLET, 2_700_000),
    ]}

    metrics = executor._recovery_execution_metrics(
        position, WALLET, None, sell_receipt,
        quote_decimals=6, quote_usd_price=1.0, recovery_gas_cost_wei=5 * 10 ** 14,
    )

    # Jamais estimé sur le solde entier (inventaire préexistant inclus) ; le
    # gas déjà connu de l'achat est néanmoins cumulé à celui de la récupération.
    assert metrics["realized_pnl_usd"] is None
    assert metrics["actual_profit_quote"] is None
    assert metrics["actual_gas_cost_wei"] == 10 ** 15 + 5 * 10 ** 14


def test_recover_open_positions_keeps_original_error_and_records_measured_pnl(tmp_path, monkeypatch):
    from app.database import Database

    db = Database(tmp_path / "recovery.db")
    signal = _pair_signal(db)
    original_error = "Écart refermé avant la 2e jambe (0.531% < 0.550% de frais minimum)."
    db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy="buyhash", execution_error=original_error)
    assert [p["id"] for p in db.list_open_positions()] == [signal["id"]]

    _stub_live_execution_environment(monkeypatch, quote_balance_units=5 * 10 ** 18)
    monkeypatch.setattr("app.wallet.pricing.get_price_usd", lambda _coin: 2_000.0)
    monkeypatch.setattr(executor, "_refresh_price_usd", lambda *_args, **_kwargs: 0.54)
    monkeypatch.setattr(
        executor, "_build_swap_call_and_ensure_allowance",
        lambda *_args, **_kwargs: ("0x" + "9" * 40, [], "swap", (), {"error": None, "gas_cost_wei": 0}),
    )
    sell_receipt = {"logs": [
        _transfer_log(ICNT, WALLET, "0x" + "2" * 40, 5 * 10 ** 18),
        _transfer_log(USDC, "0x" + "2" * 40, WALLET, 2_700_000),
    ]}
    monkeypatch.setattr(
        executor, "send_trading_transaction",
        lambda *_args, **_kwargs: {"sent": True, "tx_hash": "sellhash", "error": None,
                                    "gas_cost_wei": 5 * 10 ** 14, "receipt": sell_receipt},
    )
    buy_receipt = {"gasUsed": 100_000, "effectiveGasPrice": 10 ** 10, "logs": [
        _transfer_log(USDC, WALLET, "0x" + "1" * 40, 540_000),
        _transfer_log(ICNT, "0x" + "1" * 40, WALLET, 10 ** 18),
    ]}
    monkeypatch.setattr(executor, "_fetch_receipt", lambda _w3, tx_hash: buy_receipt if tx_hash == "buyhash" else None)

    results = executor.recover_open_positions(db, {"trading": {}})

    assert results == [{"position_id": signal["id"], "token_symbol": "ICNT", "recovered": True,
                        "tx_hash_sell": "sellhash", "error": None}]
    stored = db.fetch_one("SELECT * FROM arbitrage_signals WHERE id = ?", (signal["id"],))
    assert stored["tx_hash_sell"] == "sellhash"
    assert stored["execution_error"].startswith(original_error)
    assert "Position résiduelle revendue" in stored["execution_error"]
    assert stored["realized_pnl_usd"] == pytest.approx(-3.0)
    assert db.list_open_positions() == []
    # Un round-trip clôturé par récupération n'est jamais un arbitrage « réussi ».
    assert db.live_trading_stats()["completed"] == 0


# --- Prix d'exécution : on-chain obligatoire, ABI PancakeSwap V3 ----------------

def test_refresh_price_refuses_without_dexscreener_fallback_when_onchain_read_fails(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(executor, "_onchain_pool_price_usd", lambda *_args: None)
    monkeypatch.setattr(
        "app.trading.dex_sources.fetch_token_pairs",
        lambda *_args: pytest.fail("DexScreener ne doit pas servir de prix d'exécution"),
    )

    with caplog.at_level(logging.WARNING, logger="app.trading.executor"):
        price = executor._refresh_price_usd("base", ICNT, USDC.lower(), "pancakeswap", reference_price_usd=0.35)

    assert price is None
    assert any("Prix on-chain indisponible" in record.getMessage() for record in caplog.records)


def test_refresh_price_refuses_implausible_onchain_price_instead_of_falling_back(monkeypatch):
    monkeypatch.setattr(executor, "_onchain_pool_price_usd", lambda *_args: 1e40)
    monkeypatch.setattr(
        "app.trading.dex_sources.fetch_token_pairs",
        lambda *_args: pytest.fail("DexScreener ne doit pas servir de prix d'exécution"),
    )

    assert executor._refresh_price_usd("base", ICNT, USDC.lower(), "uniswap", reference_price_usd=0.35) is None


def test_refresh_price_keeps_dexscreener_fallback_for_dex_without_onchain_reader(monkeypatch):
    raw_pairs = [{
        "chainId": "base", "dexId": "balancer",
        "baseToken": {"address": ICNT}, "quoteToken": {"address": USDC},
        "priceUsd": "0.351", "liquidity": {"usd": 50_000},
    }]
    monkeypatch.setattr("app.trading.dex_sources.fetch_token_pairs", lambda *_args: raw_pairs)

    assert executor._onchain_price_supported("balancer") is False
    assert executor._refresh_price_usd("base", ICNT, USDC.lower(), "balancer", reference_price_usd=0.35) == 0.351


def test_pancakeswap_v3_pool_abi_decodes_packed_uint32_fee_protocol():
    from eth_abi import decode, encode
    from eth_abi.exceptions import DecodingError

    from app.trading.routers import PANCAKESWAP_V3_POOL_ABI, UNISWAP_V3_POOL_ABI

    def _slot0_types(abi):
        return [o["type"] for fn in abi if fn["name"] == "slot0" for o in fn["outputs"]]

    # Valeurs observées on-chain le 08/11/2026 sur la pool PancakeSwap V3
    # ICNT/USDC (fee 500) : feeProtocol = 222825800 (deux uint16 empaquetés),
    # impossible à représenter sur le uint8 de l'ABI Uniswap V3.
    raw = encode(_slot0_types(PANCAKESWAP_V3_POOL_ABI), [47_132_190_735_600_000_000_000_000, -10_392, 0, 1, 1, 222_825_800, True])

    decoded = decode(_slot0_types(PANCAKESWAP_V3_POOL_ABI), raw)
    assert decoded[0] == 47_132_190_735_600_000_000_000_000
    assert decoded[5] == 222_825_800
    with pytest.raises(DecodingError):
        decode(_slot0_types(UNISWAP_V3_POOL_ABI), raw)


# --- amountOutMinimum tenant compte des frais réels de la pool -----------------

def test_min_amount_out_removes_pool_fee_before_slippage_tolerance():
    # 1 000 000 unités attendues au prix spot, pool à 0,3 %, tolérance 0,5 % :
    # 1e6 × 0,997 × 0,995 = 992 015 (et plus 995 000, qui laissait les frais
    # consommer la tolérance).
    assert executor._min_amount_out_units(1_000_000, fee_pct=0.3, slippage_pct=0.5) == 992_015
    assert executor._min_amount_out_units(0, fee_pct=0.3, slippage_pct=0.5) == 1


def test_pool_fee_pct_uses_real_fee_tier_for_v3_pools_and_constants_otherwise(monkeypatch):
    monkeypatch.setattr(executor, "_find_uniswap_v3_fee_tier", lambda _w3, _a, _b: 500)
    assert executor._pool_fee_pct(None, "uniswap", USDC, ICNT) == pytest.approx(0.05)

    monkeypatch.setattr(
        executor, "_resolve_pancakeswap_route",
        lambda _w3, _a, _b, _q=None: {"is_v3": True, "fee": 2500, "pool": "0x" + "1" * 40},
    )
    assert executor._pool_fee_pct(None, "pancakeswap", ICNT, USDC) == pytest.approx(0.25)
    monkeypatch.setattr(
        executor, "_resolve_pancakeswap_route",
        lambda _w3, _a, _b, _q=None: {"is_v3": False, "pool": "0x" + "1" * 40},
    )
    assert executor._pool_fee_pct(None, "pancakeswap", ICNT, USDC) == pytest.approx(0.25)

    assert executor._pool_fee_pct(None, "aerodrome", ICNT, USDC) == pytest.approx(0.30)
    assert executor._pool_fee_pct(None, "sushiswap", ICNT, USDC) == pytest.approx(0.30)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("RPC indisponible")

    monkeypatch.setattr(executor, "_find_uniswap_v3_fee_tier", _boom)
    assert executor._pool_fee_pct(None, "uniswap", USDC, ICNT) == pytest.approx(0.30)


def test_execute_signal_sizes_buy_min_amount_out_with_pool_fee(tmp_path, monkeypatch):
    from app.database import Database

    db = Database(tmp_path / "min-out.db")
    signal = _pair_signal(db)
    _stub_live_execution_environment(monkeypatch)  # slippage 1,0 %
    prices = {"uniswap": 0.50, "pancakeswap": 0.52}  # écart 4 % > frais
    monkeypatch.setattr(
        executor, "_refresh_price_usd",
        lambda _chain, _token, _quote, dex, reference_price_usd=None: prices[dex],
    )
    monkeypatch.setattr(executor, "_find_uniswap_v3_fee_tier", lambda _w3, _a, _b: 500)
    captured = {}

    def fake_build(_db, _w3, **kwargs):
        captured.update(kwargs)
        return ("0x" + "9" * 40, [], "swap", (), {"error": "arrêt volontaire du test", "tx_hash": None})

    monkeypatch.setattr(executor, "_build_swap_call_and_ensure_allowance", fake_build)
    monkeypatch.setattr(
        executor, "send_trading_transaction",
        lambda *_args, **_kwargs: pytest.fail("l'approve en échec doit stopper avant tout swap"),
    )

    result = executor.execute_signal(db, {"trading": {}}, signal)

    assert "Approve (jambe achat) échoué" in result["error"]
    # 0,54 $ / 0,50 $ = 1,08 ICNT attendus au spot ; pool 0,05 % puis 1 % de tolérance.
    expected = int(0.54 / 0.50 * 10 ** 18 * (1 - 0.0005) * (1 - 0.01))
    assert captured["min_amount_out"] == expected
    assert captured["dex"] == "uniswap"
