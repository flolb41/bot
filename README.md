# Crypto Reward Hunter

Système de veille et de suivi **0 € de capital** pour chasser les airdrops, testnets,
quêtes et campagnes de récompenses crypto. Conçu pour tourner en continu sur un
Raspberry Pi 3, avec notifications Telegram et dashboard web.

Ce projet remplace intégralement l'ancien bot de trading : il n'exécute **aucune
transaction automatique**, ne trade pas, et ne touche jamais aux clés privées / seed
phrases. Toutes les décisions à risque (dépôt, signature, interaction on-chain)
restent **manuelles et validées par l'utilisateur**. Le cahier des charges complet
est dans `TODO.md` (fourni par l'utilisateur) ; ce code doit y rester conforme.

## Principes de sécurité (non négociables)

- Aucune seed phrase ni clé privée n'est jamais stockée, demandée ou manipulée.
- Les wallets ne sont consultés qu'en **lecture seule** (soldes, allowances, reçus de
  transaction) via RPC publics.
- Toute action impliquant un dépôt, une signature ou un engagement financier doit être
  validée manuellement par l'utilisateur ; le bot ne fait que détecter et notifier.
- Un killswitch (`/stop` sur Telegram) coupe instantanément le scan et les
  notifications.
- Un détecteur de red flags (`app/trackers/eligibility.py`) signale les formulations
  suspectes (demande de seed, "garantie 100%", etc.).

## Architecture

```
app/
  config.py          # chargement YAML + substitution des variables d'environnement
  logger.py           # logging fichier + console
  database.py         # couche SQLite (projects, tasks, wallets, rewards, transactions, ...)
  models.py           # dataclasses et enums métier
  scoring.py          # calcul du score de priorité (0-100, cf. TODO section 7)
  killswitch.py        # flag d'arrêt d'urgence (table system_state)
  scheduler.py         # orchestration APScheduler (scan, deadlines, digest quotidien)
  dashboard.py         # dashboard texte + API FastAPI
  projects/            # un module par projet suivi (10 projets prioritaires, cf. TODO section 1)
  trackers/            # points, deadlines, rewards, eligibility (red flags, coût 0)
  notifications/       # bot Telegram (commandes /start /projects /top /today ...)
  wallet/              # lecture seule : soldes, allowances, reçus de transaction
main.py                 # CLI (seed / scan / run / telegram / dashboard / status)
config/config.yaml      # configuration (intervalles, seuils de risque, répartition rewards)
.env.example            # variables d'environnement (token Telegram, RPC, adresses publiques)
scripts/                # déploiement RPi3, systemd, watchdog, sauvegarde SQLite
```

## Les 10 projets suivis en priorité + 1 bonus

Push Chain, Canopy, Analog, Kryvora Network, vibe/vibe, Polyester, Flop (FLOPAI),
Orbinum, Asentum, IRIS Credit (ordre de priorité défini par `TODO.md` section 1),
plus **Binance Learn & Earn** (section 2 du TODO) ajouté en tant que simple
surveillance du site public — aucune clé API Binance n'est utilisée, aucun compte
n'est connecté : le bot se contente de détecter un changement sur la page des
campagnes et de rappeler de faire les quiz/cours manuellement.

> ⚠️ Les URLs officielles de chaque projet ont été renseignées à partir de recherches
> web et **ne sont pas garanties à 100 %** (certains projets, comme `vibe/vibe` ou
> `Flop/FLOPAI`, n'ont pas de source officielle confirmée au moment de l'écriture).
> Chaque module dans `app/projects/` documente ce niveau de confiance dans son champ
> `notes`. **Vérifie et corrige ces URLs via les canaux officiels (Twitter/X, Discord)
> avant de t'y fier pour une action réelle.**

## Scoring

Chaque projet reçoit un score 0-100 calculé par `app/scoring.py` à partir de bonus et
malus définis dans `TODO.md` section 7 :

| Bonus | Points | Malus | Points |
|---|---|---|---|
| Fort potentiel de reward | +25 | Demande un dépôt | -30 |
| Vraiment gratuit | +20 | Demande du KYC | -20 |
| Officiel et actif | +15 | Frais élevés | -20 |
| Fenêtre courte | +15 | Contrat non vérifié | -30 |
| Testnet précoce | +10 | Demande la seed phrase | -50 |
| Faible concurrence | +10 | Comportement suspect | -20 |
| Automatisable | +5 | | |

## Installation locale

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # puis renseigner TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
python main.py seed      # initialise les 10 projets en base
python main.py scan      # un cycle de scan unique
python main.py status    # dashboard texte dans le terminal
python main.py run       # scheduler + Telegram + dashboard web (service complet)
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
| `/projects` | Liste des projets suivis et leur statut |
| `/top` | Top projets par score de priorité |
| `/today` | Tâches à faire aujourd'hui |
| `/deadlines` | Échéances à venir (fenêtre 48h) |
| `/rewards` | Récompenses reçues / en attente |
| `/balances` | Soldes des wallets (lecture seule) |
| `/status` | Dashboard texte résumé |
| `/scan` | Déclenche un scan manuel immédiat |
| `/stop` | Coupe le scan automatique (killswitch) |

## Tests

```powershell
.venv\Scripts\python -m pytest tests -q
```

Couverture : scoring (`test_scoring.py`), base de données (`test_database.py`),
détection de red flags / éligibilité 0 coût (`test_eligibility.py`), registre des
projets (`test_projects.py`).

## Répartition des récompenses (TODO section 11)

Par défaut (configurable dans `config/config.yaml`) : 50 % réinvesti dans le farming,
25 % conservé en réserve, 25 % retiré. Ceci reste indicatif : tout retrait/dépôt réel
est une décision manuelle de l'utilisateur.
