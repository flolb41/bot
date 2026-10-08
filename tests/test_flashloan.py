"""Tests de la logique pure (pas de réseau/RPC) de app.trading.flashloan :
lecture de config et décisions d'éligibilité au ROUTAGE flashloan vs classique
dans app.scheduler. L'exécution réelle (_build_leg, execute_*_via_flashloan)
nécessite web3/RPC et n'est pas testée ici (cohérent avec l'absence de tests
réseau pour app.trading.executor)."""
from app.trading import flashloan

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"


def test_flashloan_disabled_by_default():
    assert flashloan.flashloan_enabled({}) is False
    assert flashloan.flashloan_enabled({"trading": {}}) is False
    assert flashloan.flashloan_enabled({"trading": {"flashloan_enabled": True}}) is True


def test_flashloan_config_defaults():
    assert flashloan.flashloan_notional_usd({}) == 1000
    assert flashloan.flashloan_min_net_profit_usd({}) == 1.0
    assert flashloan.flashloan_liquidity_safety_fraction({}) == 0.02

    config = {"trading": {"flashloan_notional_usd": 50, "flashloan_min_net_profit_usd": 2.5,
                           "liquidity_safety_fraction": 0.01}}
    assert flashloan.flashloan_notional_usd(config) == 50
    assert flashloan.flashloan_min_net_profit_usd(config) == 2.5
    assert flashloan.flashloan_liquidity_safety_fraction(config) == 0.01


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
    eligible_signal = {"dex": "aerodrome", "token_a_address": WETH}
    assert flashloan.triangular_signal_is_flashloan_eligible(config, eligible_signal) is True

    # Cas théorique : "uniswap" fait partie de FLASHLOAN_CAPABLE_DEXES (jambe
    # kind=2 single-hop supportée par le contrat), mais en pratique
    # `triangular.TRIANGULAR_CAPABLE_DEXES` ne propose jamais "uniswap" comme
    # `dex` d'un signal triangulaire (scan dédié, scope distinct) — ce test
    # documente le comportement de la fonction elle-même, pas un cas réel.
    uniswap_signal = {"dex": "uniswap", "token_a_address": WETH}
    assert flashloan.triangular_signal_is_flashloan_eligible(config, uniswap_signal) is True

    disabled_config = {"trading": {"flashloan_enabled": False}}
    assert flashloan.triangular_signal_is_flashloan_eligible(disabled_config, eligible_signal) is False


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
    signal = {"dex": "aerodrome", "token_a_address": WETH, "min_liquidity_usd": 100_000, "spread_pct": 2.0}
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


def test_build_leg_v2_fork():
    # kind=0, pas d'appel RPC nécessaire : w3 n'est jamais utilisé.
    leg = flashloan._build_leg("sushiswap", _FakeW3(None), token_in=USDC, token_out=WETH)
    assert leg[0] == 0
    assert leg[2] == USDC and leg[3] == WETH
    assert leg[4] is False
    assert leg[6] == 0  # fee tier non pertinent pour un fork V2


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
    assert leg[0] == 2
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
