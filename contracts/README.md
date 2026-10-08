# FlashArbitrage — contrat de flashloan atomique (point 8 de la feuille de route)

## Statut actuel : **deploye et operationnel sur Base mainnet**

Ce contrat emprunte un actif via un flashloan Aave V3, execute une sequence
de swaps (2 jambes classiques ou cycle triangulaire) sur des routers DEX,
rembourse le pret + la prime, et reverse le profit net. Toute la sequence
est atomique : si le resultat final ne couvre pas le remboursement + le
profit minimum exige, la transaction entiere `revert` — aucune perte de
capital possible au-dela du gas de la tentative.

### Validation effectuee

1. **Remix VM** (simulateur EVM dans le navigateur, `test/RemixHarness.sol`
   + `test/Mocks.sol`) : scenario rentable reussi (profit transfere
   correctement), scenario perdant correctement `revert` (aucune perte de
   fonds). Validation confirmee par l'utilisateur.
2. **Deploiement reel sur Base mainnet**, le 8 octobre, depuis le wallet de
   trading de production du bot (devient automatiquement `owner` du
   contrat, voir `constructor`) :

   | Champ          | Valeur                                                                 |
   |----------------|-------------------------------------------------------------------------|
   | Adresse        | `0x73beAacEE6CD3Dc0cb4816Dd48cbdCEE45dcc1e8`                            |
   | Reseau         | Base mainnet (chain_id 8453)                                            |
   | Owner/deployer | `0x032d16Ec1F4382287cBBFbB87Dd8163934A01803` (wallet de trading du bot) |
   | Aave V3 Pool   | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5`                            |
   | Tx hash        | `0x9d91d458c69dd2e946c6d44c92ebb5fc7a3d4e04e2b57c435362e068aa1f7642`    |
   | Gas utilise    | 948 667 (cout reel ≈ 0,0000057 ETH au gas price du moment)              |
   | Explorer       | https://basescan.org/address/0x73beAacEE6CD3Dc0cb4816Dd48cbdCEE45dcc1e8 |

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

## Integration future avec le bot (non faite)

Le contrat est deploye mais **le bot Python ne l'utilise pas encore**.
L'integration cote Python (`app/trading/executor.py`) consistera a ajouter
un mode d'execution "flashloan" qui, pour un signal juge rentable par
`arbitrage.py` / `triangular.py` mais dont la taille depasse le solde
disponible du wallet, encode la sequence de jambes en `Leg[]` et appelle
`startArbitrage` sur `0x73beAacEE6CD3Dc0cb4816Dd48cbdCEE45dcc1e8` au lieu
d'executer les swaps directement depuis le wallet. Ce travail n'a pas
encore commence.
