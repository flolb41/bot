# 🤖 Bot de trading crypto pour Raspberry Pi 3

Bot de trading crypto complet, léger et fonctionnel, conçu pour tourner en continu sur un Raspberry Pi 3
(1 Go de RAM, CPU ARM Cortex-A53). Stack choisie pour son efficacité sur du matériel modeste :

- **Python 3.11** + **ccxt** : connexion à des dizaines d'exchanges (Binance, Kraken, Bybit, ...) sans dépendance lourde
- **pandas / numpy** : calcul des indicateurs techniques (via wheels précompilés `piwheels` sur ARM, pas de compilation)
- **SQLite** : persistance des trades/positions/équity sans serveur de base de données
- **Boucle de polling REST** (pas de websocket) : consommation CPU/RAM minimale, adaptée au Pi 3
- **Telegram** (API HTTP directe, sans SDK lourd) pour les alertes
- **systemd** pour tourner en arrière-plan au démarrage du Pi

⚠️ **Le trading de cryptomonnaies comporte un risque de perte en capital.** Teste toujours en mode `paper`
(simulation) et sur le testnet de l'exchange avant de passer en mode `live` avec de l'argent réel.

## Fonctionnalités

- Stratégie EMA rapide/lente + filtre RSI (modulaire, facile d'en ajouter d'autres dans `bot/strategies/`)
- Gestion du risque : sizing en % du capital, stop-loss, take-profit, trailing stop, limite de perte journalière, nombre max de positions ouvertes
- Mode **paper trading** (simulation avec solde virtuel) et mode **live** (ordres réels via ccxt)
- **Backtester** sur données historiques réelles téléchargées depuis l'exchange
- **Coffre de récompenses** : Binance Simple Earn (intérêts sur le solde inactif), conversion des poussières,
  auto-compounding des profits — le coffre recharge automatiquement le capital de trading quand il baisse
- **Wallet on-chain local sécurisé** (BSC) : clé privée chiffrée keystore V3, jamais en clair
- **Dashboard web** léger (Flask + Chart.js) : equity, PnL, positions, trades, coffre, logs — accessible depuis le LAN
- Notifications Telegram (achats, ventes, erreurs, démarrage/arrêt)
- CLI simple : `run`, `backtest`, `status`, `wallet`, `rewards`, `web`
- Logs avec rotation automatique (pas de saturation de la carte SD)

## Structure du projet

```
bot/
  config.py       # chargement config.yaml + .env
  logger.py        # logging avec rotation
  db.py            # persistance SQLite
  exchange.py      # wrapper ccxt (data + ordres)
  indicators.py     # EMA, RSI, ATR
  risk.py          # sizing, stop-loss/take-profit/trailing, limite journalière
  notifier.py       # alertes Telegram
  portfolio.py      # solde virtuel (mode paper)
  rewards.py        # coffre : Simple Earn, dust, compounding des profits
  wallet.py         # wallet on-chain local (keystore V3 chiffré, BSC)
  engine.py         # boucle principale de trading
  backtester.py     # backtest sur historique réel
  strategies/       # stratégies (interface + implémentations)
  web/              # dashboard Flask (app.py, templates/, static/)
config/config.yaml  # configuration du bot
scripts/            # installation RPi + service systemd
tests/              # tests unitaires (indicateurs, risque)
main.py             # CLI
```

## Installation sur le Raspberry Pi 3

