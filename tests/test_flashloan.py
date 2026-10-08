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


def test_pair_signal_eligibility_rejects_uniswap_v3():
    # Uniswap V3 non supporté par le contrat FlashArbitrage (voir docstring
    # du module) : un signal utilisant "uniswap" comme buy_dex ou sell_dex
    # doit retomber sur l'exécution classique, jamais le flashloan.
    config = {"trading": {"flashloan_enabled": True}}
    signal = {"buy_dex": "uniswap", "sell_dex": "sushiswap", "quote_address": USDC}
    assert flashloan.pair_signal_is_flashloan_eligible(config, signal) is False


def test_pair_signal_eligibility_rejects_non_executable_quote():
    config = {"trading": {"flashloan_enabled": True}}
    signal = {"buy_dex": "aerodrome", "sell_dex": "sushiswap", "quote_address": "0xnotaquote"}
    assert flashloan.pair_signal_is_flashloan_eligible(config, signal) is False


def test_triangular_signal_eligibility():
    config = {"trading": {"flashloan_enabled": True}}
    eligible_signal = {"dex": "aerodrome", "token_a_address": WETH}
    assert flashloan.triangular_signal_is_flashloan_eligible(config, eligible_signal) is True

    uniswap_signal = {"dex": "uniswap", "token_a_address": WETH}
    assert flashloan.triangular_signal_is_flashloan_eligible(config, uniswap_signal) is False

    disabled_config = {"trading": {"flashloan_enabled": False}}
    assert flashloan.triangular_signal_is_flashloan_eligible(disabled_config, eligible_signal) is False


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
    # Notionnel borné par 2% de la liquidité min (2000$) ET par flashloan_notional_usd (1000$) -> 1000$.
    assert preview["notional_usd"] == 1000.0
    # gross=10$ (1% de 1000) - fees (0.3%*2=6$) - slippage(0.2%=2$) - premium(0.5) - gas(0.2) = 1.3$
    assert round(preview["net_profit_usd"], 2) == 1.3


def test_pair_signal_flashloan_preview_bounded_by_liquidity(monkeypatch):
    # Liquidité très faible -> notionnel calculé via liquidity_safety_fraction
    # doit primer sur flashloan_notional_usd (pas de dimensionnement irréaliste
    # par rapport à la liquidité réelle du pool).
    monkeypatch.setattr(flashloan, "_estimate_premium_and_gas_cost_usd", lambda notional, gas_units: (0.01, 0.01))
    config = {"trading": {"flashloan_enabled": True, "flashloan_notional_usd": 1000, "liquidity_safety_fraction": 0.02}}
    signal = {"buy_dex": "aerodrome", "sell_dex": "sushiswap", "quote_address": USDC,
              "buy_liquidity_usd": 1_000, "sell_liquidity_usd": 1_000, "spread_pct": 1.0}
    # 2% de 1000$ = 20$ de notionnel max -> bien en dessous de flashloan_notional_usd.
    preview = flashloan.estimate_pair_signal_flashloan_preview(config, signal)
    assert preview is not None
    assert preview["notional_usd"] == 20.0


def test_triangular_signal_flashloan_preview_computes_notional_and_profit(monkeypatch):
    monkeypatch.setattr(flashloan, "_estimate_premium_and_gas_cost_usd", lambda notional, gas_units: (0.5, 0.2))
    config = {"trading": {"flashloan_enabled": True, "flashloan_notional_usd": 1000, "liquidity_safety_fraction": 0.02}}
    signal = {"dex": "aerodrome", "token_a_address": WETH, "min_liquidity_usd": 100_000, "spread_pct": 2.0}
    preview = flashloan.estimate_triangular_signal_flashloan_preview(config, signal)
    assert preview is not None
    assert preview["notional_usd"] == 1000.0
    # gross=20$ (2% de 1000) - fees (3*0.3%=9$) - slippage(0.2%=2$) - premium(0.5) - gas(0.2) = 8.3$
    assert round(preview["net_profit_usd"], 2) == 8.3
