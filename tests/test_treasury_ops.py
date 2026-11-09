"""Tests du dépôt/retrait USDC sur Aave V3 (app/trading/treasury_ops.py) — tout est simulé."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.database import Database
from app.trading import treasury_ops
from app.trading.guardrails import trading_whitelist
from app.trading.routers import AAVE_V3_POOL_ADDRESS, AAVE_V3_USDC_ATOKEN_ADDRESS, YIELD_CONTRACT_ADDRESSES

WALLET = "0x032d16Ec1F4382287cBBFbB87Dd8163934A01803"


class _FakeW3:
    pass


def _wire(monkeypatch, *, liquid: float, deposited: float, live: bool = True, apr: float = 4.86):
    """Branche un snapshot déterministe et capture les transactions « envoyées »."""
    sent: list[dict] = []
    approvals: list[dict] = []
    balances = {"liquid": liquid, "deposited": deposited}

    def snapshot(_w3, wallet):
        return {"wallet": wallet, "liquid_usdc": balances["liquid"], "liquid_units": int(balances["liquid"] * 1e6),
                "deposited_usdc": balances["deposited"], "deposited_units": int(balances["deposited"] * 1e6), "apr_pct": apr}

    def fake_send(_db, **kwargs):
        sent.append(kwargs)
        amount = kwargs["args"][1]
        if kwargs["function_name"] == "supply":
            balances["liquid"] -= amount / 1e6
            balances["deposited"] += amount / 1e6
        else:
            taken = balances["deposited"] if amount == treasury_ops.MAX_UINT256 else amount / 1e6
            balances["deposited"] -= taken
            balances["liquid"] += taken
        return {"sent": True, "tx_hash": f"0xtx{len(sent)}", "error": None}

    def fake_allowance(_db, _w3, **kwargs):
        approvals.append(kwargs)
        return {"sent": True, "tx_hash": "0xapprove", "error": None}

    monkeypatch.setattr(treasury_ops, "make_w3", lambda timeout=15: _FakeW3())
    monkeypatch.setattr(treasury_ops, "_is_connected_with_retry", lambda _w3: True)
    monkeypatch.setattr(treasury_ops, "_wallet_address", lambda: WALLET)
    monkeypatch.setattr(treasury_ops, "treasury_snapshot", snapshot)
    monkeypatch.setattr(treasury_ops, "send_trading_transaction", fake_send)
    monkeypatch.setattr(treasury_ops, "_ensure_allowance", fake_allowance)
    monkeypatch.setattr(treasury_ops, "is_trading_live", lambda _config: live)
    monkeypatch.setattr(treasury_ops, "_resolve_rpc_url", lambda: "http://rpc")
    return sent, approvals


def test_aave_pool_is_in_the_fixed_whitelist_and_nothing_else_new():
    assert AAVE_V3_POOL_ADDRESS.lower() in trading_whitelist()
    assert YIELD_CONTRACT_ADDRESSES == frozenset({AAVE_V3_POOL_ADDRESS})
    assert AAVE_V3_USDC_ATOKEN_ADDRESS.lower() not in trading_whitelist()   # l'aToken n'est jamais appelé


def test_deposit_dry_run_plans_everything_above_the_float(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    sent, approvals = _wire(monkeypatch, liquid=10.35, deposited=0.0)
    result = treasury_ops.deposit_usdc(db, {}, None, keep_float_usdc=2.0, dry_run=True)
    assert result["dry_run"] and result["error"] is None and result["amount_usdc"] == pytest.approx(8.35)
    assert sent == [] and approvals == []
    # Montant explicite au-delà du solde / du flottant : refusé avant tout envoi.
    assert "solde liquide" in treasury_ops.deposit_usdc(db, {}, 50, dry_run=True)["error"]
    assert "flottant" in treasury_ops.deposit_usdc(db, {}, 9.5, keep_float_usdc=2.0, dry_run=True)["error"]
    assert "Rien à déposer" in treasury_ops.deposit_usdc(db, {}, None, keep_float_usdc=10.0, dry_run=True)["error"]


def test_deposit_execute_approves_exact_amount_then_supplies_on_behalf_of_wallet(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    sent, approvals = _wire(monkeypatch, liquid=10.35, deposited=0.0)
    result = treasury_ops.deposit_usdc(db, {}, 8.0, keep_float_usdc=2.0, dry_run=False)
    assert result["sent"] and result["tx_hash"] == "0xtx1" and result["approve_tx_hash"] == "0xapprove"
    assert approvals[0]["spender"] == AAVE_V3_POOL_ADDRESS and approvals[0]["amount"] == 8_000_000
    assert approvals[0]["count_against_daily_limit"] is False
    call = sent[0]
    assert call["contract_address"] == AAVE_V3_POOL_ADDRESS and call["whitelist_target"] == AAVE_V3_POOL_ADDRESS
    assert call["function_name"] == "supply" and call["count_against_daily_limit"] is False
    asset, amount, on_behalf_of, referral = call["args"]
    assert asset.lower() == treasury_ops.USDC_ADDRESS and amount == 8_000_000 and referral == 0
    assert on_behalf_of.lower() == WALLET.lower()                      # jamais une autre adresse
    assert result["after"]["deposited_usdc"] == pytest.approx(8.0) and result["after"]["liquid_usdc"] == pytest.approx(2.35)


def test_withdraw_all_uses_max_uint_and_pays_the_wallet(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    sent, _ = _wire(monkeypatch, liquid=2.35, deposited=8.0123)
    preview = treasury_ops.withdraw_usdc(db, {}, None, dry_run=True)
    assert preview["amount_usdc"] == pytest.approx(8.0123) and sent == []
    result = treasury_ops.withdraw_usdc(db, {}, None, dry_run=False)
    asset, amount, to = sent[0]["args"]
    assert amount == treasury_ops.MAX_UINT256 and to.lower() == WALLET.lower() and sent[0]["function_name"] == "withdraw"
    assert result["sent"] and result["after"]["deposited_usdc"] == pytest.approx(0.0)
    # Partiel : montant exact ; au-delà du dépôt : refusé.
    _wire(monkeypatch, liquid=2.35, deposited=8.0)
    partial = treasury_ops.withdraw_usdc(db, {}, 3.0, dry_run=True)
    assert partial["amount_usdc"] == 3.0 and partial["error"] is None
    assert "> déposé" in treasury_ops.withdraw_usdc(db, {}, 30.0, dry_run=True)["error"]
    _wire(monkeypatch, liquid=2.35, deposited=0.0)
    assert "Aucun dépôt" in treasury_ops.withdraw_usdc(db, {}, None, dry_run=True)["error"]


def test_real_sends_require_live_trading(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    sent, approvals = _wire(monkeypatch, liquid=10.0, deposited=5.0, live=False)
    assert "Trading live désactivé" in treasury_ops.deposit_usdc(db, {}, 5.0, dry_run=False)["error"]
    assert "Trading live désactivé" in treasury_ops.withdraw_usdc(db, {}, None, dry_run=False)["error"]
    assert sent == [] and approvals == []
    assert treasury_ops.deposit_usdc(db, {}, 5.0, dry_run=True)["error"] is None   # l'aperçu reste possible


def test_auto_sweep_deposits_only_above_float_and_minimum(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    monkeypatch.setattr("app.trading.guardrails.is_stopped", lambda _db: False)
    config = {"treasury": {"auto_deposit": True, "keep_float_usdc": 2.0, "min_sweep_usdc": 5.0}}

    sent, _ = _wire(monkeypatch, liquid=6.0, deposited=10.0)        # 4 USDC au-dessus du flottant < 5 : rien
    assert treasury_ops.auto_sweep(db, config) is None and sent == []

    sent, _ = _wire(monkeypatch, liquid=107.0, deposited=10.0)      # 105 USDC balayés, flottant conservé
    result = treasury_ops.auto_sweep(db, config)
    assert result["sent"] and result["amount_usdc"] == pytest.approx(105.0)
    assert result["after"]["liquid_usdc"] == pytest.approx(2.0) and result["after"]["deposited_usdc"] == pytest.approx(115.0)
    notes = db.list_notifications(limit=5)
    assert notes and "déposés automatiquement" in notes[0]["message"]

    sent, _ = _wire(monkeypatch, liquid=107.0, deposited=10.0, live=False)   # trading live coupé : silence
    assert treasury_ops.auto_sweep(db, config) is None and sent == []
    sent, _ = _wire(monkeypatch, liquid=107.0, deposited=10.0)
    assert treasury_ops.auto_sweep(db, {"treasury": {"auto_deposit": False}}) is None and sent == []
    monkeypatch.setattr("app.trading.guardrails.is_stopped", lambda _db: True)   # killswitch : silence
    assert treasury_ops.auto_sweep(db, config) is None and sent == []


def test_treasury_snapshot_decodes_balances_and_rate(monkeypatch):
    calls = {}

    class _Fn:
        def __init__(self, value):
            self._value = value

        def call(self):
            return self._value

    class _Contract:
        def __init__(self, address, abi):
            self.address = address.lower()
            self.functions = self

        def balanceOf(self, _addr):
            return _Fn({treasury_ops.USDC_ADDRESS: 10_347_899, AAVE_V3_USDC_ATOKEN_ADDRESS.lower(): 8_000_000}[self.address])

        def getReserveData(self, _asset):
            rate_ray = 48_786_662_290_443_916_770_045_446
            return _Fn([(0,), 0, rate_ray, 0, 0, 0, 0, 0, AAVE_V3_USDC_ATOKEN_ADDRESS, "0x0", "0x0", "0x0", 0, 0, 0])

    w3 = SimpleNamespace(eth=SimpleNamespace(contract=lambda address, abi: _Contract(address, abi)))
    monkeypatch.setattr(treasury_ops, "_balance_of_with_retry", lambda contract, addr: contract.balanceOf(addr).call())
    snap = treasury_ops.treasury_snapshot(w3, WALLET)
    assert snap["liquid_usdc"] == pytest.approx(10.347899) and snap["deposited_usdc"] == pytest.approx(8.0)
    assert snap["apr_pct"] == pytest.approx(4.8787, rel=1e-3)
