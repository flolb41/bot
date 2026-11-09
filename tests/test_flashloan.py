"""Tests de la logique pure (pas de réseau/RPC) de app.trading.flashloan :
lecture de config et décisions d'éligibilité au ROUTAGE flashloan vs classique
dans app.scheduler. L'exécution réelle (_build_leg, execute_*_via_flashloan)
nécessite web3/RPC et n'est pas testée ici (cohérent avec l'absence de tests
réseau pour app.trading.executor)."""
import pytest

from app.trading import flashloan

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
TOKEN_B = "0x" + "1" * 40
TOKEN_C = "0x" + "2" * 40


def _cross_dex_config(**overrides):
    trading = {
        "flashloan_enabled": True,
        "cross_dex_flashloan_enabled": True,
        "flashloan_notional_usd": 1_000,
        "flashloan_min_net_profit_usd": 0.01,
        "liquidity_safety_fraction": 0.02,
        "min_liquidity_usd": 10_000,
    }
    trading.update(overrides)
    return {"trading": trading}


def _cross_dex_signal(**overrides):
    signal = {
        "id": 1,
        "chain": "base",
        "token_a_symbol": "WETH",
        "token_a_address": WETH,
        "token_b_symbol": "TOKEN_B",
        "token_b_address": TOKEN_B,
        "token_c_symbol": "TOKEN_C",
        "token_c_address": TOKEN_C,
        "dex_ab": "sushiswap",
        "dex_bc": "baseswap",
        "dex_ca": "swapbased",
        "token_b_usd_price": 1.0,
        "token_c_usd_price": 0.5,
        "cycle_multiplier": 1.1,
        "spread_pct": 10.0,
        "min_liquidity_usd": 100_000,
        "would_execute_flashloan": True,
    }
    signal.update(overrides)
    return signal


def test_flashloan_disabled_by_default():
    assert flashloan.flashloan_enabled({}) is False
    assert flashloan.flashloan_enabled({"trading": {}}) is False
    assert flashloan.flashloan_enabled({"trading": {"flashloan_enabled": True}}) is True
    assert flashloan.cross_dex_flashloan_enabled({}) is False
    assert flashloan.cross_dex_flashloan_enabled({"trading": {}}) is False


def test_flashloan_config_defaults():
    assert flashloan.flashloan_notional_usd({}) == 1000
    assert flashloan.flashloan_min_net_profit_usd({}) == 0.0
    assert flashloan.flashloan_liquidity_safety_fraction({}) == 0.02

    config = {"trading": {"flashloan_notional_usd": 50, "flashloan_min_net_profit_usd": 2.5,
                           "liquidity_safety_fraction": 0.01}}
    assert flashloan.flashloan_notional_usd(config) == 50
    assert flashloan.flashloan_min_net_profit_usd(config) == 2.5
    assert flashloan.flashloan_liquidity_safety_fraction(config) == 0.01


def test_flashloan_profit_floor_scales_with_notional():
    config = {
        "trading": {
            "flashloan_min_net_profit_usd": 0.25,
            "min_net_profit_pct": 0.5,
        },
    }

    assert flashloan.flashloan_min_net_profit_usd(config, 20.0) == 0.25
    assert flashloan.flashloan_min_net_profit_usd(config, 100.0) == 0.5
    assert flashloan.flashloan_min_net_profit_usd(config, 10_000.0) == 50.0
    assert flashloan.flashloan_min_net_profit_usd(config) == 0.25


def test_minimum_profit_units_always_requires_positive_onchain_profit():
    assert flashloan._minimum_profit_units(0.0, 1.0, 6) == 1
    assert flashloan._minimum_profit_units(0.25, 1.0, 6) == 250_000


def test_pair_signal_eligibility_requires_flashloan_enabled():
    signal = {"buy_dex": "aerodrome", "sell_dex": "sushiswap", "quote_address": USDC}
    assert flashloan.pair_signal_is_flashloan_eligible({"trading": {"flashloan_enabled": False}}, signal) is False
    assert flashloan.pair_signal_is_flashloan_eligible({"trading": {"flashloan_enabled": True}}, signal) is True


