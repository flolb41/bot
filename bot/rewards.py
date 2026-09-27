"""Collecte automatique de récompenses légitimes et gestion du coffre (vault).

Sources implémentées :
- Binance Simple Earn flexible : le solde inutilisé est souscrit et génère des intérêts,
  rachetables instantanément quand le bot a besoin de capital.
- Conversion des poussières (petits soldes résiduels) en BNB.
- Auto-compounding : un pourcentage des profits de trading est envoyé au coffre.

En mode paper, le coffre est simulé en base SQLite avec un APR configurable, pour
valider le circuit complet sans argent réel.

Note : le farming automatisé de faucets/airdrops n'est volontairement PAS implémenté
(contournement de captchas et violation des CGU des services).
"""
from __future__ import annotations

import logging
import time

from bot.db import Database


class RewardsCollector:
    def __init__(
        self,
        db: Database,
        mode: str,
        exchange=None,
        notifier=None,
        asset: str = "USDT",
        paper_apr_pct: float = 5.0,
        profit_skim_pct: float = 30.0,
        min_idle_to_subscribe: float = 50.0,
        keep_free: float = 100.0,
        min_trading_balance: float = 50.0,
        dust_enabled: bool = True,
    ):
        self.db = db
        self.mode = mode
        self.exchange = exchange
        self.notifier = notifier
        self.asset = asset
        self.paper_apr_pct = paper_apr_pct
        self.profit_skim_pct = profit_skim_pct
        self.min_idle_to_subscribe = min_idle_to_subscribe
        self.keep_free = keep_free
        self.min_trading_balance = min_trading_balance
        self.dust_enabled = dust_enabled
        self.logger = logging.getLogger("bot.rewards")

    def _notify(self, msg: str) -> None:
        if self.notifier:
            self.notifier.send(msg)

    # --- Coffre -----------------------------------------------------------
    def vault_balance(self) -> float:
        if self.mode == "live":
            return self._earn_balance_live()
        amount, _, _ = self.db.get_vault(self.asset)
        return amount

    def total_rewards(self) -> float:
        _, rewards, _ = self.db.get_vault(self.asset)
        return rewards

    # --- Compounding des profits -------------------------------------------
    def skim_profit(self, pnl: float) -> float:
        """Envoie un % du profit vers le coffre. Retourne le montant écrémé."""
        if pnl <= 0 or self.profit_skim_pct <= 0:
            return 0.0
        skimmed = pnl * (self.profit_skim_pct / 100.0)
        if self.mode == "live":
            if not self._earn_subscribe_live(skimmed):
                return 0.0
        amount, rewards, _ = self.db.get_vault(self.asset)
        self.db.set_vault(self.asset, amount + skimmed, rewards)
        self.logger.info("Profit écrémé vers le coffre: %.4f %s", skimmed, self.asset)
        return skimmed

    # --- Souscription du solde inactif ---------------------------------------
    def subscribe_idle(self, free_balance: float) -> float:
        """Place le solde libre excédentaire dans Earn. Retourne le montant souscrit."""
        idle = free_balance - self.keep_free
        if idle < self.min_idle_to_subscribe:
            return 0.0
        if self.mode == "live":
            if not self._earn_subscribe_live(idle):
                return 0.0
        amount, rewards, _ = self.db.get_vault(self.asset)
        self.db.set_vault(self.asset, amount + idle, rewards)
        msg = f"💰 {idle:.2f} {self.asset} inactifs placés dans le coffre (Earn)"
        self.logger.info(msg)
        self._notify(msg)
        return idle

    # --- Recharge du capital de trading ---------------------------------------
    def refill_if_needed(self, free_balance: float) -> float:
        """Rachète depuis le coffre si le solde de trading est trop bas. Retourne le montant récupéré."""
        if free_balance >= self.min_trading_balance:
            return 0.0
        vault = self.vault_balance()
        needed = min(vault, self.keep_free - free_balance)
        if needed <= 0:
            return 0.0
        if self.mode == "live":
            if not self._earn_redeem_live(needed):
                return 0.0
        amount, rewards, _ = self.db.get_vault(self.asset)
        self.db.set_vault(self.asset, max(0.0, amount - needed), rewards)
        msg = f"🔄 {needed:.2f} {self.asset} rapatriés du coffre vers le capital de trading"
        self.logger.info(msg)
        self._notify(msg)
        return needed

    # --- Cycle de collecte ------------------------------------------------------
    def collect(self) -> None:
        """Cycle périodique : accumulation des intérêts (paper) + conversion des poussières (live)."""
        if self.mode == "paper":
            self._accrue_paper()
        else:
            self._sync_vault_live()
            if self.dust_enabled:
                self._convert_dust_live()

    def _accrue_paper(self) -> None:
        amount, rewards, updated_at = self.db.get_vault(self.asset)
        if amount <= 0:
            return
        elapsed_days = (time.time() - updated_at) / 86400.0
        interest = amount * (self.paper_apr_pct / 100.0) * (elapsed_days / 365.0)
        if interest > 0:
            self.db.set_vault(self.asset, amount + interest, rewards + interest)
            self.logger.debug("Intérêts simulés: +%.6f %s", interest, self.asset)

    # --- Implémentation Binance (mode live) ------------------------------------
    def _earn_product_id(self) -> str | None:
        try:
            resp = self.exchange.exchange.sapi_get_simple_earn_flexible_list({"asset": self.asset})
            rows = resp.get("rows", [])
            if rows:
                return rows[0]["productId"]
        except Exception as exc:
            self.logger.warning("Impossible de récupérer le produit Earn %s: %s", self.asset, exc)
        return None

    def _earn_balance_live(self) -> float:
        try:
            resp = self.exchange.exchange.sapi_get_simple_earn_flexible_position({"asset": self.asset})
            return sum(float(r["totalAmount"]) for r in resp.get("rows", []))
        except Exception as exc:
            self.logger.warning("Lecture du solde Earn impossible: %s", exc)
            return 0.0

    def _earn_subscribe_live(self, amount: float) -> bool:
        product_id = self._earn_product_id()
        if not product_id:
            return False
        try:
            self.exchange.exchange.sapi_post_simple_earn_flexible_subscribe(
                {"productId": product_id, "amount": round(amount, 8)}
            )
            return True
        except Exception as exc:
            self.logger.warning("Souscription Earn échouée: %s", exc)
            return False

    def _earn_redeem_live(self, amount: float) -> bool:
        product_id = self._earn_product_id()
        if not product_id:
            return False
        try:
            self.exchange.exchange.sapi_post_simple_earn_flexible_redeem(
                {"productId": product_id, "amount": round(amount, 8)}
            )
            return True
        except Exception as exc:
            self.logger.warning("Rachat Earn échoué: %s", exc)
            return False

    def _sync_vault_live(self) -> None:
        """Aligne le coffre local sur le solde Earn réel et trace les intérêts gagnés."""
        live_balance = self._earn_balance_live()
        amount, rewards, _ = self.db.get_vault(self.asset)
        gained = max(0.0, live_balance - amount)
        self.db.set_vault(self.asset, live_balance, rewards + gained)

    def _convert_dust_live(self) -> None:
        try:
            dust = self.exchange.exchange.sapi_post_asset_dust_btc({})
            details = dust.get("details", [])
            assets = [d["asset"] for d in details if d.get("asset") not in (self.asset, "BNB")]
            if not assets:
                return
            self.exchange.exchange.sapi_post_asset_dust({"asset": ",".join(assets)})
            msg = f"🧹 Poussières converties en BNB: {', '.join(assets)}"
            self.logger.info(msg)
            self._notify(msg)
        except Exception as exc:
            self.logger.debug("Conversion des poussières impossible (normal si rien à convertir): %s", exc)
