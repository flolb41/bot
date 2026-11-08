"""Small, separately tracked live momentum positions."""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timezone

from app.database import Database
from app.trading.paper_strategies import _STRATEGIES, _eligible, _market_candidates, _record_observation

logger = logging.getLogger("app.trading.directional_live")

_MAX_SLIPPAGE_PCT = 0.5
_ENTRY_COOLDOWN_SECONDS = 5 * 60
_EXIT_COOLDOWN_SECONDS = 15 * 60


def _positive(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number > 0 else default


def _utc_day_start(now: float) -> float:
    return datetime.fromtimestamp(now, timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).timestamp()


def _gas_cost_usd(gas_wei: int) -> float | None:
    from app.trading import fees
    from app.wallet.pricing import get_price_usd

    eth_usd = get_price_usd("ethereum")
    if eth_usd is None or eth_usd <= 0:
        return None
    return fees.actual_gas_cost_usd(gas_wei, eth_usd)


def _quote_address(config: dict) -> str | None:
    trading = (config or {}).get("trading") or {}
    for token in trading.get("seed_tokens") or []:
        if str(token.get("symbol", "")).upper() == "USDC":
            return str(token["address"]).lower()
    return None


def _daily_loss_blocks_entries(db: Database, now: float, loss_limit_usd: float) -> bool:
    state = db.directional_daily_pnl_state(_utc_day_start(now))
    if int(state.get("unmeasured") or 0):
        logger.warning("Entrées directionnelles suspendues : le P&L live du jour n'est pas entièrement mesuré.")
        return True
    return float(state.get("pnl") or 0.0) <= -loss_limit_usd


def _exit_reason(
    position: dict, price: float, now: float, strategy_cfg: dict,
    daily_pnl: float, loss_limit_usd: float,
) -> str | None:
    move_pct = (price / float(position["entry_price_usd"]) - 1) * 100
    if move_pct >= _positive(strategy_cfg.get("take_profit_pct"), 2.0):
        return "take_profit"
    if move_pct <= -_positive(strategy_cfg.get("stop_loss_pct"), 1.5):
        return "stop_loss"
    if (now - float(position["entry_at"])) / 60 >= _positive(
        strategy_cfg.get("max_hold_minutes"), 60.0,
    ):
        return "max_hold"
    open_pnl = float(position["stake_usd"]) * move_pct / 100
    if position.get("entry_gas_cost_usd") is not None:
        open_pnl -= float(position["entry_gas_cost_usd"])
    if daily_pnl + open_pnl <= -loss_limit_usd:
        return "daily_loss_limit"
    return None


def _entry_candidates(db: Database, config: dict, groups: dict, now: float) -> list[tuple[float, str, dict]]:
    from app.trading.paper_strategies import _positive_config

    quote_address = _quote_address(config)
    if quote_address is None:
        logger.error("Trading directionnel suspendu : l'adresse USDC n'est pas configurée parmi les tokens de trading.")
        return []

    paper_cfg = (config or {}).get("paper_research") or {}
    candidates = []
    for candidate in _market_candidates(groups).values():
        if candidate["quote_address"] != quote_address:
            continue
        for strategy_name, base_rules in _STRATEGIES.items():
            strategy_cfg = paper_cfg.get(strategy_name) or {}
            rules = {
                **base_rules,
                "min_liquidity_usd": _positive_config(
                    strategy_cfg, "min_liquidity_usd", base_rules["min_liquidity_usd"],
                ),
                "min_volume_24h_usd": _positive_config(
                    strategy_cfg, "min_volume_24h_usd", base_rules["min_volume_24h_usd"],
                ),
                "max_fdv_usd": (
                    _positive_config(strategy_cfg, "max_fdv_usd", base_rules["max_fdv_usd"])
                    if base_rules["max_fdv_usd"] is not None else None
                ),
            }
            if not _eligible(candidate, rules):
                continue
            observation_strategy = f"live_{strategy_name}"
            _record_observation(db, observation_strategy, candidate, now)
            lookback = int(_positive_config(strategy_cfg, "lookback_minutes", 15))
            max_age = int(_positive_config(strategy_cfg, "max_reference_age_minutes", 20))
            reference = db.fetch_one(
                "SELECT price_usd FROM paper_market_observations "
                "WHERE strategy=? AND token_address=? AND quote_address=? AND observed_at<=? "
                "AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",
                (
                    observation_strategy, candidate["token_address"], quote_address,
                    now - lookback * 60, now - max_age * 60,
                ),
            )
            if reference is None or float(reference["price_usd"]) <= 0:
                continue
            change_pct = (candidate["price_usd"] / float(reference["price_usd"]) - 1) * 100
            entry_pct = _positive_config(strategy_cfg, "entry_return_pct", 1.0)
            max_entry_pct = _positive_config(strategy_cfg, "max_entry_return_pct", 5.0)
            if entry_pct <= change_pct <= max_entry_pct:
                candidates.append((candidate["liquidity_usd"], strategy_name, candidate))
    return sorted(candidates, key=lambda item: item[0], reverse=True)


def _record_failed_entry(db: Database, trade_id: int, error: str, tx_hash: str | None = None) -> None:
    db.execute(
        "UPDATE directional_live_trades SET status='failed', execution_error=?, "
        "attempt_tx_hash=COALESCE(?, attempt_tx_hash) WHERE id=? AND status='pending'",
        (error, tx_hash, trade_id),
    )


def _token_amount_for_storage(amount: int) -> str:
    if not 0 <= amount < 2**256:
        raise ValueError("Token amount must fit an unsigned 256-bit integer.")
    return str(amount)


def _position_token_amount(position: dict) -> int:
    raw_amount = position.get("token_amount_raw")
    return int(raw_amount) if raw_amount is not None else int(position["token_amount"])


def _open_position(db: Database, config: dict, strategy: str, candidate: dict, now: float) -> None:
    from web3 import Web3

    from app.trading.executor import (
        ERC20_ABI, TradingRefused, _balance_of_with_retry, _build_swap_call,
        _ensure_allowance, _get_decimals, _min_amount_out_units, _net_token_transfer_from_receipts,
        _onchain_quote_depth_usd, _pool_fee_pct, _quote_token_usd_price, _refresh_price_usd,
        _resolve_rpc_url, make_w3,
    )
    from app.trading.guardrails import send_trading_transaction, trading_keystore_path
    from app.wallet.signer import load_signer

    live_cfg = (config or {}).get("directional_live") or {}
    quote = candidate["quote_address"]
    token = candidate["token_address"]
    stake_usd = min(_positive(live_cfg.get("position_usd"), 2.0), 2.0)
    quote_usd = _quote_token_usd_price(quote)
    if quote_usd is None or quote_usd <= 0:
        logger.warning("Entrée refusée pour %s : prix USDC indisponible.", candidate["token_symbol"])
        return

    fresh_price = _refresh_price_usd("base", token, quote, candidate["dex"], candidate["price_usd"])
    if fresh_price is None or fresh_price <= 0:
        logger.warning("Entrée refusée pour %s : prix on-chain/DexScreener non disponible.", candidate["token_symbol"])
        return
    if abs(fresh_price / candidate["price_usd"] - 1) > 0.02:
        logger.warning("Entrée refusée pour %s : prix frais trop éloigné du signal.", candidate["token_symbol"])
        return
    depth = _onchain_quote_depth_usd(candidate["dex"], token, quote)
    required_depth = candidate["liquidity_usd"] / 2
    if depth is None or depth < required_depth:
        logger.warning(
            "Entrée refusée pour %s : profondeur on-chain insuffisante ou inconnue (%.2f$ requise).",
            candidate["token_symbol"], required_depth,
        )
        return

    trade_id = db.execute(
        "INSERT INTO directional_live_trades "
        "(strategy, token_address, token_symbol, quote_address, dex, entry_at, entry_price_usd, "
        "stake_usd, token_amount, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending')",
        (
            strategy, token, candidate["token_symbol"], quote, candidate["dex"],
            now, fresh_price, stake_usd,
        ),
    )
    attempt_hash = None
    buy_hash = None
    try:
        w3 = make_w3(timeout=15)
        signer = load_signer(
            keystore_path=trading_keystore_path(),
            passphrase=os.environ.get("WALLET_TRADING_PASSPHRASE"),
        )
        wallet = signer.address
        signer = None
        quote_decimals = _get_decimals(w3, quote)
        token_decimals = _get_decimals(w3, token)
        amount_in = int(stake_usd / quote_usd * (10 ** quote_decimals))
        quote_contract = w3.eth.contract(address=Web3.to_checksum_address(quote), abi=ERC20_ABI)
        if _balance_of_with_retry(quote_contract, wallet) < amount_in:
            raise TradingRefused(f"Solde USDC insuffisant pour engager au plus {stake_usd:.2f} $.")

        expected_tokens = stake_usd / fresh_price * (10 ** token_decimals)
        min_tokens = _min_amount_out_units(
            expected_tokens,
            fee_pct=_pool_fee_pct(w3, candidate["dex"], quote, token, quote),
            slippage_pct=_MAX_SLIPPAGE_PCT,
        )
        router, abi, function_name, args = _build_swap_call(
            candidate["dex"], w3, token_in=quote, token_out=token,
            amount_in=amount_in, min_amount_out=min_tokens, recipient=wallet,
            deadline=int(time.time()) + 180, quote_address=quote,
        )
        approval = _ensure_allowance(
            db, w3, token_address=quote, owner=wallet, spender=router, amount=amount_in,
        )
        attempt_hash = approval.get("tx_hash")
        if attempt_hash:
            db.execute(
                "UPDATE directional_live_trades SET attempt_tx_hash=? WHERE id=?",
                (attempt_hash, trade_id),
            )
        if approval.get("error"):
            raise TradingRefused(f"Approbation USDC échouée : {approval['error']}")

        token_contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        balance_before = _balance_of_with_retry(token_contract, wallet)
        result = send_trading_transaction(
            db, rpc_url=_resolve_rpc_url(), contract_address=router, abi=abi,
            function_name=function_name, args=args, whitelist_target=router,
            action_label=f"achat directionnel {candidate['token_symbol']} pour {stake_usd:.2f}$",
            count_against_daily_limit=not bool(attempt_hash),
        )
        buy_hash = result.get("tx_hash")
        attempt_hash = buy_hash or attempt_hash
        if attempt_hash:
            db.execute(
                "UPDATE directional_live_trades SET attempt_tx_hash=? WHERE id=?",
                (attempt_hash, trade_id),
            )
        if result.get("error"):
            receipt = result.get("receipt")
            if receipt is not None and int(receipt.get("status", 0)) == 0:
                db.execute(
                    "UPDATE directional_live_trades SET status='failed', entry_tx_hash=?, "
                    "execution_error=? WHERE id=?",
                    (buy_hash, result["error"], trade_id),
                )
                db.set_state("directional_entry_cooldown_until", str(time.time() + _ENTRY_COOLDOWN_SECONDS))
                return
            raise TradingRefused(f"Achat refusé/échoué : {result['error']}")

        receipt = result.get("receipt")
        received = _net_token_transfer_from_receipts(token, wallet, [receipt])
        if received is None:
            balance_after = _balance_of_with_retry(token_contract, wallet)
            received = max(0, balance_after - balance_before)
        gas_wei = int(approval.get("gas_cost_wei") or 0) + int(result.get("gas_cost_wei") or 0)
        try:
            gas_usd = _gas_cost_usd(gas_wei) if gas_wei else 0.0
        except Exception:
            logger.exception("Coût du gas d'entrée impossible à mesurer pour %s.", candidate["token_symbol"])
            gas_usd = None
        if received <= 0:
            db.execute(
                "UPDATE directional_live_trades SET status='unresolved', entry_tx_hash=?, "
                "entry_gas_cost_usd=?, execution_error=? WHERE id=?",
                (result.get("tx_hash"), gas_usd, "Achat confirmé mais quantité reçue non mesurable.", trade_id),
            )
            db.add_notification(
                "⚠️ Position directionnelle à vérifier",
                f"{candidate['token_symbol']} : achat confirmé, quantité non mesurable. Aucune nouvelle entrée ne sera ouverte.",
                level="alert",
            )
            return
        db.execute(
            "UPDATE directional_live_trades SET status='open', token_amount_raw=?, entry_tx_hash=?, "
            "entry_gas_cost_usd=?, execution_error=NULL WHERE id=?",
            (_token_amount_for_storage(received), result.get("tx_hash"), gas_usd, trade_id),
        )
        db.add_notification(
            "✅ Position directionnelle ouverte",
            f"{candidate['token_symbol']}/{candidate.get('quote_symbol', 'USDC')} — mise {stake_usd:.2f}$, "
            f"tx {result.get('tx_hash')}",
            level="info",
        )
    except Exception as exc:
        if buy_hash:
            db.execute(
                "UPDATE directional_live_trades SET status='unresolved', entry_tx_hash=?, "
                "attempt_tx_hash=COALESCE(?, attempt_tx_hash), execution_error=? WHERE id=?",
                (buy_hash, attempt_hash, str(exc), trade_id),
            )
            db.add_notification(
                "⚠️ Position directionnelle à vérifier",
                f"{candidate['token_symbol']} : achat diffusé mais suivi incomplet ({exc}). "
                "Aucune nouvelle entrée ne sera ouverte.",
                level="alert",
            )
        else:
            _record_failed_entry(db, trade_id, str(exc), attempt_hash)
        db.set_state("directional_entry_cooldown_until", str(time.time() + _ENTRY_COOLDOWN_SECONDS))
        logger.warning("Entrée directionnelle refusée pour %s : %s", candidate["token_symbol"], exc)
        db.add_notification(
            "⚠️ Entrée directionnelle non exécutée",
            f"{candidate['token_symbol']} : {exc}",
            level="warning",
        )


def _close_position(db: Database, config: dict, trade: dict, now: float, price: float, reason: str) -> None:
    from web3 import Web3

    from app.trading.executor import (
        ERC20_ABI, TradingRefused, _balance_of_with_retry, _build_swap_call,
        _ensure_allowance, _get_decimals, _min_amount_out_units, _net_token_transfer_from_receipts,
        _pool_fee_pct, _quote_token_usd_price, _resolve_rpc_url, make_w3,
    )
    from app.trading.guardrails import send_trading_transaction, trading_keystore_path
    from app.wallet.signer import load_signer

    w3 = make_w3(timeout=15)
    signer = load_signer(
        keystore_path=trading_keystore_path(),
        passphrase=os.environ.get("WALLET_TRADING_PASSPHRASE"),
    )
    wallet = signer.address
    signer = None
    token = trade["token_address"]
    quote = trade["quote_address"]
    amount = _position_token_amount(trade)
    token_decimals = _get_decimals(w3, token)
    quote_decimals = _get_decimals(w3, quote)
    token_contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
    if _balance_of_with_retry(token_contract, wallet) < amount:
        raise TradingRefused("Solde du token inférieur à la quantité achetée par cette position; vente refusée.")

    quote_usd = _quote_token_usd_price(quote)
    if quote_usd is None or quote_usd <= 0:
        raise TradingRefused("Prix USD du token de cotation indisponible; vente différée.")
    min_quote = _min_amount_out_units(
        amount / (10 ** token_decimals) * price / quote_usd * (10 ** quote_decimals),
        fee_pct=_pool_fee_pct(w3, trade["dex"], token, quote, quote),
        slippage_pct=_MAX_SLIPPAGE_PCT,
    )
    router, abi, function_name, args = _build_swap_call(
        trade["dex"], w3, token_in=token, token_out=quote,
        amount_in=amount, min_amount_out=min_quote, recipient=wallet,
        deadline=int(time.time()) + 180, quote_address=quote,
    )
    approval = _ensure_allowance(
        db, w3, token_address=token, owner=wallet, spender=router, amount=amount,
        count_against_daily_limit=False,
    )
    if approval.get("error"):
        raise TradingRefused(f"Approbation du token à vendre échouée : {approval['error']}")
    result = send_trading_transaction(
        db, rpc_url=_resolve_rpc_url(), contract_address=router, abi=abi,
        function_name=function_name, args=args, whitelist_target=router,
        action_label=f"vente directionnelle {trade['token_symbol']} ({reason})",
        count_against_daily_limit=False,
    )
    if result.get("error"):
        raise TradingRefused(f"Vente échouée : {result['error']}")

    try:
        received_quote = _net_token_transfer_from_receipts(
            quote, wallet, [result.get("receipt")],
        )
    except Exception:
        logger.exception("Lecture du transfert de sortie impossible pour %s.", trade["token_symbol"])
        received_quote = None
    exit_gas_wei = int(approval.get("gas_cost_wei") or 0) + int(result.get("gas_cost_wei") or 0)
    try:
        exit_gas_usd = _gas_cost_usd(exit_gas_wei) if exit_gas_wei else 0.0
    except Exception:
        logger.exception("Coût du gas de sortie impossible à mesurer pour %s.", trade["token_symbol"])
        exit_gas_usd = None
    entry_gas_usd = trade["entry_gas_cost_usd"]
    net_pnl = None
    if received_quote is not None and exit_gas_usd is not None and entry_gas_usd is not None:
        net_pnl = (
            received_quote / (10 ** quote_decimals) * quote_usd
            - float(trade["stake_usd"]) - float(entry_gas_usd) - exit_gas_usd
        )
    db.execute(
        "UPDATE directional_live_trades SET status='closed', exit_at=?, exit_price_usd=?, "
        "exit_tx_hash=?, exit_gas_cost_usd=?, net_pnl_usd=?, exit_reason=?, execution_error=NULL "
        "WHERE id=? AND status='open'",
        (now, price, result.get("tx_hash"), exit_gas_usd, net_pnl, reason, trade["id"]),
    )
    db.set_state("directional_entry_cooldown_until", str(now + _EXIT_COOLDOWN_SECONDS))
    db.add_notification(
        "✅ Position directionnelle clôturée",
        f"{trade['token_symbol']} — motif {reason}, P&L net "
        f"{'non mesuré' if net_pnl is None else f'{net_pnl:+.4f}$'}, tx {result.get('tx_hash')}",
        level="info" if net_pnl is not None and net_pnl >= 0 else "warning",
    )


def run_live_directional_strategy(db: Database, config: dict, groups: dict[str, list[dict]]) -> None:
    """Manage one USDC-quoted momentum position; never invoke the arbitrage path."""
    live_cfg = (config or {}).get("directional_live") or {}
    if not live_cfg.get("enabled", False):
        return
    from app.trading.guardrails import is_trading_live, is_stopped

    if not is_trading_live(config) or is_stopped(db):
        return

    now = time.time()
    position = db.directional_open_trade()
    if position is not None:
        if position["status"] != "open":
            logger.error(
                "Trading directionnel suspendu : position %s en état %s nécessitant une vérification.",
                position["token_symbol"], position["status"],
            )
            return
        from app.trading.executor import _refresh_price_usd

        price = _refresh_price_usd(
            "base", position["token_address"], position["quote_address"],
            position["dex"], position["entry_price_usd"],
        )
        if price is None or price <= 0:
            logger.warning("Position %s conservée : prix de sortie indisponible.", position["token_symbol"])
            return
        strategy_cfg = ((config or {}).get("paper_research") or {}).get(position["strategy"]) or {}
        daily_state = db.directional_daily_pnl_state(_utc_day_start(now))
        reason = _exit_reason(
            position, price, now, strategy_cfg,
            float(daily_state.get("pnl") or 0.0),
            _positive(live_cfg.get("daily_loss_limit_usd"), 1.0),
        )
        if reason:
            try:
                _close_position(db, config, position, now, price, reason)
            except Exception as exc:
                logger.exception("Clôture directionnelle en échec pour %s.", position["token_symbol"])
                db.execute(
                    "UPDATE directional_live_trades SET execution_error=? WHERE id=? AND status='open'",
                    (str(exc), position["id"]),
                )
                db.add_notification(
                    "⚠️ Clôture directionnelle en attente",
                    f"{position['token_symbol']} — {exc}",
                    level="alert",
                )
        return

    loss_limit = _positive(live_cfg.get("daily_loss_limit_usd"), 1.0)
    if _daily_loss_blocks_entries(db, now, loss_limit):
        return
    cooldown_until = db.get_state("directional_entry_cooldown_until")
    if cooldown_until and now < float(cooldown_until):
        return
    candidates = _entry_candidates(db, config, groups, now)
    if candidates:
        _, strategy, candidate = candidates[0]
        _open_position(db, config, strategy, candidate, now)
