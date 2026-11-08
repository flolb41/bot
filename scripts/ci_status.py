#!/usr/bin/env python3
"""Statut CI GitHub d'un commit, pour la mise à jour automatique du Pi.

Usage : ci_status.py OWNER/REPO SHA [NOM_DU_CHECK]

Code de sortie (consommé par scripts/self_update.sh) :
    0  le check exigé est terminé avec succès
    2  en attente (check pas encore créé, en file d'attente ou en cours)
    3  le check a échoué (failure, cancelled, timed_out, action_required…)
    4  statut indéterminable (API GitHub injoignable, réponse inattendue)

Dépôt public : aucune authentification requise (60 requêtes/heure par IP,
largement suffisant : l'appel n'a lieu que lorsqu'un nouveau commit est
détecté). Un jeton optionnel `GITHUB_TOKEN` relève la limite.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

EXIT_SUCCESS = 0
EXIT_PENDING = 2
EXIT_FAILED = 3
EXIT_UNKNOWN = 4

_OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def fetch_check_runs(repo: str, sha: str, check_name: str, opener=urllib.request.urlopen) -> list[dict]:
    """Check runs GitHub Actions du commit `sha` portant le nom `check_name`."""
    query = urllib.parse.urlencode({"check_name": check_name, "per_page": 50})
    url = f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?{query}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "crypto-reward-hunter-self-update",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with opener(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return list(payload.get("check_runs") or [])


def evaluate(check_runs: list[dict]) -> int:
    """Verdict à partir des check runs (plusieurs exécutions possibles du même
    workflow : seule la plus récente compte)."""
    if not check_runs:
        return EXIT_PENDING
    latest = max(check_runs, key=lambda run: run.get("started_at") or run.get("id") or 0)
    if latest.get("status") != "completed":
        return EXIT_PENDING
    return EXIT_SUCCESS if latest.get("conclusion") in _OK_CONCLUSIONS else EXIT_FAILED


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return EXIT_UNKNOWN
    repo, sha = argv[1], argv[2]
    check_name = argv[3] if len(argv) > 3 else "tests"
    try:
        runs = fetch_check_runs(repo, sha, check_name)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"Statut CI indisponible pour {sha[:8]} : {exc}", file=sys.stderr)
        return EXIT_UNKNOWN
    verdict = evaluate(runs)
    label = {EXIT_SUCCESS: "succès", EXIT_PENDING: "en attente", EXIT_FAILED: "échec"}[verdict]
    print(f"CI « {check_name} » pour {sha[:8]} : {label} ({len(runs)} exécution(s))")
    return verdict


if __name__ == "__main__":
    sys.exit(main(sys.argv))
