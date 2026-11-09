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
.venv\Scripts\pip install -r requirements-dev.txt   # pytest (même suite que la CI GitHub)
copy .env.example .env        # puis renseigner TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
python -m pytest -q            # 190+ tests, aucune transaction ni accès wallet
python main.py scan-arbitrage  # un scan manuel, SIMULATION uniquement
python main.py trading-status  # explique les garde-fous live, sans transaction
python main.py status          # dashboard texte dans le terminal
python main.py run             # scheduler + Telegram + dashboard web (service complet)
```

### Dashboard trading uniquement (Raspberry Pi)

Le dashboard peut être lancé sans scheduler ni scans, ce qui laisse le bot de
trading désarmé tout en gardant l’interface web disponible :

```bash
mkdir -p ~/.config/systemd/user
cp scripts/trading-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trading-dashboard.service
```

Avec le gestionnaire utilisateur persistant activé, l’interface est disponible
sur `http://<IP-DU-PI>:8000` après redémarrage. Le service principal
`crypto-reward-hunter` est indépendant et n’est pas nécessaire pour afficher
le dashboard.

Pour utiliser le scheduler et le dashboard ensemble sous systemd utilisateur
(sans `sudo`), installe plutôt le service complet ci-dessous ; il remplace le
service dashboard seul afin d’éviter un conflit sur le port 8000 :

```bash
systemctl --user disable --now trading-dashboard.service
cp scripts/trading-bot-user.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trading-bot-user.service
```

Le service complet `trading-bot-user.service` est l’unique service à activer
pour ce projet. Si l’ancienne installation système est présente, désactive-la
avant de lancer le service utilisateur afin d’éviter deux schedulers et deux
connexions Telegram :

```bash
sudo systemctl disable --now crypto-reward-hunter.service
```

Pour que le service utilisateur démarre après un redémarrage sans session SSH,
active la persistance systemd utilisateur une seule fois :

```bash
sudo loginctl enable-linger "$USER"
```

Les indicateurs « rentables » ne comptent que les routes et devises de départ
prises en charge par l’exécution réelle. Le dashboard distingue le P&L
hypothétique du P&L réalisé : ce dernier utilise le delta on-chain (ou
l’événement de profit flashloan) diminué des frais de gas mesurés dans les
reçus. Il n’est pas rétrocalculé pour les anciennes transactions ; une mesure
USD reste indisponible si le prix ETH ou celui du token de cotation manque.

### Pilotage du dashboard

Le dashboard est organisé en onglets (vue d’ensemble, trading, recherche paper,
réglages). Il suit la fraîcheur du dernier scan, l’uptime du processus, la
taille de la base et les positions résiduelles. La santé du scan est renseignée
par le scheduler après chaque cycle terminé.

Les réglages live sont protégés par `DASHBOARD_CONTROL_TOKEN` et une vérification
d’origine navigateur. Le script `scripts/deploy.sh` crée ce jeton sur le Pi s’il
n’existe pas. Pour l’utiliser dans l’interface, récupère-le directement en SSH
sur le Pi et ne le partage pas. Le dashboard est servi en HTTP sur le LAN :
ne redirige pas le port 8000 vers Internet et utilise uniquement un réseau de
confiance. Pour saisir le jeton dans le navigateur, affiche-le directement dans
ton terminal SSH (`grep '^DASHBOARD_CONTROL_TOKEN=' ~/bot/.env`) ; ne le copie pas
dans un message ou un ticket.

Les curseurs pilotent le profit net minimum classique (0,01–5 $), le seuil
relatif (0,5–1 %), le seuil flashloan (0,25–10 $), la liquidité minimale des
pools (10 k$–1 M$) et l’activation des flashloans. Les réglages persistés en
base prennent effet au prochain scan ; ils ne modifient ni la taille maximale
des trades, ni les limites de risque, ni les garde-fous d’exécution. Le bouton
« Arrêt d’urgence » active le killswitch ; la reprise se fait par `/start` sur
Telegram. Une transaction déjà envoyée ne peut pas être annulée par ce bouton.

### Recherche et trading directionnel

