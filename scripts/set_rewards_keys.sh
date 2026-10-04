#!/usr/bin/env bash
# Saisie masquée des clés Binance réelles dédiées au reward hunter (REWARDS_API_KEY / REWARDS_API_SECRET).
# Usage : ./scripts/set_rewards_keys.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
[ -f "$ENV_FILE" ] || cp "$PROJECT_DIR/.env.example" "$ENV_FILE"

echo "=== Clés Binance RÉELLES pour le reward hunter (sans droit de retrait) ==="
echo "La saisie est masquée : rien ne s'affiche quand tu tapes ou colles. Valide avec Entrée."
echo

read -r -s -p "API Key    : " API_KEY; echo
read -r -s -p "API Secret : " API_SECRET; echo
read -r -s -p "Confirme le Secret : " API_SECRET2; echo
echo

if [ -z "$API_KEY" ] || [ -z "$API_SECRET" ]; then
    echo "❌ Clé ou secret vide, abandon." >&2; exit 1
fi
if [ "$API_SECRET" != "$API_SECRET2" ]; then
    echo "❌ Les deux saisies du secret ne correspondent pas, abandon." >&2; exit 1
fi
if [[ "$API_KEY" =~ [[:space:]] ]] || [[ "$API_SECRET" =~ [[:space:]] ]]; then
    echo "❌ Espace ou retour à la ligne détecté dans la saisie, abandon." >&2; exit 1
fi

# Les valeurs passent par l'environnement, jamais en argument (invisible dans l'historique et `ps`).
export API_KEY API_SECRET
python3 - "$ENV_FILE" <<'EOF'
import os, sys, re
path = sys.argv[1]
key, secret = os.environ["API_KEY"], os.environ["API_SECRET"]
lines = open(path, encoding="utf-8").read().splitlines()
out, seen = [], set()
for line in lines:
    if re.match(r"^REWARDS_API_KEY=", line):
        out.append(f"REWARDS_API_KEY={key}"); seen.add("k")
    elif re.match(r"^REWARDS_API_SECRET=", line):
        out.append(f"REWARDS_API_SECRET={secret}"); seen.add("s")
    else:
        out.append(line)
if "k" not in seen: out.append(f"REWARDS_API_KEY={key}")
if "s" not in seen: out.append(f"REWARDS_API_SECRET={secret}")
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
EOF
unset API_KEY API_SECRET API_SECRET2
chmod 600 "$ENV_FILE"

echo "✅ Clés rewards enregistrées dans $ENV_FILE (permissions 600)."
echo
echo "=== Test de lecture Simple Earn (compte réel) ==="
cd "$PROJECT_DIR"
exec .venv/bin/python main.py rewards status --live