def test_pair_signal_eligibility_accepts_uniswap_v3():
    # Uniswap V3 supporté par le contrat FlashArbitrage depuis l'ajout de la
    # jambe single-hop kind=2 (SwapRouter02 exactInputSingle, voir docstring
    # du module et contracts/FlashArbitrage.sol) : un signal utilisant
    # "uniswap" comme buy_dex ou sell_dex est désormais éligible au flashloan.
    config = {"trading": {"flashloan_enabled": True}}
    signal = {"buy_dex": "uniswap", "sell_dex": "sushiswap", "quote_address": USDC}
    assert flashloan.pair_signal_is_flashloan_eligible(config, signal) is True


def test_pair_signal_eligibility_rejects_non_executable_quote():
    config = {"trading": {"flashloan_enabled": True}}
    signal = {"buy_dex": "aerodrome", "sell_dex": "sushiswap", "quote_address": "0xnotaquote"}
    assert flashloan.pair_signal_is_flashloan_eligible(config, signal) is False


def test_triangular_signal_eligibility():
    config = {"trading": {"flashloan_enabled": True}}
    eligible_signal = {"dex": "sushiswap", "token_a_address": WETH}
    assert flashloan.triangular_signal_is_flashloan_eligible(config, eligible_signal) is True
    # Aerodrome/PancakeSwap can resolve to stable or concentrated pools that
    # the scanner's generic DEX quote does not identify exactly; never execute
    # those triangular opportunities until route-aware quoting is available.
    for dex in ("aerodrome", "pancakeswap"):
        signal = {"dex": dex, "token_a_address": WETH}
        assert flashloan.triangular_signal_is_flashloan_eligible(config, signal) is False

    # Uniswap V3 n'est pas pris en charge par le scanner triangulaire.
    uniswap_signal = {"dex": "uniswap", "token_a_address": WETH}
    assert flashloan.triangular_signal_is_flashloan_eligible(config, uniswap_signal) is False

    disabled_config = {"trading": {"flashloan_enabled": False}}
    assert flashloan.triangular_signal_is_flashloan_eligible(disabled_config, eligible_signal) is False


def test_cross_dex_signal_eligibility_requires_both_flags_and_valid_routes():
    signal = _cross_dex_signal()
    assert flashloan.cross_dex_triangular_signal_is_flashloan_eligible(
        _cross_dex_config(), signal,
    )
    assert not flashloan.cross_dex_triangular_signal_is_flashloan_eligible(
        _cross_dex_config(cross_dex_flashloan_enabled=False), signal,
    )
    assert not flashloan.cross_dex_triangular_signal_is_flashloan_eligible(
        _cross_dex_config(flashloan_enabled=False), signal,
    )
    assert not flashloan.cross_dex_triangular_signal_is_flashloan_eligible(
        _cross_dex_config(), _cross_dex_signal(chain="ethereum"),
    )
    assert not flashloan.cross_dex_triangular_signal_is_flashloan_eligible(
        _cross_dex_config(), _cross_dex_signal(token_c_address=TOKEN_B),
    )
    assert not flashloan.cross_dex_triangular_signal_is_flashloan_eligible(
        _cross_dex_config(), _cross_dex_signal(dex_ca="unknown-dex"),
    )
    assert not flashloan.cross_dex_triangular_signal_is_flashloan_eligible(
        _cross_dex_config(),
        _cross_dex_signal(dex_bc="sushiswap", dex_ca="sushiswap"),
    )


def test_cross_dex_flashloan_preview_applies_three_leg_costs_and_gas_safety(monkeypatch):
    monkeypatch.setattr(
        flashloan, "_estimate_premium_and_gas_cost_usd",
        lambda _notional, _units: (0.0, 0.01),
    )
    preview = flashloan.estimate_cross_dex_triangular_flashloan_preview(
        _cross_dex_config(), _cross_dex_signal(),
    )

    assert preview is not None
    assert 0 < preview["notional_usd"] <= 1_000
    assert preview["net_profit_usd"] > 0.01
    assert preview["estimated_gas_cost_usd"] == 0.015