Le scan partagé alimente deux expérimentations paper sans émission d'ordres :
`liquid_momentum` (actifs plus liquides) et `microcap_momentum` (FDV au plus
50 M$, liquidité de pool au moins 25 k$ et volume 24 h au moins 100 k$).
La sélection se limite aux tokens déjà présents dans les pools découvertes,
aux quotes USDC/WETH et aux DEX exécutables ; ce n'est pas un scanner exhaustif
des microcaps Base.

Les paramètres initiaux (signal +1 % sur 15 minutes, take-profit +2 %, stop
-1,5 %, durée maximale 60 minutes et mise simulée de 5 $) sont des hypothèses
expérimentales, non des paramètres optimisés. Le P&L paper déduit les frais DEX,
un tampon de slippage et le gas estimé aller-retour. Les données absentes ou les
pools qui disparaissent limitent la qualité des observations ; les résultats ne
garantissent ni exécution réelle ni rentabilité. Comparer les résultats nets
sur une période suffisante avant toute décision de changement de stratégie.

Une exécution directionnelle live séparée peut utiliser ces signaux, sans
passer par l'exécuteur d'arbitrage. Elle ne traite que les paires cotées en
USDC et impose une seule position ouverte, une mise maximale de 2 $, une
liquidité on-chain au moins égale à la moitié du seuil de la stratégie, une
revalidation du prix avant l'ordre et un slippage maximal de 0,5 %. Les sorties
reprennent les règles paper (+2 %, -1,5 % ou 60 minutes). Le P&L journalier
agrège les trades live mesurés ; une mesure manquante bloque les nouvelles
entrées, et le seuil de perte de 1 $ suspend les entrées suivantes. Ces
garde-fous ne garantissent pas une perte maximale absolue : un mouvement rapide,
un revert ou une transaction de clôture impossible peut dépasser le stop ou
retarder la sortie. L'activation reste conditionnée à l'ensemble des contrôles
live habituels (confirmation explicite, wallet, simulations et killswitch).

**Désactivée le 09/11/2026** (`directional_live.enabled: false`) : bilan
mesuré de 16 trades live à −0,35 $ (dont 0,09 $ de gas), 579 trades paper à
−28,6 $ sur les deux variantes, et un backtest perps de la même famille
momentum sans avantage hors échantillon. La désactivation ne bloque que les
nouvelles entrées : une position encore ouverte reste suivie et fermée par ses
règles. Les expérimentations paper continuent d'alimenter le dashboard.

Les seuils live configurés sont de **0,01 $** pour les voies classiques et
**0,25 $** pour les flashloans, avec un rendement estimé minimum de **0,5 %**
du montant engagé/emprunté. Ces planchers filtrent les petites marges, mais ne
garantissent ni la qualité des estimations ni un profit réalisé : la simulation
on-chain reste obligatoire et les résultats doivent être suivis dans le P&L
réalisé.

Une exception de liquidité, limitée aux paires **BRETT/WETH** et
**BRETT/USDC**, autorise les pools à partir de 25 $ de liquidité DexScreener,
mais exige aussi au moins 25 $ de profondeur quote mesurée on-chain sur chaque
pool réellement routée. La taille par trade est plafonnée à 2 $, puis réduite
si le solde, la liquidité (garde-fou général de 2 %) ou le slippage estimé
l'impose. Le slippage maximal configuré pour ces paires est de 15 % ; c'est
une tolérance élevée, susceptible d'entraîner une perte importante sur les
swaps classiques. Les flashloans sont autorisés pour BRETT, mais restent
bornés par le même plafond de taille, la règle générale de liquidité, le
profit net minimal et la simulation on-chain. Le contrat flashloan vérifie le
profit net agrégé et revert si le minimum n'est pas atteint (le gas reste
dépensé), mais n'impose pas actuellement de limite on-chain de slippage pour
chaque jambe séparément. Le plafond de 15 % y sert au dimensionnement estimé,
pas à une garantie stricte par swap. Cette exception ne s'applique pas aux
autres paires ou aux pools dont la profondeur réelle ne peut pas être vérifiée.