1. Copier le projet sur le Pi (`git clone` ou `scp`), par exemple dans `/home/pi/bot`.
2. Lancer le script d'installation, qui utilise l'index [piwheels.org](https://www.piwheels.org) pour éviter de compiler
   pandas/numpy depuis les sources (très long sur Pi 3) :

   ```bash
   cd /home/pi/bot
   chmod +x scripts/install_rpi.sh
   ./scripts/install_rpi.sh
   ```

3. Éditer `.env` (clés API exchange, token/chat Telegram) et `config/config.yaml` (symboles, stratégie, risque).
4. **Toujours commencer en mode `paper` et `sandbox: true`** dans `config.yaml`.

> 💡 Si l'installation des dépendances échoue par manque de mémoire, augmente le swap du Pi
> (`sudo dphys-swapfile swapoff && sudo nano /etc/dphys-swapfile` → `CONF_SWAPSIZE=1024` → `sudo dphys-swapfile setup && sudo dphys-swapfile swapon`).

## Utilisation

```bash
# Backtest sur les 30 derniers jours (aucune connexion API requise, données publiques)
.venv/bin/python main.py backtest --days 30

# Démarrage du bot (paper ou live selon config.yaml)
.venv/bin/python main.py run

# État actuel : positions ouvertes, derniers trades, solde, coffre
.venv/bin/python main.py status
```

## Coffre de récompenses et circuit du capital

Le bot fait travailler le capital en continu :

```
profits de trading ─┬─> coffre (Binance Simple Earn, génère des intérêts)
solde inactif ──────┘          │
                               │ recharge automatique si le solde
                               ▼ de trading passe sous le seuil
                     capital de trading
```

- `profit_skim_pct` : % de chaque profit envoyé au coffre (compounding)
- `min_idle_to_subscribe` / `keep_free` : placement du solde inactif dans Earn
- `min_trading_balance` : seuil de recharge automatique depuis le coffre
- `dust_enabled` : conversion périodique des poussières en BNB (mode live)
- En mode `paper`, le coffre est simulé avec `paper_apr_pct` pour valider le circuit complet

```bash
.venv/bin/python main.py rewards status    # solde du coffre + récompenses cumulées
.venv/bin/python main.py rewards collect   # cycle de collecte immédiat
```

> ⚠️ Le farming automatisé de faucets/airdrops n'est volontairement pas implémenté :
> il repose sur le contournement de captchas et viole les CGU des services (risque de ban).

## Wallet on-chain sécurisé

Wallet EVM local sur le Pi (réseau BSC, frais faibles). La clé privée est chiffrée au format
keystore V3 (scrypt) — jamais stockée en clair. Mot de passe via `WALLET_PASSWORD` dans `.env`
ou saisie interactive.

```bash
.venv/bin/python main.py wallet create                     # génère le wallet chiffré
.venv/bin/python main.py wallet address                    # adresse publique (pour recevoir)
.venv/bin/python main.py wallet balance                    # soldes BNB + USDT on-chain
.venv/bin/python main.py wallet sweep --amount 100         # retire 100 USDT de Binance vers le wallet
.venv/bin/python main.py wallet send --to 0x... --amount 50 # renvoie 50 USDT (ex: dépôt Binance)
```

**Sécurité :**
- Sauvegarde le fichier `data/keystore.json` **et** le mot de passe hors du Pi (sans les deux, fonds perdus)
- `wallet sweep` nécessite le droit *withdraw* sur la clé API — ne l'active que si tu utilises cette commande,
  et restreins les retraits à l'adresse du wallet (whitelist d'adresses sur Binance)
- Prévois un peu de BNB sur le wallet pour payer le gas des envois sortants

## Tourner en continu (systemd)

```bash
sudo cp scripts/trading-bot.service scripts/trading-bot-web.service /etc/systemd/system/
sudo nano /etc/systemd/system/trading-bot.service   # adapter les chemins/utilisateur si besoin
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot trading-bot-web
sudo systemctl status trading-bot
journalctl -u trading-bot -f      # logs en direct
```

## Dashboard web

Interface en lecture seule, conçue pour rester légère sur le Pi (Flask + une page HTML, Chart.js via CDN,
~30 Mo de RAM). Elle lit la base SQLite : elle ne peut ni passer d'ordre ni toucher au wallet.

```bash
.venv/bin/python main.py web                  # http://<ip-du-pi>:8080
.venv/bin/python main.py web --port 9000      # port personnalisé
```

Affiche : solde/equity et variation depuis le départ, PnL 24h et total, taux de réussite, coffre et
récompenses cumulées, adresse du wallet, courbe d'equity, PnL par symbole, positions ouvertes (SL/TP/trailing),
derniers trades, logs en direct, indicateur bot en ligne/hors ligne. Actualisation auto toutes les 30 s.

**Sécurité :** définis `WEB_PASSWORD` dans `.env` pour exiger un mot de passe (HTTP Basic). Sans lui, le dashboard
est ouvert à tout le réseau local. Ne l'expose jamais directement sur Internet : passe par un VPN (WireGuard/Tailscale)
ou un reverse proxy HTTPS.

## Passer en mode réel (live)

1. Valider la stratégie sur plusieurs backtests et plusieurs jours/semaines en mode `paper`.
2. Générer des clés API sur l'exchange avec les droits **trading uniquement** (jamais de retrait).
3. Dans `config/config.yaml` : `trading.mode: live` et `exchange.sandbox: false`.
4. Remplir `.env` avec les vraies clés API.
5. Redémarrer le service et surveiller de près les premiers cycles.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Ajouter une stratégie

Créer un fichier dans `bot/strategies/`, hériter de `Strategy` (dans `base.py`), implémenter `generate_signal(df)`,
puis l'enregistrer dans `bot/strategies/__init__.py` (dict `STRATEGIES`).
