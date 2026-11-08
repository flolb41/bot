import pytest

from app.trading import guardrails
from app.trading.guardrails import (
    LIVE_CONFIRMATION_PHRASE,
    TradingRefused,
    is_trading_live,
    send_trading_transaction,
    trading_live_blockers,
)
from app.trading.routers import AERODROME_SLIPSTREAM_FACTORIES, PANCAKESWAP_V3_ROUTER_ADDRESS


def test_trading_live_blockers_report_missing_requirements_without_secret_values(monkeypatch):
    monkeypatch.delenv("TRADING_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_LIVE_CONFIRMED", raising=False)

    blockers = trading_live_blockers({"trading": {"enabled": True}})

    assert blockers == [
        "TRADING_ENABLED est absent ou désactivé",
        "TRADING_LIVE_CONFIRMED est absent ou invalide",
    ]
    assert LIVE_CONFIRMATION_PHRASE not in " ".join(blockers)


def test_trading_live_requires_all_configuration_and_environment_gates(monkeypatch):
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("TRADING_LIVE_CONFIRMED", LIVE_CONFIRMATION_PHRASE)

    assert is_trading_live({"trading": {"enabled": True}})
    assert not is_trading_live({"trading": {"enabled": False}})


def test_trading_live_confirmation_must_match_exactly(monkeypatch):
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("TRADING_LIVE_CONFIRMED", LIVE_CONFIRMATION_PHRASE.lower())

    blockers = trading_live_blockers({"trading": {"enabled": True}})

    assert blockers == ["TRADING_LIVE_CONFIRMED est absent ou invalide"]


@pytest.mark.parametrize(
    "router_address",
    [
        PANCAKESWAP_V3_ROUTER_ADDRESS,
        *(router for _, _, router in AERODROME_SLIPSTREAM_FACTORIES),
    ],
)
def test_fixed_transaction_whitelist_allows_selected_v3_routers(monkeypatch, router_address):
    monkeypatch.setattr(guardrails, "is_stopped", lambda _db: False)
    monkeypatch.setattr(
        guardrails,
        "make_w3",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("whitelist passed")),
    )

    with pytest.raises(RuntimeError, match="whitelist passed"):
        send_trading_transaction(
            None,
            rpc_url="unused",
            contract_address=router_address,
            abi=[],
            function_name="unused",
            args=(),
            whitelist_target=router_address,
            action_label="test",
        )


def test_fixed_transaction_whitelist_still_rejects_unknown_router():
    unknown_router = "0x" + "1" * 40

    with pytest.raises(TradingRefused, match="absent de la whitelist fixe"):
        send_trading_transaction(
            None,
            rpc_url="unused",
            contract_address=unknown_router,
            abi=[],
            function_name="unused",
            args=(),
            whitelist_target=unknown_router,
            action_label="test",
        )