Les cycles triangulaires cross-DEX sont également préfiltrés en flashloan.
Leur exécution est activée dans la configuration du Pi après validation
explicite, et exige aussi les garde-fous live habituels ainsi que
`trading.flashloan_enabled`. Avant l'envoi, l'exécuteur exige un prix USD de
chaque token intermédiaire, une profondeur on-chain vérifiable pour chacune des
trois routes, plafonne le notionnel selon la route la moins profonde, ajoute
une marge de sécurité au gas estimé et simule le cycle atomique complet via le
contrat flashloan existant. Le contrat impose ensuite un profit minimum
agrégé ; une simulation réussie n'empêche pas un revert ultérieur ni les frais
de gas correspondants. Le minimum de profit configuré est faible et ne garantit
pas un profit réalisé. Le dashboard distingue le spread théorique, le
préfiltre estimatif et l'état d'exécution mesuré.

## Déploiement sur Raspberry Pi 3

```bash
./scripts/deploy.sh florian@192.168.1.158   # sauvegarde puis redémarre trading-bot-user
```

Le script sauvegarde le code, la configuration, le manifeste des dépendances
et la base SQLite, installe les dépendances déclarées (avec piwheels), puis
vérifie la compilation et la configuration avant de redémarrer le service. Par
défaut, il exige que `trading.cross_dex_flashloan_enabled` soit `false`. Pour
déployer explicitement une configuration qui autorise les cycles cross-DEX
live, ajoute `--enable-cross-dex-live` à la commande ; cette option ne contourne
pas les autres garde-fous de trading.

La maintenance SQLite archive avant purge les signaux de scan, opportunités
filtrées, observations paper et notifications lues de plus de 90 jours dans
`data/reward_hunter_archive.db`. Les exécutions live, positions/trades paper ou
live et notifications non lues ne sont pas purgés. La valeur se règle via
`database.retention_days`; `0` désactive la maintenance automatique.

Ou installation initiale directement sur le Pi :

```bash
./scripts/install_rpi.sh
mkdir -p ~/.config/systemd/user
cp scripts/trading-bot-user.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trading-bot-user.service
sudo loginctl enable-linger "$USER"
```

N’active pas simultanément `crypto-reward-hunter.service` et
`trading-bot-user.service`. Si l’ancien service système est déjà installé,
arrête-le avec `sudo systemctl disable --now crypto-reward-hunter.service`.

Pour activer le trading réel, importe d'abord ton wallet de trading **en SSH
direct sur le Pi** (jamais à distance) :

```bash
.venv/bin/python main.py import-trading-wallet
```

