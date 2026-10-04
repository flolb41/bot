from pathlib import Path
import getpass

import pytest

from bot.wallet import Wallet, WalletError
from main import _wallet_password


def test_create_and_reload_wallet(tmp_path: Path):
    keystore = tmp_path / "keystore.json"
    address = Wallet.create(str(keystore), "motdepasse123")
    assert address.startswith("0x")
    assert keystore.exists()

    wallet = Wallet(str(keystore), rpc_url="http://localhost:0")
    assert wallet.address.lower() == address.lower()


def test_create_refuses_existing_keystore(tmp_path: Path):
    keystore = tmp_path / "keystore.json"
    Wallet.create(str(keystore), "motdepasse123")
    with pytest.raises(WalletError):
        Wallet.create(str(keystore), "motdepasse123")


def test_create_refuses_short_password(tmp_path: Path):
    with pytest.raises(WalletError):
        Wallet.create(str(tmp_path / "k.json"), "court")


def test_unlock_with_wrong_password(tmp_path: Path):
    keystore = tmp_path / "keystore.json"
    Wallet.create(str(keystore), "motdepasse123")
    wallet = Wallet(str(keystore), rpc_url="http://localhost:0")
    with pytest.raises(WalletError):
        wallet._unlock("mauvais_mdp")


def test_unlock_with_correct_password(tmp_path: Path):
    keystore = tmp_path / "keystore.json"
    Wallet.create(str(keystore), "motdepasse123")
    wallet = Wallet(str(keystore), rpc_url="http://localhost:0")
    key = wallet._unlock("motdepasse123")
    assert len(bytes(key)) == 32


def test_wallet_creation_rejects_password_mismatch(monkeypatch):
    answers = iter(["motdepasse123", "motdepasse456"])
    monkeypatch.delenv("WALLET_PASSWORD", raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(answers))

    with pytest.raises(SystemExit, match="ne correspondent pas"):
        _wallet_password("bot", confirm=True)