def test_cross_dex_preview_refuses_missing_or_invalid_depth_and_gas(monkeypatch):
    config = _cross_dex_config()
    assert flashloan.estimate_cross_dex_triangular_flashloan_preview(
        config, _cross_dex_signal(min_liquidity_usd=None),
    ) is None

    monkeypatch.setattr(
        flashloan, "_estimate_premium_and_gas_cost_usd",
        lambda _notional, _units: (0.0, None),
    )
    assert flashloan.estimate_cross_dex_triangular_flashloan_preview(
        config, _cross_dex_signal(),
    ) is None


def test_cross_dex_executor_builds_mixed_legs_and_persists_simulated_result(
    tmp_path, monkeypatch,
):
    from app.database import Database
    from web3 import Web3

    config = _cross_dex_config()
    signal = _cross_dex_signal()
    db = Database(tmp_path / "cross-dex.db")
    signal["id"] = db.add_cross_dex_triangular_signal(signal)
    built_legs = []

    class FakeWeb3:
        class eth:
            pass

        @staticmethod
        def to_checksum_address(address):
            return Web3.to_checksum_address(address)

    class FakeEth:
        def contract(self, **_kwargs):
            return object()

    FakeWeb3.eth = FakeEth()

    monkeypatch.setattr(flashloan, "is_trading_live", lambda _config: True)
    monkeypatch.setattr(flashloan, "make_w3", lambda **_kwargs: FakeWeb3())
    monkeypatch.setattr(flashloan, "_is_connected_with_retry", lambda _w3: True)
    monkeypatch.setattr(flashloan, "_get_decimals", lambda _w3, _address: 18)
    monkeypatch.setattr(flashloan, "_quote_token_usd_price", lambda _address: 2_000.0)
    monkeypatch.setattr(flashloan, "_onchain_quote_depth_usd", lambda *_args: 100_000.0)
    monkeypatch.setattr(
        flashloan, "_estimate_premium_and_gas_cost_usd",
        lambda _notional, _units: (0.0, 0.01),
    )
    monkeypatch.setattr(
        flashloan, "_build_leg",
        lambda dex, _w3, *, token_in, token_out: (
            built_legs.append((dex, token_in, token_out)) or (dex, token_in, token_out)
        ),
    )
    monkeypatch.setattr(flashloan, "_flash_arbitrage_abi", lambda: [])
    monkeypatch.setattr(flashloan, "_flashloan_actual_profit_quote", lambda *_args: 0.25)
    monkeypatch.setattr(
        flashloan, "_actual_execution_metrics",
        lambda profit, _price, _gas: {
            "realized_pnl_usd": profit,
            "actual_profit_quote": profit,
            "actual_gas_cost_wei": 100,
            "actual_gas_cost_usd": 0.01,
        },
    )
    sent = {}

    def fake_send(*_args, **kwargs):
        sent.update(kwargs)
        return {
            "tx_hash": "0xsimulated",
            "error": None,
            "receipt": None,
            "gas_cost_wei": 100,
        }

    monkeypatch.setattr(flashloan, "send_trading_transaction", fake_send)

    result = flashloan.execute_cross_dex_triangular_signal_via_flashloan(db, config, signal)

    assert result == {"executed": True, "tx_hash": "0xsimulated", "error": None}
    assert [leg[0] for leg in built_legs] == ["sushiswap", "baseswap", "swapbased"]
    assert len(sent["args"][2]) == 3
    stored = db.fetch_one(
        "SELECT * FROM cross_dex_triangular_signals WHERE id = ?", (signal["id"],),
    )
    assert stored["execution_status"] == "executed"
    assert stored["execution_tx_hash"] == "0xsimulated"
    assert stored["actual_profit_quote"] == 0.25


