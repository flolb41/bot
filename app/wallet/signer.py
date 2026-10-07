"""Wallet dédié à l'auto-signature (Phase 4 du TODO — "interactions").

Contrairement au reste du module `wallet/` (strictement lecture seule), ce
fichier est le SEUL endroit du projet autorisé à manipuler une clé privée.
Règles strictes :

- La clé privée n'est JAMAIS stockée en clair sur disque : elle est chiffrée
  au format keystore Ethereum V3 (`eth_account.Account.encrypt`) protégé par
  une passphrase lue depuis `WALLET_AUTOSIGNER_PASSPHRASE` (.env, jamais Git).
- La clé privée n'est déchiffrée qu'en mémoire, au moment de signer, puis
  immédiatement abandonnée (pas de cache, pas de log, pas d'écriture disque).
- Ce wallet est séparé du wallet personnel/MetaMask de l'utilisateur
  (`WALLET_FARM_EVM_ADDRESS`) : il ne doit contenir que le strict nécessaire
  pour les transactions automatisées (voir `app/wallet/autosign.py` pour les
  garde-fous : whitelist de contrats, limites de montant/gas, simulation
  préalable, arrêt automatique en cas d'anomalie).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger("app.wallet.signer")

DEFAULT_KEYSTORE_PATH = "data/autosigner_keystore.json"


def generate_random_passphrase(length: int = 32) -> str:
    """Génère une passphrase aléatoire robuste (à stocker dans .env, jamais dans Git)."""
    return secrets.token_urlsafe(length)


def create_autosigner_wallet(keystore_path: str | Path = DEFAULT_KEYSTORE_PATH,
                              passphrase: str | None = None) -> dict:
    """Génère un NOUVEAU wallet dédié à l'auto-signature, directement en mémoire,
    puis l'écrit chiffré sur disque. La clé privée en clair n'est jamais retournée
    ni journalisée : seule l'adresse publique sort de cette fonction.

    Doit être exécuté directement sur la machine qui fera tourner le bot
    (le RPi3), jamais ailleurs : la clé privée ne doit transiter par aucun
    autre poste.
    """
    from eth_account import Account

    path = Path(keystore_path)
    if path.exists():
        raise FileExistsError(
            f"Un keystore existe déjà à {path} — supprime-le explicitement si tu veux "
            "en régénérer un nouveau (cela invalidera l'accès à l'ancien wallet)."
        )
    path.parent.mkdir(parents=True, exist_ok=True)

    used_passphrase = passphrase or os.environ.get("WALLET_AUTOSIGNER_PASSPHRASE") or generate_random_passphrase()

    account = Account.create()
    keystore = Account.encrypt(account.key, used_passphrase)

    path.write_text(json.dumps(keystore), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # chmod indisponible (ex: Windows) — sans impact sur Linux/RPi

    address = account.address
    logger.info("Wallet auto-signature créé : %s (keystore chiffré, clé privée jamais journalisée).", address)

    return {
        "address": address,
        "keystore_path": str(path),
        "passphrase_was_generated": passphrase is None and not os.environ.get("WALLET_AUTOSIGNER_PASSPHRASE"),
        "passphrase": used_passphrase,
    }


def load_signer(keystore_path: str | Path | None = None, passphrase: str | None = None):
    """Déchiffre le keystore EN MÉMOIRE et retourne un `LocalAccount` capable de signer.

    N'écrit jamais la clé déchiffrée sur disque. L'appelant doit utiliser le
    compte immédiatement puis le laisser sortir de portée (pas de variable
    globale conservant la clé déchiffrée).
    """
    from eth_account import Account

    path = Path(keystore_path or os.environ.get("WALLET_AUTOSIGNER_KEYSTORE_PATH", DEFAULT_KEYSTORE_PATH))
    used_passphrase = passphrase or os.environ.get("WALLET_AUTOSIGNER_PASSPHRASE")
    if not used_passphrase:
        raise RuntimeError("WALLET_AUTOSIGNER_PASSPHRASE manquant (.env) — impossible de déchiffrer le keystore.")
    if not path.exists():
        raise FileNotFoundError(f"Keystore auto-signature introuvable: {path}")

    keystore = json.loads(path.read_text(encoding="utf-8"))
    private_key = Account.decrypt(keystore, used_passphrase)
    return Account.from_key(private_key)


def get_autosigner_address(keystore_path: str | Path | None = None) -> str | None:
    """Lit l'adresse publique sans déchiffrer la clé privée (présente en clair dans le keystore V3)."""
    path = Path(keystore_path or os.environ.get("WALLET_AUTOSIGNER_KEYSTORE_PATH", DEFAULT_KEYSTORE_PATH))
    if not path.exists():
        return None
    try:
        keystore = json.loads(path.read_text(encoding="utf-8"))
        raw_address = keystore.get("address", "")
        return f"0x{raw_address}" if raw_address and not raw_address.startswith("0x") else (raw_address or None)
    except (OSError, json.JSONDecodeError, KeyError):
        return None
