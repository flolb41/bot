#!/usr/bin/env bash
# Déploie le bot sur le Raspberry Pi et redémarre les services.
# Usage : ./scripts/deploy.sh [user@host]   (défaut : florian@192.168.1.158)
# N'envoie que les fichiers nécessaires à l'exécution (pas de tests, .git, venv, données, secrets).
set -euo pipefail

TARGET="${1:-florian@192.168.1.158}"
REMOTE_DIR="~/bot"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo ">> Envoi du code vers $TARGET:$REMOTE_DIR"
tar czf - \
  --exclude=.git --exclude=.venv --exclude=tests --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=data --exclude=logs --exclude=.env --exclude=requirements-dev.txt --exclude=pyproject.toml \
  --exclude=.gitattributes --exclude=.gitignore \
  . | ssh "$TARGET" "mkdir -p $REMOTE_DIR && tar xzf - -C $REMOTE_DIR \
      && cd $REMOTE_DIR && sed -i 's/\r\$//' scripts/*.sh scripts/*.service config/*.yaml .env.example \
      && chmod +x scripts/*.sh"

echo ">> Mise à jour des dépendances Python"
ssh "$TARGET" "cd $REMOTE_DIR && .venv/bin/pip install -q --extra-index-url https://www.piwheels.org/simple -r requirements.txt"

echo ">> Redémarrage des services (mot de passe sudo du Pi)"
ssh -t "$TARGET" "sudo systemctl restart trading-bot trading-bot-web && sleep 5 && systemctl is-active trading-bot trading-bot-web"

echo ">> Déploiement terminé."
