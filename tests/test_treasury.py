from pathlib import Path

import pytest

from bot.treasury import Treasury
from bot.wallet import Wallet, WalletError

USDT = "0x55d398326f99059fF775485246999027B3197955"


@pytest.fixture
def wallets(tmp_path: Path):
    bot_path = tmp_path / "bot.json"
    vault_path = tmp_path / "vault.json"
    Wallet.create(str(bot_path), "botpassword1")
    Wallet.create(str(vault_path), "vaultpassword1")
    return Wallet(str(bot_path), "http://localhost:0"), Wallet(str(vault_path), "http://localhost:0")


def test_ready_requires_both(tmp_path: Path, wallets):
    bot, vault = wallets
    assert Treasury(bot, vault, USDT).ready() is True
    missing = Wallet(str(tmp_path / "none.json"), "http://localhost:0")
    assert Treasury(bot, missing, USDT).ready() is False


def test_sweep_amount_below_threshold(wallets, monkeypatch):
    bot, vault = wallets
    t = Treasury(bot, vault, USDT, sweep_threshold=200, keep_on_bot=50)
    monkeypatch.setattr(bot, "token_balance", lambda *_: 150.0)
    assert t.sweep_amount() == 0.0


def test_sweep_amount_above_threshold(wallets, monkeypatch):
    bot, vault = wallets
    t = Treasury(bot, vault, USDT, sweep_threshold=200, keep_on_bot=50)
    monkeypatch.setattr(bot, "token_balance", lambda *_: 320.0)
    assert t.sweep_amount() == pytest.approx(270.0)


def test_sweep_refuses_without_gas(wallets, monkeypatch):
    bot, vault = wallets
    t = Treasury(bot, vault, USDT, sweep_threshold=200, keep_on_bot=50)
    monkeypatch.setattr(bot, "token_balance", lambda *_: 320.0)
    monkeypatch.setattr(bot, "native_balance", lambda: 0.0)
    with pytest.raises(WalletError):
        t.sweep_to_vault("botpassword1")


def test_sweep_sends_to_vault_address(wallets, monkeypatch):
    bot, vault = wallets
    t = Treasury(bot, vault, USDT, sweep_threshold=200, keep_on_bot=50)
    monkeypatch.setattr(bot, "token_balance", lambda *_: 320.0)
    monkeypatch.setattr(bot, "native_balance", lambda: 0.01)
    sent = {}

    def fake_send(password, token, to, amount, decimals):
        sent.update(password=password, token=token, to=to, amount=amount)
        return "0xtx"

    monkeypatch.setattr(bot, "send_token", fake_send)
    assert t.sweep_to_vault("botpassword1") == "0xtx"
    assert sent["to"] == vault.address
    assert sent["amount"] == pytest.approx(270.0)
    assert sent["token"] == USDT