def test_amm_price_impact_pct_grows_with_notional_vs_liquidity():
    # Pool profond -> impact faible ; pool fin -> impact nettement plus élevé,
    # modèle à produit constant (x*y=k), voir docstring de la fonction.
    assert flashloan._amm_price_impact_pct(1000, 1_000_000) < flashloan._amm_price_impact_pct(1000, 195_000)
    assert flashloan._amm_price_impact_pct(1000, None) == 100.0
    assert flashloan._amm_price_impact_pct(1000, 0) == 100.0


def test_optimal_flashloan_notional_usd_finds_concave_maximum():
    # Fonction concave simple : profit = 10*n - n^2 (maximum exact en n=5,
    # profit=25), bien à l'intérieur de la borne haute (100) -> la recherche
    # ternaire doit converger dessus sans buter sur la borne.
    optimal = flashloan._optimal_flashloan_notional_usd(100.0, lambda n: 10 * n - n ** 2)
    assert round(optimal, 2) == 5.0


def test_optimal_flashloan_notional_usd_clamped_by_upper_bound():
    # Fonction strictement croissante sur l'intervalle -> l'optimum est à la
    # borne haute elle-même (jamais au-delà, même si profit_fn continuerait
    # de croître : c'est le plafond de liquidité/config qui doit gagner).
    optimal = flashloan._optimal_flashloan_notional_usd(50.0, lambda n: n)
    assert round(optimal, 2) == 50.0


def test_optimal_flashloan_notional_usd_zero_upper_bound():
    assert flashloan._optimal_flashloan_notional_usd(0.0, lambda n: n) == 0.0


def test_pair_signal_flashloan_preview_returns_none_when_not_eligible():
    # Flashloan désactivé -> pas d'aperçu affiché sur le dashboard.
    config = {"trading": {"flashloan_enabled": False}}
    signal = {"buy_dex": "aerodrome", "sell_dex": "sushiswap", "quote_address": USDC,
              "buy_liquidity_usd": 100_000, "sell_liquidity_usd": 100_000, "spread_pct": 0.5}
    assert flashloan.estimate_pair_signal_flashloan_preview(config, signal) is None


def test_pair_signal_flashloan_preview_computes_notional_and_profit(monkeypatch):
    # Mock du coût gas/prime (réseau) pour tester uniquement la logique de
    # dimensionnement et d'agrégation des coûts, même principe que les tests
    # réseau mockés de test_executor.py.
    monkeypatch.setattr(flashloan, "_estimate_premium_and_gas_cost_usd", lambda notional, gas_units: (0.5, 0.2))
    config = {"trading": {"flashloan_enabled": True, "flashloan_notional_usd": 1000, "liquidity_safety_fraction": 0.02}}
    signal = {"buy_dex": "aerodrome", "sell_dex": "sushiswap", "quote_address": USDC,
              "buy_liquidity_usd": 100_000, "sell_liquidity_usd": 100_000, "spread_pct": 1.0}
    preview = flashloan.estimate_pair_signal_flashloan_preview(config, signal)
    assert preview is not None
    # Plafond = min(flashloan_notional_usd=1000$, 2% de la liquidité min=2000$) = 1000$,
    # mais la recherche ternaire (voir `_optimal_flashloan_notional_usd`) trouve un
    # notionnel bien plus faible (~50$) : au-delà, le slippage AMM convexe dévore le
    # profit linéaire plus vite qu'il ne se construit, sur un spread de seulement 1%.
    assert round(preview["notional_usd"], 2) == 50.05
    assert round(preview["net_profit_usd"], 2) == -0.12


