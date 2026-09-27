#!/usr/bin/env bash
# Installation du bot de trading sur Raspberry Pi 3 (Raspberry Pi OS).
# Utilise l'index piwheels.org pour récupérer des wheels précompilés ARM
# (indispensable : compiler pandas/numpy depuis les sources sur un Pi 3 est très long, voire impossible avec 1 Go de RAM).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo ">> Mise à jour du système..."
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip

echo ">> Vérification du swap (recommandé >= 1 Go pour l'installation des dépendances)..."
free -h

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
    echo ">> Fichier .env créé, pense à y renseigner tes clés API et ton token Telegram."
fi

echo ">> Installation terminée."
echo "1. Édite config/config.yaml et .env selon tes besoins."
echo "2. Teste en mode paper : .venv/bin/python main.py backtest --days 30"
echo "3. Lance le bot        : .venv/bin/python main.py run"
echo "4. Pour le faire tourner en service au démarrage, voir scripts/trading-bot.service"