Scripts de maintenance (à planifier via cron) :
- `scripts/watchdog.sh` : redémarre le service si le dashboard ne répond plus
  (sonde `/api/health`, et s'efface pendant une mise à jour automatique).
- `scripts/backup.sh` : sauvegarde horodatée de `data/reward_hunter.db` (rétention 30 j).
  Le script utilise l’API de sauvegarde SQLite et vérifie l’intégrité de la copie.

## Mise à jour automatique du Pi

La branche `main` de GitHub est la source de vérité : le Pi est un clone git
et `bot-self-update.timer` (systemd utilisateur) exécute
`scripts/self_update.sh` toutes les 2 minutes.

1. `git fetch` ; rien à faire si `HEAD` = `origin/main`.
2. **Gate CI** : le commit n'est appliqué que si le check GitHub Actions
   `tests` (`.github/workflows/ci.yml`) est terminé avec succès
   (`scripts/ci_status.py`). Tests en échec → commit ignoré + notification ;
   CI toujours absente après 30 min → notification.
3. Sauvegarde SQLite + `config.yaml` dans `deploy-backups/auto-<date>/`,
   killswitch activé le temps de l'opération, arrêt du service.
4. `git reset --hard origin/main`, `pip install` seulement si
   `requirements.txt` a changé, `compileall`, `pip check`, import des modules
   principaux, ré-enregistrement des réglages runtime.
5. Redémarrage puis attente de `/api/health` (90 s max). En cas d'échec :
   **rollback** sur le commit précédent, commit fautif mémorisé
   (`.self-update/rejected`) pour ne pas boucler, notification `warning` ;
   si le rollback échoue aussi, le killswitch reste actif (notification
   `alert`).
6. Killswitch restauré à sa valeur d'avant, notification `info` dans le
   dashboard. Journal : `logs/self_update.log`.

Garde-fous : refus si des fichiers suivis ont été modifiés directement sur
le Pi (`git status` doit être propre) ; un commit qui fait passer
`trading.cross_dex_flashloan_enabled` à `true` exige un marqueur
`~/bot/.allow-cross-dex-live` posé à la main (équivalent du
`--enable-cross-dex-live` de `deploy.sh`). `deploy.sh` refuse désormais de
pousser une archive sur un Pi géré par git.

Installation (une fois, sur le Pi, sans sudo) :

```bash
bash ~/bot/scripts/install_self_update.sh          # clone git + timer
sudo bash ~/bot/scripts/install_os_updates.sh      # mises à jour de sécurité Debian, sans reboot auto
```

Variables optionnelles pour `self_update.sh` : `SELF_UPDATE_BRANCH` (défaut
`main`), `SELF_UPDATE_REQUIRED_CHECK` (défaut `tests`, vide pour désactiver la
gate CI), `GITHUB_TOKEN` (relève la limite d'API, inutile pour un dépôt
public). Si `perps-paper.service` est actif, il est arrêté puis redémarré avec
la mise à jour, et `.venv-perps` est réinstallé lorsque
`requirements-perps.txt` change.

## Perps sur Base — phase papier

`app/perps/` simule deux stratégies de perpétuels sur **Veranta** (ex-Avantis,
perps sur Base) **sans aucun ordre ni clé** : il lit le marché public (prix,
spread dynamique, frais d'ouverture/clôture, funding par côté et par heure,
rollover, intérêt ouvert, position minimale) et comptabilise chaque position
papier avec ces coûts réels. Synthetix V3 sur Base a été écarté le 08/11/2026
(`maxOpenInterest = 0` sur ETH/BTC/SOL : marchés en extinction).

- **`funding_carry`** : short perp + long spot modélisé (frais DEX, glissement,
  gas) quand le rendement **net** du short (funding encaissé − rollover) tient
  ≥ `entry_min_apr_pct` en médiane sur `entry_persistence_hours` et que le gain
  attendu sur `max_hold_hours` couvre `entry_min_edge_ratio` fois le coût d'une
  rotation ; sortie quand le funding net redevient un coût pendant
  `exit_confirm_hours` d'affilée ou après `max_hold_hours`. Les alts Veranta
  paient 5–15 %/an de rollover : seuls ETH/BTC (rollover nul) portent vraiment.
  **Vérifié le 09/11/2026** sur ~50 positions réelles d'autres traders (lecture
  publique `account.positions(trader)`) : le funding est bien crédité aux
  shorts ETH/BTC (`unrealised_funding_fee` négatif = crédit, égal à
  taux × notionnel × heures ; rollover 0), mais le taux varie — sur plusieurs
  jours l'accumulé ne fait que 50–90 % du taux instantané. Compter ~7–9 %/an
  net, pas 10,5–12,8 ; le dashboard affiche la moyenne glissante à côté de
  l'instantané pour cette raison.
- **`trend`** : tendance sur `lookback_minutes` confirmée par la dernière heure,
  levier faible, stop/objectif/durée max, cooldown de ré-entrée, plafond de
  positions et de perte journalière ; liquidation simulée à 85 % du collatéral
  (seuil Veranta). **Désactivée le 09/11/2026** après backtest (voir plus
  bas) : aucun avantage mesurable, les frais mangent un signal qui vaut pile
  ou face. Les positions papier encore ouvertes se ferment seules (stop,
  objectif ou durée max) ; aucune nouvelle entrée.
- **`funding_scan`** : toutes les 30 min, un seul appel `snapshot` lit le
  funding, le rollover et l'intérêt ouvert des ~200 paires Veranta ; le
  dashboard classe les paires par rendement net du meilleur côté (instantané
  et moyenne sur `average_window_days`) et une alerte (événement, 1 par
  paire/côté et par 12 h) signale les extrêmes ≥ `alert_min_net_apr_pct` sur
  les marchés vivants (OI total ≥ `alert_min_oi_usd`). Les paires sans jambe
  spot sur Base (`spot_hedgeable`) sont marquées « biais directionnel
  seulement ».
- **`treasury`** : toutes les heures, observe (papier) ce que le capital oisif
  pourrait toucher : taux de dépôt USDC d'Aave V3 Base lu on-chain
  (`getReserveData`, `eth_call` brut), rendements du vault LP Veranta via le
  SDK, solde USDC du wallet (`wallet_address`, lecture seule) et manque à
  gagner cumulé. Aucun dépôt réel : c'est la référence que toute stratégie
  doit battre.