def test_pair_signal_flashloan_preview_bounded_by_liquidity(monkeypatch):
    # Liquidité très faible -> le plafond de notionnel calculé via
    # liquidity_safety_fraction doit primer sur flashloan_notional_usd, et la
    # recherche ternaire reste bornée par ce plafond réduit (0,5$, bien en
    # dessous du plafond max de 20$ = 2% de 1000$ de liquidité).
    monkeypatch.setattr(flashloan, "_estimate_premium_and_gas_cost_usd", lambda notional, gas_units: (0.01, 0.01))
    config = {"trading": {"flashloan_enabled": True, "flashloan_notional_usd": 1000, "liquidity_safety_fraction": 0.02}}
    signal = {"buy_dex": "aerodrome", "sell_dex": "sushiswap", "quote_address": USDC,
              "buy_liquidity_usd": 1_000, "sell_liquidity_usd": 1_000, "spread_pct": 1.0}
    preview = flashloan.estimate_pair_signal_flashloan_preview(config, signal)
    assert preview is not None
    assert round(preview["notional_usd"], 2) == 0.5
    assert preview["notional_usd"] <= 20.0


def test_pair_signal_flashloan_preview_penalizes_thin_liquidity_pool(monkeypatch):
    # Cas réel observé en production (BRETT/WETH, pool PancakeSwap ~195k$) :
    # un pool nettement moins profond que l'autre côté doit faire chuter le
    # profit net estimé bien plus que le tampon fixe 0,20% ne le laissait
    # penser — c'est précisément le correctif demandé par l'utilisateur après
    # avoir observé un aperçu de +5,67$ suivi d'un refus réel à -6,98$.
    monkeypatch.setattr(flashloan, "_estimate_premium_and_gas_cost_usd", lambda notional, gas_units: (0.5, 0.2))
    config = {"trading": {"flashloan_enabled": True, "flashloan_notional_usd": 1000, "liquidity_safety_fraction": 0.02}}
    deep_pool_signal = {"buy_dex": "aerodrome", "sell_dex": "sushiswap", "quote_address": USDC,
                         "buy_liquidity_usd": 1_000_000, "sell_liquidity_usd": 1_000_000, "spread_pct": 1.37}
    thin_pool_signal = {"buy_dex": "aerodrome", "sell_dex": "sushiswap", "quote_address": USDC,
                         "buy_liquidity_usd": 1_000_000, "sell_liquidity_usd": 195_000, "spread_pct": 1.37}
    deep_preview = flashloan.estimate_pair_signal_flashloan_preview(config, deep_pool_signal)
    thin_preview = flashloan.estimate_pair_signal_flashloan_preview(config, thin_pool_signal)
    assert deep_preview["net_profit_usd"] > thin_preview["net_profit_usd"]
    # La recherche ternaire choisit un notionnel optimal DIFFÉRENT pour chaque
    # pool (plus prudent sur le pool fin), mais l'écart de profit net reste
    # significatif, uniquement dû à la liquidité disponible.
    assert deep_preview["net_profit_usd"] - thin_preview["net_profit_usd"] > 1.0


def test_triangular_signal_flashloan_preview_computes_notional_and_profit(monkeypatch):
    monkeypatch.setattr(flashloan, "_estimate_premium_and_gas_cost_usd", lambda notional, gas_units: (0.5, 0.2))
    config = {"trading": {"flashloan_enabled": True, "flashloan_notional_usd": 1000, "liquidity_safety_fraction": 0.02}}
    for dex in ("aerodrome", "pancakeswap"):
        unsupported_signal = {
            "dex": dex, "token_a_address": WETH, "min_liquidity_usd": 100_000, "spread_pct": 2.0,
        }
        assert flashloan.estimate_triangular_signal_flashloan_preview(config, unsupported_signal) is None

    signal = {"dex": "sushiswap", "token_a_address": WETH, "min_liquidity_usd": 100_000, "spread_pct": 2.0}
    preview = flashloan.estimate_triangular_signal_flashloan_preview(config, signal)
    assert preview is not None
    # Plafond = min(1000$, 2% de 100k$=2000$) = 1000$, mais la recherche
    # ternaire trouve un notionnel optimal bien plus faible (~87,73$) : avec
    # 3 jambes sur le même pool, le slippage AMM s'accumule 3x plus vite que
    # sur un aller-retour classique, rendant les gros notionnels non rentables.
    assert round(preview["notional_usd"], 2) == 87.73
    assert round(preview["net_profit_usd"], 2) == 0.26


