# Crypto Reward Hunter

Bot d'arbitrage inter-DEX (achat sur un DEX, revente sur un autre au meilleur
prix) sur la chaîne **Base**. Conçu pour tourner en continu sur un Raspberry
Pi 3, avec notifications Telegram et dashboard web.

Le bot scanne en continu des paires de tokens sur plusieurs DEX (Uniswap,
Aerodrome, Sushiswap, Pancakeswap, Baseswap, Alien Base), calcule le profit net
estimé après frais DEX, slippage et gas, et — uniquement si le trading live est
explicitement activé via plusieurs garde-fous séparés — exécute réellement le
round-trip (achat puis revente) quand l'opportunité est jugée rentable.

## Principes de sécurité (non négociables)

- Aucune seed phrase ni clé privée en clair n'est jamais stockée ou committée :
  le wallet de trading est chiffré sur disque au format keystore Ethereum V3
  (voir `app/wallet/signer.py`).
- Le trading réel reste désactivé tant que **toutes** ces conditions ne sont pas
  réunies volontairement (voir `app/trading/guardrails.py`) :
  1. `trading.enabled: true` dans `config/config.yaml`
  2. `TRADING_ENABLED=true` dans le `.env`
  3. `TRADING_LIVE_CONFIRMED="<phrase exacte>"` dans le `.env`
  4. Le killswitch n'est pas actif
- Un killswitch (`/stop` sur Telegram) coupe instantanément le scan et toute
  tentative d'exécution.
- Chaque trade reste borné par une taille maximale (fraction du capital réel du
  wallet, plafonnée en plus par la liquidité des pools concernées) et par un
  seuil de rentabilité minimal après frais/gas.

## Architecture

```
app/
  config.py            # chargement YAML + substitution des variables d'environnement
  logger.py             # logging fichier + console
  database.py           # couche SQLite (system_state, notifications, arbitrage_signals)
  killswitch.py         # flag d'arrêt d'urgence (table system_state)
  scheduler.py           # orchestration APScheduler (scan d'arbitrage périodique)
  dashboard.py           # dashboard texte + web (FastAPI)
  notifications/         # bot Telegram (commandes /start /arbitrage /status /scan /stop)
  wallet/
    signer.py            # chargement/déchiffrement du wallet de trading (keystore chiffré)
    pricing.py           # prix USD de référence via CoinGecko (cache)
  trading/
    arbitrage.py         # détection des écarts de prix inter-DEX (simulation)
    dex_sources.py        # intégration DEX Screener (prix/liquidité par pool)
    fees.py               # estimation des frais DEX + coût de gas réel
    executor.py           # exécution réelle d'un round-trip (achat + revente)
    guardrails.py         # garde-fous d'activation du trading live
    routers.py            # adresses des routers DEX whitelistés
main.py                   # CLI (run / telegram / dashboard / status / scan-arbitrage / ...)
config/config.yaml        # configuration (intervalle de scan, seuils, tokens suivis)
.env.example               # variables d'environnement (token Telegram, RPC, wallet de trading)
scripts/                   # déploiement RPi3, systemd, watchdog, sauvegarde SQLite
```

## Installation locale

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # puis renseigner TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
python main.py scan-arbitrage  # un scan manuel, SIMULATION uniquement
python main.py status          # dashboard texte dans le terminal
python main.py run             # scheduler + Telegram + dashboard web (service complet)
```

## Déploiement sur Raspberry Pi 3

```bash
./scripts/deploy.sh florian@192.168.1.158   # envoie le code + redémarre le service
```

Ou installation initiale directement sur le Pi :

```bash
./scripts/install_rpi.sh
sudo cp scripts/crypto-reward-hunter.service /etc/systemd/system/
sudo systemctl enable --now crypto-reward-hunter
```

Pour activer le trading réel, importe d'abord ton wallet de trading **en SSH
direct sur le Pi** (jamais à distance) :

```bash
.venv/bin/python main.py import-trading-wallet
```

Scripts de maintenance (à planifier via cron) :
- `scripts/watchdog.sh` : redémarre le service si le dashboard ne répond plus.
- `scripts/backup.sh` : sauvegarde horodatée de `data/reward_hunter.db` (rétention 30 j).

## Docker

```bash
docker compose up -d --build
```

Le `Dockerfile` utilise l'index [piwheels.org](https://www.piwheels.org/) pour
récupérer des wheels ARM précompilées, adapté au Raspberry Pi 3.

## Commandes Telegram

| Commande | Description |
|---|---|
| `/start` | Démarre/réactive le bot (annule un `/stop`) |
| `/arbitrage` | Statistiques d'arbitrage (signaux détectés, rentables, P&L, trades réels) |
| `/status` | Dashboard texte résumé |
| `/scan` | Déclenche un scan d'arbitrage manuel immédiat |
| `/stop` | Coupe le scan automatique (killswitch) |

## Tests

```powershell
.venv\Scripts\python -m pytest tests -q
```

Couverture : schéma et opérations de la base de données, cycle de vie d'un
signal d'arbitrage, killswitch (`tests/test_database.py`).
