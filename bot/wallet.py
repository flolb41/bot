"""Wallet on-chain local sécurisé (EVM, BSC par défaut).

Sécurité :
- La clé privée n'est JAMAIS stockée en clair : keystore V3 standard Ethereum (scrypt).
- Le mot de passe vient de la variable d'environnement WALLET_PASSWORD ou d'une saisie
  interactive (getpass), jamais de config.yaml.
- Fichier keystore en permissions 600 (sous Linux/RPi).
- Les appels réseau passent par JSON-RPC brut (requests) : pas de dépendance web3 lourde,
  important sur un Raspberry Pi 3.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import requests
from eth_account import Account

# Sélecteur ERC20/BEP20 transfer(address,uint256)
_TRANSFER_SELECTOR = "a9059cbb"
# Sélecteur balanceOf(address)
_BALANCEOF_SELECTOR = "70a08231"


class WalletError(Exception):
    pass


class Wallet:
    def __init__(self, keystore_path: str, rpc_url: str, chain_id: int = 56):
        self.keystore_path = Path(keystore_path)
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.logger = logging.getLogger("bot.wallet")

    # --- Gestion de la clé ------------------------------------------------
    @staticmethod
    def create(keystore_path: str, password: str) -> str:
        """Génère une nouvelle clé, la chiffre (keystore V3) et retourne l'adresse."""
        path = Path(keystore_path)
        if path.exists():
            raise WalletError(f"Un keystore existe déjà: {path}. Suppression manuelle requise (attention aux fonds !).")
        if len(password) < 8:
            raise WalletError("Le mot de passe du wallet doit faire au moins 8 caractères.")

        acct = Account.create()
        keystore = Account.encrypt(acct.key, password)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(keystore), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # non supporté sous Windows
        return acct.address

    def exists(self) -> bool:
        return self.keystore_path.exists()

    @property
    def address(self) -> str:
        if not self.exists():
            raise WalletError("Aucun keystore. Crée d'abord le wallet: python main.py wallet create")
        data = json.loads(self.keystore_path.read_text(encoding="utf-8"))
        return "0x" + data["address"]

    def _unlock(self, password: str) -> bytes:
        data = json.loads(self.keystore_path.read_text(encoding="utf-8"))
        try:
            return Account.decrypt(data, password)
        except ValueError as exc:
            raise WalletError("Mot de passe du wallet incorrect.") from exc

    # --- JSON-RPC ---------------------------------------------------------
    def _rpc(self, method: str, params: list) -> str:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        resp = requests.post(self.rpc_url, json=payload, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise WalletError(f"Erreur RPC {method}: {body['error']}")
        return body["result"]

    # --- Soldes -----------------------------------------------------------
    def native_balance(self) -> float:
        """Solde en BNB (ou monnaie native de la chaîne)."""
        wei = int(self._rpc("eth_getBalance", [self.address, "latest"]), 16)
        return wei / 1e18

    def token_balance(self, token_address: str, decimals: int = 18) -> float:
        """Solde d'un token BEP-20/ERC-20 (ex: USDT sur BSC)."""
        data = "0x" + _BALANCEOF_SELECTOR + self.address[2:].lower().zfill(64)
        result = self._rpc("eth_call", [{"to": token_address, "data": data}, "latest"])
        return int(result, 16) / (10**decimals)

    # --- Envois -----------------------------------------------------------
    def _send_raw(self, signed_hex: str) -> str:
        return self._rpc("eth_sendRawTransaction", [signed_hex])

    def _base_tx(self, gas: int) -> dict:
        nonce = int(self._rpc("eth_getTransactionCount", [self.address, "pending"]), 16)
        gas_price = int(self._rpc("eth_gasPrice", []), 16)
        return {"nonce": nonce, "gas": gas, "gasPrice": gas_price, "chainId": self.chain_id}

    def send_native(self, password: str, to: str, amount: float) -> str:
        """Envoie du BNB natif. Retourne le hash de transaction."""
        key = self._unlock(password)
        tx = self._base_tx(gas=21_000)
        tx.update({"to": to, "value": int(amount * 1e18)})
        signed = Account.sign_transaction(tx, key)
        tx_hash = self._send_raw(signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex())
        self.logger.info("Transaction native envoyée: %s", tx_hash)
        return tx_hash

    def send_token(self, password: str, token_address: str, to: str, amount: float, decimals: int = 18) -> str:
        """Envoie un token BEP-20 (ex: USDT vers l'adresse de dépôt de l'exchange)."""
        key = self._unlock(password)
        raw_amount = int(amount * (10**decimals))
        data = (
            "0x"
            + _TRANSFER_SELECTOR
            + to[2:].lower().zfill(64)
            + hex(raw_amount)[2:].zfill(64)
        )
        tx = self._base_tx(gas=80_000)
        tx.update({"to": token_address, "value": 0, "data": data})
        signed = Account.sign_transaction(tx, key)
        tx_hash = self._send_raw(signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex())
        self.logger.info("Transfert token envoyé: %s", tx_hash)
        return tx_hash