class _FakeContractFunctions:
    def __init__(self, return_value):
        self._return_value = return_value

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            return _FakeCallable(self._return_value)
        return _call


class _FakeCallable:
    def __init__(self, return_value):
        self._return_value = return_value

    def call(self):
        return self._return_value


class _FakeContract:
    def __init__(self, return_value):
        self.functions = _FakeContractFunctions(return_value)


class _FakeEth:
    def __init__(self, return_value):
        self._return_value = return_value

    def contract(self, address, abi):  # noqa: ARG002 - signature imposée par web3
        return _FakeContract(self._return_value)


class _FakeW3:
    def __init__(self, return_value):
        self.eth = _FakeEth(return_value)


def test_build_leg_v2_fork(monkeypatch):
    # kind=0 ; la vérification d'existence du pool (voir
    # `executor._v2_fork_pool_confirmed_absent`) est monkeypatchée pour
    # confirmer le pool présent, sans appel RPC réel.
    monkeypatch.setattr(flashloan, "_v2_fork_pool_confirmed_absent", lambda w3, dex, token_in, token_out: False)
    leg = flashloan._build_leg("sushiswap", _FakeW3(None), token_in=USDC, token_out=WETH)
    assert leg[0] == 0
    assert leg[2] == USDC and leg[3] == WETH
    assert leg[4] is False
    assert leg[6] == 0  # fee tier non pertinent pour un fork V2


def test_build_leg_v2_fork_refused_when_no_pool(monkeypatch):
    # Bug confirmé en prod le 2026-10-08 (AlienBase sans pool WETH/EURC) :
    # `_build_leg` doit refuser clairement plutôt que de construire une jambe
    # qui reverterait à l'exécution avec `('execution reverted', 'no data')`.
    monkeypatch.setattr(flashloan, "_v2_fork_pool_confirmed_absent", lambda w3, dex, token_in, token_out: True)
    try:
        flashloan._build_leg("alien-base", _FakeW3(None), token_in=USDC, token_out=WETH)
        assert False, "TradingRefused attendu"
    except flashloan.TradingRefused:
        pass


def test_build_leg_aerodrome_classic(monkeypatch):
    # kind=1 : la résolution classique-vs-slipstream passe maintenant par
    # executor._resolve_aerodrome_route (importée dans le namespace de
    # flashloan) — on la monkeypatch pour simuler le cas "la pool classique a
    # la meilleure liquidité" sans appel RPC réel.
    default_factory = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
    monkeypatch.setattr(
        flashloan,
        "_resolve_aerodrome_route",
        lambda w3, token_in, token_out: {
            "is_slipstream": False, "router": "0x9999999999999999999999999999999999999999",
            "default_factory": default_factory, "pool": "0x1111111111111111111111111111111111111111",
        },
    )
    leg = flashloan._build_leg("aerodrome", _FakeW3(None), token_in=USDC, token_out=WETH)
    assert leg[0] == 1
    assert leg[5] == default_factory
    assert leg[6] == 0


def test_build_leg_aerodrome_slipstream(monkeypatch):
    # kind=3 : cas "la pool Slipstream (concentrated liquidity) a la
    # meilleure liquidité" — tickSpacing transporté dans le champ `fee`.
    slipstream_router = "0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F"
    monkeypatch.setattr(
        flashloan,
        "_resolve_aerodrome_route",
        lambda w3, token_in, token_out: {
            "is_slipstream": True, "router": slipstream_router, "tick_spacing": 100,
            "pool": "0x2222222222222222222222222222222222222222",
        },
    )
    leg = flashloan._build_leg("aerodrome", _FakeW3(None), token_in=USDC, token_out=WETH)
    assert leg[0] == 3
    assert leg[1] == slipstream_router
    assert leg[6] == 100


