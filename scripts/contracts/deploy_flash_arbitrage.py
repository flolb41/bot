"""Deploiement du contrat FlashArbitrage (point 8 - flashloan atomique).

Usage:
    python scripts/contracts/deploy_flash_arbitrage.py \
        --rpc-url https://sepolia.base.org \
        --private-key-env TESTNET_DEPLOYER_PRIVATE_KEY \
        --aave-pool 0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27

Le contrat est d'abord recompile a la volee avec solcx (garantit que le
bytecode deploye correspond toujours au .sol courant), puis deploye et
l'adresse + l'ABI sont sauvegardes dans contracts/deployments/<chain_id>.json.

IMPORTANT : ce script ne doit etre utilise que lorsque le wallet cible
(deployeur) a ete explicitement approvisionne et verifie par l'utilisateur.
Ne jamais utiliser la cle privee du wallet de trading reel du bot tant que
le contrat n'a pas ete valide (tests + revue) sur un reseau de test.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import solcx
from eth_account import Account
from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "contracts" / "FlashArbitrage.sol"
DEPLOYMENTS_DIR = REPO_ROOT / "contracts" / "deployments"
SOLC_VERSION = "0.8.20"


def compile_contract() -> tuple[list, str]:
    solcx.set_solc_version(SOLC_VERSION)
    out = solcx.compile_files(
        [str(CONTRACT_PATH)],
        output_values=["abi", "bin"],
        optimize=True,
        optimize_runs=200,
        solc_version=SOLC_VERSION,
    )
    key = next(k for k in out if k.endswith(":FlashArbitrage") or k.endswith("FlashArbitrage"))
    return out[key]["abi"], out[key]["bin"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", required=True, help="URL RPC du reseau cible (testnet ou mainnet)")
    parser.add_argument(
        "--private-key-env",
        required=True,
        help="Nom de la variable d'environnement contenant la cle privee du deployeur (jamais en argument direct)",
    )
    parser.add_argument("--aave-pool", required=True, help="Adresse du contrat Pool Aave V3 sur ce reseau")
    args = parser.parse_args()

    private_key = os.environ.get(args.private_key_env)
    if not private_key:
        raise SystemExit(f"Variable d'environnement '{args.private_key_env}' absente ou vide.")

    w3 = Web3(Web3.HTTPProvider(args.rpc_url))
    if not w3.is_connected():
        raise SystemExit(f"Impossible de se connecter au RPC: {args.rpc_url}")

    account = Account.from_key(private_key)
    chain_id = w3.eth.chain_id
    balance = w3.eth.get_balance(account.address)
    print(f"Reseau: chain_id={chain_id}")
    print(f"Deployeur: {account.address} (solde: {w3.from_wei(balance, 'ether')} ETH)")
    if balance == 0:
        raise SystemExit("Solde nul: le wallet deployeur n'a pas d'ETH pour payer le gas.")

    abi, bytecode = compile_contract()
    print("Contrat compile (solc", SOLC_VERSION, ")")

    aave_pool = Web3.to_checksum_address(args.aave_pool)
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    nonce = w3.eth.get_transaction_count(account.address)
    tx = contract.constructor(aave_pool).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "chainId": chain_id,
        }
    )
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Tx de deploiement envoyee: {tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise SystemExit(f"Le deploiement a echoue (status=0). Receipt: {receipt}")

    address = receipt.contractAddress
    print(f"FlashArbitrage deploye a l'adresse: {address}")
    print(f"Gas utilise: {receipt.gasUsed}")

    DEPLOYMENTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DEPLOYMENTS_DIR / f"{chain_id}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "chain_id": chain_id,
                "address": address,
                "aave_pool": aave_pool,
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