Service **séparé** du bot d'arbitrage : process `perps_main.py`, venv
`.venv-perps` (le SDK Veranta impose des versions d'`eth-account`/`websockets`
incompatibles avec web3), base `data/perps_paper.db`, journal
`logs/perps.log`, dashboard `http://<IP-DU-PI>:8001` (`/api/perps`,
`/api/health`). Configuration dans la section `perps:` de `config/config.yaml`.

```bash
bash ~/bot/scripts/install_perps_paper.sh   # une fois, sans sudo
.venv-perps/bin/python perps_main.py status # résumé texte (marchés, stratégies, funding, trésorerie)
```

### Backtest de la stratégie tendance

Le papier produit ~3 trades par semaine : il faudrait des mois pour juger.
`perps_main.py backtest` rejoue la stratégie sur les bougies du feed Veranta
(1 an en 1 h, 2 ans en 4 h, 4 ans en journalier ; cache JSON dans
`data/perps_candles/`) avec le **même modèle de coûts et les mêmes règles**
que le moteur papier — un test de parité le garantit en mode `--close-only`.
Par défaut, stop et objectif sont évalués sur les plus haut/plus bas de la
bougie (stop d'abord si les deux sont touchés, gap exécuté à l'ouverture).

```bash
.venv-perps/bin/python perps_main.py backtest --symbol ETH/USD --resolution 60 --days 365
.venv-perps/bin/python perps_main.py backtest --resolution 240 --days 730 --grid     # grille + walk-forward 70/30
.venv-perps/bin/python perps_main.py backtest --symbol BTC/USD --live-costs --json    # coûts actuels du marché
```

`--grid` optimise sur les premiers 70 % de l'historique et juge les meilleures
combinaisons sur les 30 % restants : seule la ligne « test » compte ; si elle
est négative alors que l'entraînement était positif, la grille a sur-appris.
Limites : un marché par simulation, coûts constants sur la période, pas de
glissement au-delà du spread coté.

Résultats du 09/11/2026 (coûts réels du jour, 100 $ de notionnel à 2x) :

| Marché | 1 h × 1 an | 4 h × 2 ans | Journalier × 4 ans | Walk-forward (meilleure combinaison → test) |
|---|---|---|---|---|
| ETH/USD | PF 0,92, −35 $ | PF 0,96, −28 $ | PF 0,89, −56 $ | +34 $ → −24 $ |
| BTC/USD | PF 0,82, −53 $ | PF 0,98, −7 $ | PF 0,82, −73 $ | +30 $ → +13 $ (seul cas positif, 28 trades) |
| SOL/USD | PF 0,85, −80 $ | PF 0,92, −66 $ | PF 0,63, −260 $ | +40 $ → −10 $ |

Aucune résolution, aucun marché, aucune des 192–216 combinaisons par marché ne
donne un avantage robuste : la stratégie tendance est désactivée. La
rentabilité du module repose sur le carry ETH/BTC (~7–9 %/an net vérifiés) et
sur la trésorerie (4,9 % Aave sans risque de marché, 25 % en vault LP avec
risque de drawdown).

### Prérequis du mode live (rien n'est câblé)

`app/perps/broker.py` prépare le passage en réel **sans l'activer** : le moteur
papier n'appelle aucun broker. Avant toute décision, trois verrous
indépendants doivent être verts (`perps_main.py live-status`, lecture seule) :

1. `perps.mode: live` dans la config **et** des plafonds non nuls dans
   `perps.live` (`max_collateral_usd`, `max_total_collateral_usd`,
   `max_leverage`) cohérents avec les stratégies ;
2. la phrase exacte `JE COMPRENDS QUE LES PERPS LIVE PEUVENT PERDRE TOUT LE
   COLLATERAL` dans la variable `PERPS_LIVE_CONFIRMED` (`.env`) ;
3. une **clé déléguée** (`VERANTA_PRIVATE_KEY`) distincte du wallet trader
   (`VERANTA_TRADER_ADDRESS`) : créer un wallet dédié qui ne détient que le
   collatéral qu'on accepte de perdre, l'enregistrer comme délégué sur
   l'interface Veranta (ou `account.register_delegate()`), et ne stocker sur le
   Pi que la clé déléguée — elle peut signer des ordres, pas retirer les fonds.
   Le wallet MetaMask principal ne doit jamais servir de trader.