def test_build_leg_pancakeswap_classic(monkeypatch):
    monkeypatch.setattr(
        flashloan,
        "_resolve_pancakeswap_route",
        lambda w3, token_in, token_out: {
            "is_v3": False, "router": "0x9999999999999999999999999999999999999999",
            "pool": "0x1111111111111111111111111111111111111111",
        },
    )
    leg = flashloan._build_leg("pancakeswap", _FakeW3(None), token_in=USDC, token_out=WETH)
    assert leg[0] == 0
    assert leg[6] == 0


def test_build_leg_pancakeswap_v3(monkeypatch):
    pcs_v3_router = "0x1b81D678ffb9C0263b24A97847620C99d213eB14"
    monkeypatch.setattr(
        flashloan,
        "_resolve_pancakeswap_route",
        lambda w3, token_in, token_out: {
            "is_v3": True, "router": pcs_v3_router, "fee": 500,
            "pool": "0x2222222222222222222222222222222222222222",
        },
    )
    leg = flashloan._build_leg("pancakeswap", _FakeW3(None), token_in=USDC, token_out=WETH)
    assert leg[0] == 4
    assert leg[1] == pcs_v3_router
    assert leg[6] == 500


def test_build_leg_uniswap_v3(monkeypatch):
    # kind=2 : le fee tier est résolu on-chain via executor._find_uniswap_v3_fee_tier
    # (importée dans le namespace de flashloan) — on la monkeypatch ici pour
    # éviter tout appel RPC réel.
    monkeypatch.setattr(flashloan, "_find_uniswap_v3_fee_tier", lambda w3, token_in, token_out: 500)
    leg = flashloan._build_leg("uniswap", _FakeW3(None), token_in=USDC, token_out=WETH)
    assert leg[0] == 2
    assert leg[2] == USDC and leg[3] == WETH
    assert leg[6] == 500


def test_build_leg_rejects_unsupported_dex():
    import pytest

    with pytest.raises(flashloan.TradingRefused):
        flashloan._build_leg("not-a-dex", _FakeW3(None), token_in=USDC, token_out=WETH)


def test_cross_dex_executor_depth_refusal_remembers_the_shallow_pool(tmp_path, monkeypatch):
    from app.database import Database
    from app.trading import pool_memory

    config = _cross_dex_config()
    signal = _cross_dex_signal()
    db = Database(tmp_path / "cross-dex-shallow.db")
    signal["id"] = db.add_cross_dex_triangular_signal(signal)

    class FakeEth:
        def contract(self, **_kwargs):
            return object()

    class FakeWeb3:
        eth = FakeEth()

    monkeypatch.setattr(flashloan, "is_trading_live", lambda _config: True)
    monkeypatch.setattr(flashloan, "make_w3", lambda **_kwargs: FakeWeb3())
    monkeypatch.setattr(flashloan, "_is_connected_with_retry", lambda _w3: True)
    monkeypatch.setattr(flashloan, "_get_decimals", lambda _w3, _address: 18)
    monkeypatch.setattr(flashloan, "_quote_token_usd_price", lambda _address: 2_000.0)
    depths = {("sushiswap", WETH.lower(), TOKEN_B): 24.98}
    monkeypatch.setattr(
        flashloan, "_onchain_quote_depth_usd",
        lambda dex, token_in, token_out, *_args: depths.get((dex, token_in.lower(), token_out.lower()), 100_000.0),
    )
    monkeypatch.setattr(
        flashloan, "send_trading_transaction",
        lambda *_args, **_kwargs: pytest.fail("aucune transaction ne doit être envoyée"),
    )

    result = flashloan.execute_cross_dex_triangular_signal_via_flashloan(db, config, signal)

    assert result["executed"] is False
    assert "Profondeur on-chain 24.98$ sous le plancher" in result["error"]
    assert pool_memory.recent_shallow_pool_depth("sushiswap", TOKEN_B, WETH) == 24.98   # clé symétrique
    assert pool_memory.is_pool_recently_shallow("baseswap", TOKEN_B, TOKEN_C) is False
