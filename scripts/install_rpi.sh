#!/usr/bin/env bash
# Installation du Crypto Reward Hunter sur Raspberry Pi 3 (Raspberry Pi OS).
# Utilise l'index piwheels.org pour récupérer des wheels précompilés ARM.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo ">> Mise à jour du système..."
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip

echo ">> Création de l'environnement virtuel..."
python3 -m venv .venv
source .venv/bin/activate

echo ">> Installation des dépendances via piwheels (wheels ARM précompilés)..."
pip install --upgrade pip
pip install --extra-index-url https://www.piwheels.org/simple -r requirements.txt

echo ">> Création des dossiers de données/logs..."
mkdir -p data logs

if [ ! -f .env ]; then
    cp .env.example .env
    echo ">> Fichier .env créé, pense à y renseigner ton token Telegram et tes adresses de wallet."
fi

echo ">> Initialisation de la base de données (projets suivis)..."
.venv/bin/python main.py seed

echo ">> Installation terminée."
echo "1. Édite config/config.yaml et .env selon tes besoins (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)."
echo "2. Teste en local       : .venv/bin/python main.py scan"
echo "3. Lance le système     : .venv/bin/python main.py run"
echo "4. Pour un service au démarrage, voir scripts/crypto-reward-hunter.service"
echo "5. Pour la sauvegarde SQLite automatique, voir scripts/backup.sh"
echo "6. Pour le watchdog, voir scripts/watchdog.sh"
