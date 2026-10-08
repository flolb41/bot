# FlashArbitrage — contrat de flashloan atomique (point 8 de la feuille de route)

## Statut actuel : **code complet et compile, PAS encore execute sur une chaine**

Ce contrat emprunte un actif via un flashloan Aave V3, execute une sequence
de swaps (2 jambes classiques ou cycle triangulaire) sur des routers DEX,
rembourse le pret + la prime, et reverse le profit net. Toute la sequence
est atomique : si le resultat final ne couvre pas le remboursement + le
profit minimum exige, la transaction entiere `revert` — aucune perte de
capital possible au-dela du gas de la tentative.

### Pourquoi "pas encore execute" ?

- Les tests locaux sur une vraie EVM (via Anvil/Foundry, binaires Windows
  deja telecharges dans `.foundry/`) sont bloques dans cet environnement
  sandbox : impossible d'ouvrir un port d'ecoute local pour `anvil.exe`
  (confirme : un simple `python -m http.server` fonctionne, mais pas
  `anvil.exe` — restriction specifique au process, cause exacte non
  identifiee, possiblement lie au pare-feu/EDR sur ce binaire precis).
- Le deploiement reel sur le testnet Base Sepolia necessite un wallet
  dedie approvisionne en ETH de test ; ce financement n'a pas ete fait
  (decision explicite de l'utilisateur de ne pas alimenter le wallet pour
  le moment).

**Consequence : ce contrat n'a ete valide que par compilation (solcx,
`solc 0.8.20`, zero erreur) et relecture manuelle du code. Il n'a PAS ete
teste par execution reelle (ni mock, ni testnet, ni mainnet).**

### Avant tout deploiement avec de vrais fonds (mainnet)

Ne JAMAIS deployer ce contrat sur mainnet avec des fonds reels sans une
validation d'execution prealable. Options recommandees, par ordre de
simplicite :

1. **Remix IDE** (https://remix.ethereum.org) — copier `FlashArbitrage.sol`
   et `test/Mocks.sol`, utiliser la "Remix VM" (simulateur EVM dans le
   navigateur, zero installation) pour deployer les mocks + le contrat et
   rejouer un scenario rentable et un scenario perdant. C'est la option la
   plus rapide pour quiconque a un navigateur.
2. **Anvil en local**, hors de cet environnement sandbox (ex: sur la
   machine de l'utilisateur directement) : `anvil` puis utiliser
   `scripts/contracts/deploy_flash_arbitrage.py` pointe sur
   `http://127.0.0.1:8545`.
3. **Testnet Base Sepolia reel** : approvisionner le wallet de test en ETH
   (faucet gratuit), puis utiliser le script de deploiement ci-dessous.

## Fichiers

- `FlashArbitrage.sol` — le contrat principal.
- `test/Mocks.sol` — mocks (ERC20, Pool Aave, Router DEX) pour tests
  deterministes, **a ne jamais deployer sur testnet/mainnet reel**.
- `build/` — artefacts de compilation (ABI + bytecode), ignores par git.
- `deployments/<chain_id>.json` — infos de deploiement (adresse, ABI, tx
  hash) par reseau, generes par le script de deploiement, ignores par git.

## Adresses Aave V3 Pool connues

| Reseau             | Chain ID | Pool                                        |
|---------------------|----------|----------------------------------------------|
| Base Sepolia (test) | 84532    | `0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27` |
| Base (mainnet)      | 8453     | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5` |

## Deploiement

```powershell
$env:TESTNET_DEPLOYER_PRIVATE_KEY = "0x..."
.\.venv\Scripts\python.exe scripts\contracts\deploy_flash_arbitrage.py `
    --rpc-url https://sepolia.base.org `
    --private-key-env TESTNET_DEPLOYER_PRIVATE_KEY `
    --aave-pool 0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27
```

## Integration future avec le bot (non faite)

Une fois le contrat valide et deploye, l'integration cote Python
(`app/trading/executor.py`) consistera a ajouter un mode d'execution
"flashloan" qui, pour un signal juge rentable par `arbitrage.py` /
`triangular.py` mais dont la taille depasse le solde disponible du
wallet, encode la sequence de jambes en `Leg[]` et appelle
`startArbitrage` sur le contrat deploye au lieu d'executer les swaps
directement depuis le wallet. Ce travail n'a pas encore commence.
