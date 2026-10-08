"""Deploiement MAINNET du contrat FlashArbitrage (point 8).

A executer UNIQUEMENT sur le RPi, en SSH direct, avec le wallet de trading
deja configure et utilise par le bot (meme mecanisme de signature que les
transactions d'arbitrage normales : `app.wallet.signer.load_signer` + le
keystore chiffre `data/trading_wallet_keystore.json`). Le deployeur devient
automatiquement le `owner` du contrat (voir le constructeur de
FlashArbitrage.sol) : aucune configuration supplementaire necessaire.

Le contrat N'EST PAS recompile ici : le RPi est en architecture aarch64 et
les binaires solc precompiles (solcx) ne sont disponibles qu'en
linux-amd64. L'ABI + bytecode sont donc pre-compiles sur un poste x86_64
via `scripts/contracts/compile_flash_arbitrage.py` et commites dans
`contracts/FlashArbitrage.compiled.json`, que ce script se contente de lire.

SECURITE :
- Ne demande ni n'accepte jamais la cle privee en argument ou en clair :
  elle reste chiffree sur disque et n'est dechiffree qu'en memoire par
  `load_signer()`, exactement comme pour les transactions de trading du
  bot (aucun nouveau chemin de risque introduit).
- Mode `--dry-run` (par defaut) : lit l'artefact compile, estime le gas,
  affiche le cout en ETH, et s'arrete LA sans rien envoyer.
- Le deploiement reel n'a lieu qu'avec `--send`, et exige en plus de taper
  exactement la phrase de confirmation ci-dessous (anti fat-finger) :
      JE CONFIRME LE DEPLOIEMENT MAINNET DE FLASHARBITRAGE
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from web3 import Web3  # noqa: E402

from app.trading.guardrails import trading_keystore_path  # noqa: E402
from app.wallet.signer import load_signer  # noqa: E402

COMPILED_ARTIFACT_PATH = REPO_ROOT / "contracts" / "FlashArbitrage.compiled.json"
DEPLOYMENTS_DIR = REPO_ROOT / "contracts" / "deployments"
CONFIRMATION_PHRASE = "JE CONFIRME LE DEPLOIEMENT MAINNET DE FLASHARBITRAGE"

BASE_MAINNET_CHAIN_ID = 8453
AAVE_V3_BASE_MAINNET_POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"


def resolve_rpc_url() -> str:
    return os.environ.get("RPC_BASE") or "https://mainnet.base.org"


def load_compiled_artifact() -> tuple[list, str]:
    if not COMPILED_ARTIFACT_PATH.exists():
        raise SystemExit(
            f"Artefact compile introuvable: {COMPILED_ARTIFACT_PATH}\n"
            "Generez-le sur un poste x86_64 avec scripts/contracts/compile_flash_arbitrage.py "
            "puis copiez-le sur le Pi (le solc precompile n'existe pas en aarch64)."
        )
    with COMPILED_ARTIFACT_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data["abi"], data["bytecode"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true",
                         help="Envoie reellement la transaction (sinon: simulation/estimation seulement)")
    args = parser.parse_args()

    rpc_url = resolve_rpc_url()
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        raise SystemExit(f"RPC injoignable: {rpc_url}")

    chain_id = w3.eth.chain_id
    if chain_id != BASE_MAINNET_CHAIN_ID:
        raise SystemExit(f"RPC connecte a chain_id={chain_id}, attendu {BASE_MAINNET_CHAIN_ID} (Base mainnet).")

    passphrase = os.environ.get("WALLET_TRADING_PASSPHRASE")
    if not passphrase:
        raise SystemExit("WALLET_TRADING_PASSPHRASE manquant dans l'environnement (.env du Pi).")

    account = load_signer(keystore_path=trading_keystore_path(), passphrase=passphrase)
    balance_wei = w3.eth.get_balance(account.address)
    balance_eth = w3.from_wei(balance_wei, "ether")

    print(f"Reseau      : Base mainnet (chain_id={chain_id})")
    print(f"RPC         : {rpc_url}")
    print(f"Wallet      : {account.address} (owner du futur contrat)")
    print(f"Solde ETH   : {balance_eth}")
    print(f"Aave Pool   : {AAVE_V3_BASE_MAINNET_POOL}")

    abi, bytecode = load_compiled_artifact()
    print(f"Artefact compile charge, bytecode: {len(bytecode)} caracteres")

    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    nonce = w3.eth.get_transaction_count(account.address)
    gas_price_wei = w3.eth.gas_price

    tx_template = {
        "from": account.address,
        "nonce": nonce,
        "chainId": chain_id,
        "gasPrice": gas_price_wei,
    }
    estimated_gas = contract.constructor(Web3.to_checksum_address(AAVE_V3_BASE_MAINNET_POOL)).estimate_gas(
        {"from": account.address}
    )
    estimated_cost_wei = estimated_gas * gas_price_wei
    estimated_cost_eth = w3.from_wei(estimated_cost_wei, "ether")

    print(f"Gas estime  : {estimated_gas}")
    print(f"Gas price   : {w3.from_wei(gas_price_wei, 'gwei')} gwei")
    print(f"Cout estime : {estimated_cost_eth} ETH")

    if balance_wei < estimated_cost_wei:
        raise SystemExit(
            f"Solde insuffisant ({balance_eth} ETH) pour couvrir le cout estime ({estimated_cost_eth} ETH)."
        )

    if not args.send:
        print("\n--dry-run (par defaut) : aucune transaction envoyee.")
        print("Relancer avec --send pour deployer reellement (confirmation demandee).")
        return

    print(f"\nPour confirmer le deploiement REEL sur mainnet, tape exactement :\n  {CONFIRMATION_PHRASE}")
    typed = input("> ").strip()
    if typed != CONFIRMATION_PHRASE:
        raise SystemExit("Phrase de confirmation incorrecte — deploiement annule.")

    tx = contract.constructor(Web3.to_checksum_address(AAVE_V3_BASE_MAINNET_POOL)).build_transaction(
        {**tx_template, "gas": int(estimated_gas * 1.2)}
    )
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Tx envoyee: {tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    if receipt.status != 1:
        raise SystemExit(f"Le deploiement a echoue (status=0). Receipt: {receipt}")

    address = receipt.contractAddress
    print(f"FlashArbitrage deploye sur Base mainnet: {address}")
    print(f"Gas reellement utilise: {receipt.gasUsed}")

    DEPLOYMENTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DEPLOYMENTS_DIR / f"{chain_id}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "chain_id": chain_id,
                "address": address,
                "aave_pool": AAVE_V3_BASE_MAINNET_POOL,
                "deployer": account.address,
                "tx_hash": tx_hash.hex(),
                "abi": abi,
            },
            f,
            indent=2,
        )
    print(f"Infos de deploiement sauvegardees: {out_path}")


if __name__ == "__main__":
    main()
