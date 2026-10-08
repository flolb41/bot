# FlashArbitrage — contrat de flashloan atomique (point 8 de la feuille de route)

## Statut actuel : **deploye et operationnel sur Base mainnet** (v2, avec Uniswap V3)

Ce contrat emprunte un actif via un flashloan Aave V3, execute une sequence
de swaps (2 jambes classiques ou cycle triangulaire) sur des routers DEX,
rembourse le pret + la prime, et reverse le profit net. Toute la sequence
est atomique : si le resultat final ne couvre pas le remboursement + le
profit minimum exige, la transaction entiere `revert` — aucune perte de
capital possible au-dela du gas de la tentative.

### Historique des versions

- **v1** (deployee le 8 octobre, PLUS UTILISEE) : jambes `kind=0`
  (fork UniswapV2 classique) et `kind=1` (Aerodrome) uniquement.

  | Champ          | Valeur                                                                 |
  |----------------|-------------------------------------------------------------------------|
  | Adresse        | `0x73beAacEE6CD3Dc0cb4816Dd48cbdCEE45dcc1e8`                            |
  | Tx hash        | `0x9d91d458c69dd2e946c6d44c92ebb5fc7a3d4e04e2b57c435362e068aa1f7642`    |

- **v2** (deployee, ADRESSE ACTIVE — voir `app.trading.routers.FLASH_ARBITRAGE_ADDRESS`) :
  ajoute `kind=2` (Uniswap V3 SwapRouter02, single-hop `exactInputSingle`,
  champ `fee` ajoute a la struct `Leg`). Motivation : la route reellement la
  plus profitable observee en production (uniswap -> aerodrome) ne
  beneficiait jamais du financement flashloan car Uniswap V3 n'etait pas
  supporte par le contrat, la limitant a la taille du solde du wallet
  (~7-8$) au lieu d'un notionnel optimise bien plus grand.

  | Champ          | Valeur                                                                 |
  |----------------|-------------------------------------------------------------------------|
  | Adresse        | `0x6991D2e6cDB057c50978a6111E31fd8f3c524727`                            |
  | Reseau         | Base mainnet (chain_id 8453)                                            |
  | Owner/deployer | `0x032d16Ec1F4382287cBBFbB87Dd8163934A01803` (wallet de trading du bot) |
  | Aave V3 Pool   | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5`                            |
  | Tx hash        | `0xdb08dc75f7ab042fd66cee7251e424899e1dbc275cf3a203da4e5c857be39964`    |
  | Gas utilise    | 1 018 292 (cout reel ≈ 0,0000062 ETH au gas price du moment)            |
  | Explorer       | https://basescan.org/address/0x6991D2e6cDB057c50978a6111E31fd8f3c524727 |

### Validation effectuee

1. **Remix VM** (simulateur EVM dans le navigateur, `test/RemixHarness.sol`
   + `test/Mocks.sol`) : scenario rentable reussi (profit transfere
   correctement), scenario perdant correctement `revert` (aucune perte de
   fonds). Validation confirmee par l'utilisateur pour la v1. La v2 ajoute
   `runV3LegScenario` (meme harnais) pour valider la jambe `kind=2` ; le
   deploiement v2 a ete decide sur la base de la suite de tests Python
   (43/43) et de la compilation propre du contrat + du harnais, sans
   repasser par un clic Remix manuel (choix explicite de l'utilisateur).
2. **Deploiement reel sur Base mainnet**, voir tableaux ci-dessus (v1 et v2).

## Fichiers

- `FlashArbitrage.sol` — le contrat principal.
- `FlashArbitrage.compiled.json` — ABI + bytecode pre-compiles (solc
  0.8.20), commites pour tracabilite. Necessaire car le RPi est en
  architecture aarch64 et les binaires solc precompiles par solcx ne sont
  disponibles qu'en linux-amd64 ; ce fichier est donc genere sur un poste
  x86_64 (`scripts/contracts/compile_flash_arbitrage.py`) puis copie sur
  le Pi au lieu d'y etre recompile.
- `test/Mocks.sol` — mocks (ERC20, Pool Aave, Router DEX) pour tests
  deterministes, **a ne jamais deployer sur testnet/mainnet reel**.
- `test/RemixHarness.sol` — harnais de test one-click pour Remix IDE.
- `deployments/<chain_id>.json` — infos de deploiement (adresse, ABI, tx
  hash) par reseau, ignores par git (contiennent l'ABI complete,
  redondante avec `FlashArbitrage.compiled.json`).

## Adresses Aave V3 Pool connues

| Reseau             | Chain ID | Pool                                        |
|---------------------|----------|----------------------------------------------|
| Base Sepolia (test) | 84532    | `0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27` |
| Base (mainnet)      | 8453     | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5` |

## Re-deploiement / mise a jour du contrat

Si le code du contrat change, il faut re-compiler puis re-deployer une
nouvelle instance (pas de proxy/upgradeabilite — chaque version est un
nouveau contrat a une nouvelle adresse) :

```powershell
# 1. Recompiler (poste x86_64 uniquement, le Pi ne peut pas faire tourner solc)
.\.venv\Scripts\python.exe scripts\contracts\compile_flash_arbitrage.py

# 2. Copier l'artefact + le script de deploiement sur le Pi
scp contracts\FlashArbitrage.compiled.json florian@192.168.1.158:~/bot/contracts/
scp scripts\contracts\deploy_flash_arbitrage_mainnet.py florian@192.168.1.158:~/bot/scripts/contracts/
```

```bash
# 3. Sur le Pi : dry-run d'abord (estimation de gas, aucun envoi)
cd ~/bot && set -a && . ./.env && set +a
.venv/bin/python scripts/contracts/deploy_flash_arbitrage_mainnet.py

# 4. Deploiement reel (demande une phrase de confirmation exacte)
.venv/bin/python scripts/contracts/deploy_flash_arbitrage_mainnet.py --send
```

Le script `deploy_flash_arbitrage_mainnet.py` reutilise le wallet de
trading existant du bot via `app.wallet.signer.load_signer()` (meme
keystore chiffre que pour les transactions de trading normales) — aucune
cle privee en clair, aucun wallet separe a gerer.

Un script generique plus ancien, `scripts/contracts/deploy_flash_arbitrage.py`
(cle privee via variable d'environnement, utilisable sur testnet avec un
wallet jetable), reste disponible pour des tests sur un autre reseau.

## Integration avec le bot

L'integration cote Python est faite (`app/trading/flashloan.py`) : pour un
signal juge rentable mais dont la taille optimale depasse le solde
disponible du wallet, le scheduler route vers
`execute_pair_signal_via_flashloan`/`execute_triangular_signal_via_flashloan`
au lieu de l'execution classique de `executor.py` — controle par
`trading.flashloan_enabled` dans `config.yaml` (desactive par defaut).
Apres le redeploiement v2 (Uniswap V3), penser a verifier que
`app.trading.routers.FLASH_ARBITRAGE_ADDRESS` pointe bien vers la nouvelle
adresse avant de reactiver `flashloan_enabled` en production.
