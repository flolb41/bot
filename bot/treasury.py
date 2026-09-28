"""Trésorerie on-chain à deux wallets : BOT (opérationnel) et VAULT (coffre-fort).

- Le wallet BOT est le seul dont le bot connaît le mot de passe (WALLET_PASSWORD).
  Il ne contient qu'une petite somme de travail.
- Le wallet VAULT a son propre mot de passe (VAULT_PASSWORD), jamais chargé par le bot
  en fonctionnement : le bot peut y DÉPOSER (simple transfert vers son adresse) mais
  jamais en RETIRER. Si le bot ou une autorisation DeFi est compromis, le vault est isolé.
"""
from __future__ import annotations

import logging

from bot.wallet import Wallet, WalletError


class Treasury:
    def __init__(
        self,
        bot_wallet: Wallet,
        vault_wallet: Wallet,
        usdt_contract: str,
        usdt_decimals: int = 18,
        sweep_threshold: float = 200.0,
        keep_on_bot: float = 50.0,
        min_bnb_for_gas: float = 0.002,
    ):
        self.bot = bot_wallet
        self.vault = vault_wallet
        self.usdt_contract = usdt_contract
        self.usdt_decimals = usdt_decimals
        self.sweep_threshold = sweep_threshold
        self.keep_on_bot = keep_on_bot
        self.min_bnb_for_gas = min_bnb_for_gas
        self.logger = logging.getLogger("bot.treasury")

    def ready(self) -> bool:
        return self.bot.exists() and self.vault.exists()

    def balances(self) -> dict:
        out: dict = {"bot": None, "vault": None}
        if self.bot.exists():
            out["bot"] = {
                "address": self.bot.address,
                "bnb": self.bot.native_balance(),
                "usdt": self.bot.token_balance(self.usdt_contract, self.usdt_decimals),
            }
        if self.vault.exists():
            out["vault"] = {
                "address": self.vault.address,
                "bnb": self.vault.native_balance(),
                "usdt": self.vault.token_balance(self.usdt_contract, self.usdt_decimals),
            }
        return out

    def sweep_amount(self) -> float:
        """Montant USDT à déplacer du BOT vers le VAULT (0 si sous le seuil)."""
        if not self.ready():
            return 0.0
        bot_usdt = self.bot.token_balance(self.usdt_contract, self.usdt_decimals)
        if bot_usdt < self.sweep_threshold:
            return 0.0
        return max(0.0, bot_usdt - self.keep_on_bot)

    def sweep_to_vault(self, bot_password: str, amount: float | None = None) -> str | None:
        """Transfère l'excédent USDT du wallet BOT vers le VAULT. Retourne le hash ou None."""
        if not self.ready():
            raise WalletError("Les deux wallets (bot et vault) doivent exister.")
        amount = amount if amount is not None else self.sweep_amount()
        if amount <= 0:
            return None
        if self.bot.native_balance() < self.min_bnb_for_gas:
            raise WalletError(f"BNB insuffisant sur le wallet BOT pour payer le gas (< {self.min_bnb_for_gas}).")
        tx_hash = self.bot.send_token(bot_password, self.usdt_contract, self.vault.address, amount, self.usdt_decimals)
        self.logger.info("🔐 %.4f USDT déplacés vers le VAULT (%s) — tx %s", amount, self.vault.address, tx_hash)
        return tx_hash
