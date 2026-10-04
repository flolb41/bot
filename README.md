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
- **Wallets on-chain locaux sécurisés** (BSC) : BOT opérationnel + VAULT coffre-fort, clés chiffrées keystore V3
- **Cotation PancakeSwap** en lecture seule : écart de prix CEX/DEX par symbole, alerte au-delà d'un seuil
- **Dashboard web** léger (Flask + Chart.js) : equity, PnL, positions, trades, coffre, écarts CEX/DEX, logs
- Notifications Telegram (achats, ventes, erreurs, démarrage/arrêt)
- CLI simple : `run`, `backtest`, `status`, `wallet`, `rewards`, `dex`, `web`
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
  wallet.py         # wallet on-chain (keystore V3 chiffré, BSC)
  treasury.py       # trésorerie 2 wallets : transfert BOT -> VAULT
  dex.py            # cotation PancakeSwap V2 (getAmountsOut via RPC, sans web3.py)
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
.venv/bin/python main.py rewards status --live          # lit le compte Simple Earn réel
.venv/bin/python main.py rewards collect --live         # collecte réelle isolée, sans démarrer le trading
```

La collecte `--live` utilise les clés réelles dédiées `REWARDS_API_KEY` et `REWARDS_API_SECRET` dans `.env`;
elle reste indépendante de `trading.mode`, `exchange.sandbox` et `rewards.enabled`, qui continuent à contrôler
le bot de trading. Donne à ces clés les droits de lecture et Simple Earn requis, mais aucun droit de retrait.
La commande convertit les poussières si activé et place le solde libre excédant `keep_free` dans Simple Earn;
elle ne passe aucun ordre de trading et ne nécessite pas les wallets BSC.

Pour l'automatiser toutes les 6 h sur le Pi (indépendamment du service de trading) :

```bash
sudo cp scripts/trading-rewards.service scripts/trading-rewards.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now trading-rewards.timer
journalctl -u trading-rewards -n 20     # résultat des dernières collectes
```

> ⚠️ Le farming automatisé de faucets/airdrops n'est volontairement pas implémenté :
> il repose sur le contournement de captchas et viole les CGU des services (risque de ban).

## Wallets on-chain sécurisés (BOT / VAULT)

Deux wallets EVM locaux sur le Pi (réseau BSC, frais faibles), clés privées chiffrées au format
keystore V3 (scrypt) — jamais stockées en clair.

| Wallet | Rôle | Mot de passe | Qui le connaît |
|---|---|---|---|
| **BOT** | opérationnel, petite somme de travail | `WALLET_PASSWORD` | le bot (dans `.env`) |
| **VAULT** | coffre-fort, reçoit l'excédent | `VAULT_PASSWORD` | toi uniquement, **pas dans le `.env` du Pi** |

Le bot peut **déposer** sur le VAULT (simple transfert vers son adresse) mais jamais en **retirer** :
si le bot ou une autorisation DeFi est compromis, le VAULT reste isolé.

```bash
.venv/bin/python main.py wallet create                       # crée le wallet BOT
.venv/bin/python main.py wallet --role vault create          # crée le wallet VAULT (mot de passe différent !)
.venv/bin/python main.py wallet balance                      # soldes BNB/USDT des deux wallets
.venv/bin/python main.py wallet sweep --amount 100           # retire 100 USDT de Binance vers le BOT
.venv/bin/python main.py wallet to-vault                     # déplace l'excédent BOT -> VAULT
.venv/bin/python main.py wallet to-vault --amount 50         # ou un montant précis
.venv/bin/python main.py --role vault wallet send --to 0x... --amount 50   # retrait du VAULT (VAULT_PASSWORD demandé)
```

Les wallets BSC BOT/VAULT servent uniquement aux transferts on-chain; ils ne sont pas requis pour Binance Simple Earn.

`wallet.auto_sweep.enabled: true` dans la config = le bot déplace seul l'excédent BOT → VAULT toutes les heures
dès que le BOT dépasse `threshold` USDT (en gardant `keep_on_bot`).

**Sécurité :**
- Sauvegarde les deux fichiers keystore **et** leurs mots de passe hors du Pi (sans les deux, fonds perdus)
- Importe le keystore dans MetaMask si tu veux juste *regarder* le wallet
- `wallet sweep` nécessite le droit *withdraw* sur la clé API — restreins les retraits à l'adresse du BOT (whitelist Binance)
- Prévois un peu de BNB sur le BOT pour payer le gas des transferts

## Cotation PancakeSwap (CEX vs DEX)

À chaque cycle, le bot cote le prix effectif d'un achat de `dex.notional` USDT sur PancakeSwap V2
(`getAmountsOut` via le RPC public BSC, aucune clé, aucun gas) et enregistre l'écart avec le prix Binance.
Affiché dans le dashboard ; alerte log/Telegram si |écart| > `dex.alert_spread_pct`.

```bash
.venv/bin/python main.py dex                                 # symboles de la config
.venv/bin/python main.py dex --symbols BNB/USDT --notional 500
```

En pratique le DEX est ~0,1–0,5 % plus cher (frais LP 0,25 % + slippage) : trader sur PancakeSwap
avec un petit capital n'est pas rentable face à Binance (0,1 %, sans gas). Cette brique sert à le mesurer,
pas à trader.

## Tourner en continu (systemd)

```bash
sudo cp scripts/trading-bot.service scripts/trading-bot-web.service /etc/systemd/system/
sudo nano /etc/systemd/system/trading-bot.service   # adapter les chemins/utilisateur si besoin
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot trading-bot-web
sudo systemctl status trading-bot
journalctl -u trading-bot -f      # logs en direct
```

## Redéployer depuis le PC

```bash
./scripts/deploy.sh                    # florian@192.168.1.158 par défaut
./scripts/deploy.sh pi@192.168.1.50    # autre cible
```

Envoie uniquement les fichiers nécessaires (pas de tests, `.git`, venv, données ni secrets), met à jour
les dépendances et redémarre les services. Nécessite une clé SSH installée sur le Pi (`ssh-copy-id`).

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

Stratégies fournies (backtest 90 j, BTC + ETH, 1h, 10 % du capital par trade) :

| Stratégie | Type | Rdt | Trades | Win | DD max | Profil |
|---|---|---|---|---|---|---|
| `ema_rsi` | suivi de tendance | +0,87 % | 99 | 30 % | 1,08 % | pertes petites fréquentes, gains larges ; perd en range |
| `bollinger_rsi` | retour à la moyenne | +0,46 % | 31 | 81 % | 0,14 % | gagne en range, peu de trades ; préférer `trailing_stop_pct: 0` |

Le 15m est perdant pour `ema_rsi` (frais) et neutre pour `bollinger_rsi`. Comparer avec :

```bash
python scripts/compare_backtests.py --days 90 --strategy ema_rsi,bollinger_rsi --timeframes 1h,4h
python main.py backtest --set strategy.name=bollinger_rsi --set risk.trailing_stop_pct=0
```