`validate_order` refuse avant envoi tout ce que le venue refuserait après
avoir fait payer gas et spread (notionnel < 100 USDC, levier, plafonds, stop
du mauvais côté ou au-delà du prix de liquidation, glissement > 5 %).
`VerantaBroker` transmet TP/SL au venue à l'ouverture : ils tiennent même si
le Pi s'éteint, contrairement aux stops du papier évalués au tick.

Hypothèses de modélisation : le spread joue contre le trader à l'ouverture et
à la clôture ; la jambe spot du carry est exécutée au prix d'index majoré de
`spot_cost_pct` ; les stops s'exécutent au prix observé au tick (5 min), sans
ordre déclencheur côté venue. Les résultats papier ne préjugent pas d'une
exécution live, qui n'est pas implémentée.

## Analyse : persistance des écarts

`scripts/spread_persistence.py` (lecture seule) mesure, sur les N derniers
jours, combien de temps chaque écart ≥ seuil persiste d'un scan à l'autre et
croise avec les tentatives live (écart détecté vs écart revérifié on-chain au
moment d'agir, P&L réalisé). Un écart réel capté par un bot lent n'apparaît
que dans un scan ; un écart présent des heures à valeur figée est structurel
(pool quasi vide, prix DexScreener périmé, frais non couverts).

```bash
.venv/bin/python scripts/spread_persistence.py --days 30 --threshold 0.7 [--json rapport.json]
```

Résultat du 08/11/2026 (30 j, 135 017 signaux) : les 12 routes les plus
fréquentes ≥ 0,7 % étaient toutes structurelles (jusqu'à 14 h d'affilée, CV de
l'écart ≈ 0), les signaux « rentables » survivaient à 100 % au scan suivant,
et à l'exécution l'écart réel était systématiquement sous les frais. La
latence n'était donc pas le facteur limitant : les écarts affichés n'existaient
pas on-chain (cf. le correctif ABI PancakeSwap V3 du même jour).

## Trésorerie : USDC oisif sur Aave V3

Au niveau de funding observé le 09/11/2026 (~8 %/an net réaliste), un carry
ETH à 1x rapporte ~3,8 %/an sur le capital engagé (100 $ de spot + 100 USDC
de collatéral, frais de rotation déduits) alors que les mêmes USDC sur Aave V3
touchent ~4,9 %/an sans liquidation, sans venue et sans frais. Le carry ne
redevient supérieur qu'au-dessus de ~15 %/an de funding — le seuil d'alerte
du scan. En attendant, l'USDC qui dort doit au moins toucher Aave.

```bash
.venv/bin/python main.py treasury status                               # soldes liquide/déposé, taux courant
.venv/bin/python main.py treasury deposit                              # aperçu : tout sauf 2 USDC de flottant
.venv/bin/python main.py treasury deposit --amount 8 --execute         # dépôt réel
.venv/bin/python main.py treasury withdraw --execute                   # retrait total (intérêts compris)
```

Mêmes garde-fous que les swaps (`app/trading/treasury_ops.py`) : trading live
requis, killswitch, whitelist fixe en code (le Pool Aave est la seule adresse
ajoutée, dans `routers.YIELD_CONTRACT_ADDRESSES`), approbation ERC-20 au
montant exact, simulation avant envoi, et `onBehalfOf`/`to` toujours égaux au
wallet de trading : les fonds ne peuvent aller nulle part ailleurs. Le
dashboard perps (`:8001`) affiche USDC liquide et déposé (aUSDC, intérêts
inclus) ; le manque à gagner cumulé ne porte que sur la part liquide. Risque
résiduel : le contrat Aave V3 lui-même et le peg de l'USDC.

## Liquidation manuelle d'inventaire

```bash
.venv/bin/python main.py liquidate-inventory 0x<token> [0x<token2>]   # aperçu (dry-run)
.venv/bin/python main.py liquidate-inventory 0x<token> --execute       # vente réelle en USDC
```

Revend tout le solde d'un token hérité d'un round-trip interrompu sur le DEX
exécutable au meilleur prix on-chain, avec les garde-fous de la récupération
automatique (profondeur de pool ≥ 20× la taille, `amountOutMinimum` net des
frais, routeur whitelisté, killswitch) ; la vente n'est pas décomptée du
plafond quotidien de transactions.

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
