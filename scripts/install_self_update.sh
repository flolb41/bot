#!/usr/bin/env bash
# Rend le Pi autonome pour ses mises à jour : ~/bot devient un clone git de
# la branche main et bot-self-update.timer applique chaque nouveau commit
# (voir scripts/self_update.sh). À lancer UNE fois sur le Pi, en tant
# qu'utilisateur du service (pas de sudo) :
#     bash ~/bot/scripts/install_self_update.sh [URL_DU_DEPOT] [BRANCHE]
set -euo pipefail

BOT_DIR="${BOT_DIR:-$HOME/bot}"
REPO_URL="${1:-https://github.com/flolb41/bot.git}"
BRANCH="${2:-main}"
cd "$BOT_DIR"

if [ ! -d .git ]; then
    git init -q -b "$BRANCH"
    git remote add origin "$REPO_URL"
fi
# Le bit exécutable des scripts est géré par chmod côté Pi : ne pas le
# considérer comme une modification locale (ce qui bloquerait les mises à jour).
git config core.fileMode false
git fetch -q origin "$BRANCH"
# Aligne les fichiers SUIVIS sur le dépôt ; .env, data/, logs/, deploy-backups/
# et autres fichiers non suivis ou ignorés ne sont pas touchés.
git reset -q --hard "origin/$BRANCH"
git branch -q --set-upstream-to="origin/$BRANCH" "$BRANCH" 2>/dev/null || true
chmod +x scripts/*.sh

mkdir -p "$HOME/.config/systemd/user"
cp scripts/bot-self-update.service scripts/bot-self-update.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now bot-self-update.timer

echo "Clone git établi sur $(git rev-parse --short HEAD) ($BRANCH) ; mise à jour automatique active :"
systemctl --user list-timers bot-self-update.timer --no-pager
echo "Journal : $BOT_DIR/logs/self_update.log"
