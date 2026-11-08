#!/usr/bin/env bash
# Installe le service perps PAPIER sur le Pi : venv dédié (.venv-perps, SDK
# Veranta) + unité systemd utilisateur. À lancer une fois, sans sudo :
#     bash ~/bot/scripts/install_perps_paper.sh
# Les mises à jour suivantes passent par scripts/self_update.sh (git), qui
# réinstalle requirements-perps.txt s'il change et redémarre le service.
set -euo pipefail

BOT_DIR="${BOT_DIR:-$HOME/bot}"
cd "$BOT_DIR"

if [ ! -x .venv-perps/bin/python ]; then
    python3 -m venv .venv-perps
fi
.venv-perps/bin/pip install -q --upgrade pip
.venv-perps/bin/pip install -q --extra-index-url https://www.piwheels.org/simple -r requirements-perps.txt
.venv-perps/bin/python -c "import veranta_sdk, fastapi, uvicorn, yaml, dotenv; import app.perps.service"

mkdir -p "$HOME/.config/systemd/user"
cp scripts/perps-paper.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now perps-paper.service
sleep 3
systemctl --user --no-pager status perps-paper.service | head -n 5
echo "Dashboard perps : http://$(hostname -I | awk '{print $1}'):8001  —  journal : $BOT_DIR/logs/perps.log"
