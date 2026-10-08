"""Compile FlashArbitrage.sol et sauvegarde l'ABI + bytecode dans un fichier
JSON (contracts/FlashArbitrage.compiled.json), commite dans le depot.

Objectif : le RPi est en architecture aarch64, or les binaires solc
precompiles distribues par solcx/binaries.soliditylang.org ne sont
disponibles qu'en linux-amd64 -> impossible d'installer solc directement
sur le Pi. On compile donc ici (Windows, ou tout poste x86_64), et le
script de deploiement sur le Pi lit cet artefact pre-compile au lieu de
recompiler localement.

Committer cet artefact permet aussi de tracer precisement quel bytecode a
ete deploye pour quelle version du source (.sol).
"""

from __future__ import annotations

import json
from pathlib import Path

import solcx

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "contracts" / "FlashArbitrage.sol"
OUT_PATH = REPO_ROOT / "contracts" / "FlashArbitrage.compiled.json"
SOLC_VERSION = "0.8.20"


def main() -> None:
    solcx.set_solc_version(SOLC_VERSION)
    out = solcx.compile_files(
        [str(CONTRACT_PATH)],
        output_values=["abi", "bin"],
        optimize=True,
        optimize_runs=200,
        solc_version=SOLC_VERSION,
    )
    key = next(k for k in out if k.endswith("FlashArbitrage"))
    abi, bytecode = out[key]["abi"], out[key]["bin"]

    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "solc_version": SOLC_VERSION,
                "optimize_runs": 200,
                "abi": abi,
                "bytecode": bytecode,
            },
            f,
            indent=2,
        )
    print(f"ABI+bytecode sauvegardes: {OUT_PATH}")
    print(f"Bytecode: {len(bytecode)} caracteres")


if __name__ == "__main__":
    main()
