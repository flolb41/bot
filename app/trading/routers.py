"""Adresses de contrats et ABIs minimales pour l'exécution réelle de swaps sur Base.

Toutes les adresses ci-dessous ont été vérifiées individuellement via DEUX
sources indépendantes avant d'être codées en dur (documentation officielle du
protocole + étiquette de contrat vérifié sur BaseScan, recoupée avec un second
explorateur quand c'était pertinent) :

- Uniswap V3 SwapRouter02  : developers.uniswap.org/docs (déploiements Base) + BaseScan
- Uniswap V3 Factory       : developers.uniswap.org/docs (déploiements Base)
- Aerodrome Router         : github.com/aerodrome-finance/contracts (IRouter.sol) + BaseScan
- SushiSwap Router v2      : BaseScan + codeslaw.app (recoupement, contrat à 2M+ tx)

PancakeSwap v2 Router (classique, `swapExactTokensForTokens`, ABI standard
UniswapV2Router02) a été vérifié et whitelisté pour l'exécution (voir
EXECUTABLE_DEXES) — distinct de son "Smart Router"/Universal Router (jamais
utilisé ici, encodage multi-route trop complexe/risqué) :
- PancakeSwap Router v2 : docs.pancakeswap.finance → developer.pancakeswap.finance/contracts/v2/addresses
  (PancakeRouter.sol, "Periphery" Base) + étiquette "PancakeSwap: Router v2.0"
  vérifiée sur BaseScan (contrat à 265k+ tx).

SwapBased Router (fork UniswapV2Router02 classique, même ABI que
sushiswap/pancakeswap/baseswap/alien-base) vérifié via TROIS sources
concordantes :
- docs.swapbased.finance/informational/contract-adresses (adresse officielle)
- BaseScan : étiquette "SwapBased : Uniswap V2 Router 02" vérifiée (332k+ tx)
- DexScreener API : dexId "swapbased" confirmé sur une pool SwapBased connue

Ne JAMAIS ajouter une adresse ici sans la vérifier via au moins deux sources
indépendantes (cf. pratique établie dans app/trading/arbitrage.py pour les
seed_tokens) : une adresse de router erronée ferait perdre les fonds envoyés.

PancakeSwap V3 (SwapRouter, ABI `IV3SwapRouter`, pas de `deadline`) et
Aerodrome Slipstream (CL, 3 factories concurrentes) ajoutés le 2026-10-08 :
adresses vérifiées via developer.pancakeswap.finance/contracts/v3/addresses
(table officielle) + BaseScan (étiquette "PancakeSwap V3: Swap Router",
1,7M+ tx) pour PancakeSwap, et github.com/aerodrome-finance/slipstream
(README, table "Deployments") pour Aerodrome Slipstream — PAS exposés comme de
nouveaux dex_id/EXECUTABLE_DEXES : `executor._onchain_pool_price_usd` et
`_build_swap_call`/`flashloan._build_leg` choisissent dynamiquement, pour
"aerodrome"/"pancakeswap", entre le pool classique et le(s) pool(s)
V3/Slipstream selon la plus grande réserve on-chain (voir leurs docstrings).
"""
from __future__ import annotations

CHAIN_ID = 8453  # Base mainnet

# DEX dont l'exécution réelle est implémentée. Les autres DEX whitelistés pour
# la DÉTECTION (voir config.yaml trading.dex_whitelist) peuvent rester dans la
# comparaison de prix sans jamais être utilisés comme buy_dex/sell_dex réel.
#
# baseswap / alien-base : adresses de router vérifiées via DEUX sources
# indépendantes (étiquette "Verified" BaseScan + recoupement doc/GitHub
# officiel du projet, cf. historique de session) le 2026-10-07. Tous deux sont
# des forks classiques Uniswap V2 (`swapExactTokensForTokens`), même ABI que
# sushiswap/pancakeswap ci-dessous (voir `_build_swap_call` dans executor.py).
EXECUTABLE_DEXES = frozenset({
    "uniswap", "aerodrome", "sushiswap", "pancakeswap", "baseswap", "alien-base", "swapbased",
})

ROUTER_ADDRESSES: dict[str, str] = {
    "uniswap": "0x2626664c2603336E57B271c5C0b26F421741e481",     # SwapRouter02
    "aerodrome": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",    # Router.sol
    "sushiswap": "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891",    # UniswapV2Router02-style
    "pancakeswap": "0x8cFe327CEc66d1C090Dd72bd0FF11d690C33a2Eb",  # PancakeRouter.sol v2 (UniswapV2Router02-style)
    "baseswap": "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86",     # BaseSwap Router (UniswapV2Router02-style)
    "alien-base": "0x8c1A3cF8f83074169FE5D7aD50B978e1cD6b37c7",   # AlienBase Router (UniswapV2Router02-style)
    "swapbased": "0xaaa3b1F1bd7BCc97fD1917c18ADE665C5D31F066",    # SwapBased Router (UniswapV2Router02-style)
}

UNISWAP_V3_FACTORY_ADDRESS = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V3_FEE_TIERS = (500, 3000, 10000, 100)  # ordre d'essai : les plus courants d'abord

# PancakeSwap V3 (concentrated liquidity, même ABI que Uniswap V3 SwapRouter02
# sans `deadline` -- interface officielle `IV3SwapRouter`, confirmée via
# developer.pancakeswap.finance/contracts/v3/smartrouter/v3swaprouter +
# raw.githubusercontent.com/Uniswap/swap-router-contracts (struct identique,
# PancakeSwap documente explicitement une interface nommée pareil). Adresses
# vérifiées sur developer.pancakeswap.finance/contracts/v3/addresses (table
# officielle Base) ET recoupées sur BaseScan (étiquette "PancakeSwap V3: Swap
# Router", 1,7M+ tx — PAS le "Smart Router" multi-protocole
# 0x678Aa4bF4E210cf2166753e054d5b7c31cc7fa86, jamais utilisé ici, même principe
# que le choix du Router v2 classique pour PancakeSwap v2 ci-dessus).
PANCAKESWAP_V3_ROUTER_ADDRESS = "0x1b81D678ffb9C0263b24A97847620C99d213eB14"
PANCAKESWAP_V3_FACTORY_ADDRESS = "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"
PANCAKESWAP_V3_FEE_TIERS = (500, 2500, 100, 10000)  # confirmé on-chain : les 4 tiers ont une pool WETH/USDC

# Aerodrome Slipstream (concentrated liquidity, fork de Uniswap V3) --
# fragmenté en TROIS factories/routers concurrents et simultanément actifs
# (voir github.com/aerodrome-finance/slipstream README, table "Deployments") :
# aucun ne remplace les précédents, les pools existantes restent utilisables
# sur chacun. `_best_aerodrome_slipstream_pool` (executor.py) les interroge
# TOUTES et choisit la pool avec la plus grande réserve on-chain.
AERODROME_SLIPSTREAM_FACTORIES: tuple[tuple[str, str, str], ...] = (
    # (nom, PoolFactory, SwapRouter)
    ("initial", "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A", "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"),
    ("gauge_caps", "0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a", "0xcbBb8035cAc7D4B3Ca7aBb74cF7BdF900215Ce0D"),
    ("gauges_v3", "0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef", "0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F"),
)

# Contrat `FlashArbitrage` (arbitrage financé par flashloan Aave V3, voir
# contracts/FlashArbitrage.sol et contracts/README.md) déployé RÉELLEMENT sur
# Base mainnet. C'est la SEULE adresse, en dehors des routers DEX ci-dessus,
# vers laquelle `app.trading.guardrails.send_trading_transaction` autorise
# l'envoi d'une transaction — volontairement codée en dur ici pour rester
# cohérente avec le principe "whitelist fixe, non modifiable par variable
# d'environnement" déjà appliqué à ROUTER_ADDRESSES (voir docstring de
# guardrails.send_trading_transaction).
#
# v1 (2026-10-08, tx 0x9d91d458c69dd2e946c6d44c92ebb5fc7a3d4e04e2b57c435362e068aa1f7642) :
#   0x73beAacEE6CD3Dc0cb4816Dd48cbdCEE45dcc1e8 — legs kind=0 (V2 fork) / kind=1
#   (Aerodrome) uniquement, PLUS UTILISÉ.
# v2 (2026-10-08, tx db08dc75f7ab042fd66cee7251e424899e1dbc275cf3a203da4e5c857be39964) :
#   0x6991D2e6cDB057c50978a6111E31fd8f3c524727 — ajoute kind=2 (Uniswap V3
#   SwapRouter02, single-hop exactInputSingle), PLUS UTILISÉ.
# v3 (déploiement courant, tx edb04cb5168b3c552158efe56255c6909cca15593e854adc1835996bb98af86d) :
#   ajoute kind=3 (Aerodrome Slipstream, single-hop exactInputSingle avec
#   tickSpacing) ; kind=2 est aussi réutilisé pour PancakeSwap V3 (même ABI
#   SwapRouter02, aucun changement Solidity nécessaire pour ce DEX).
FLASH_ARBITRAGE_ADDRESS = "0x8B1815a311B580AdD95d00B23B6A90bd7eE777aD"
FLASHLOAN_CONTRACT_ADDRESSES = frozenset({FLASH_ARBITRAGE_ADDRESS})

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}],
     "stateMutability": "view", "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
]

UNISWAP_V3_FACTORY_ABI = [
    {"inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"},
                {"name": "fee", "type": "uint24"}],
     "name": "getPool", "outputs": [{"name": "pool", "type": "address"}],
     "stateMutability": "view", "type": "function"},
]

UNISWAP_V3_ROUTER_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "recipient", "type": "address"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
            "name": "params", "type": "tuple",
        }],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    },
]

# Aerodrome Router.sol (IRouter.sol vérifié sur github.com/aerodrome-finance/contracts)
AERODROME_ROUTER_ABI = [
    {"inputs": [], "name": "defaultFactory", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [
        {"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"},
        {"name": "stable", "type": "bool"}, {"name": "_factory", "type": "address"},
     ],
     "name": "poolFor", "outputs": [{"name": "pool", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {
                "components": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "stable", "type": "bool"},
                    {"name": "factory", "type": "address"},
                ],
                "name": "routes", "type": "tuple[]",
            },
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable", "type": "function",
    },
]

# SushiSwap Router v2 (ABI standard UniswapV2Router02)
UNISWAP_V2_ROUTER_ABI = [
    {
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable", "type": "function",
    },
]

# ABIs minimales de POOL (lecture seule, aucune fonction d'écriture) utilisées
# par `executor._onchain_pool_price_usd` pour lire le prix directement depuis
# le pool exact qui sera routé par `_build_swap_call` — en complément de la
# revérification DexScreener (`_refresh_price_usd`), pour les deux DEX où
# l'ambiguïté multi-pool par dex_id a été démontrée (voir historique du fix
# `InsufficientOutputAmount` : plusieurs pools Aerodrome/Uniswap partagent le
# même dex_id sur DexScreener alors qu'un seul est réellement utilisé ici).
AERODROME_POOL_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getReserves", "outputs": [
        {"name": "_reserve0", "type": "uint256"},
        {"name": "_reserve1", "type": "uint256"},
        {"name": "_blockTimestampLast", "type": "uint256"},
     ], "stateMutability": "view", "type": "function"},
]

UNISWAP_V3_POOL_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"},
        {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"},
        {"name": "feeProtocol", "type": "uint8"},
        {"name": "unlocked", "type": "bool"},
     ], "stateMutability": "view", "type": "function"},
]

# ABIs minimales (lecture seule) pour les forks UniswapV2 classiques
# (sushiswap/pancakeswap/baseswap/alien-base/swapbased) : chaque routeur V2
# expose `factory()`, et la factory expose `getPair(tokenA, tokenB)` ->
# adresse de LA pool canonique unique pour cette paire sur ce DEX (contrairement
# à Aerodrome/Uniswap V3, un fork V2 classique n'a qu'UNE seule pool possible
# par paire, d'où l'ancienne restriction de `_onchain_pool_price_usd` à
# aerodrome/uniswap — mais lire cette pool unique directement reste plus frais
# qu'une revérification DexScreener, d'où cette extension).
UNISWAP_V2_ROUTER_FACTORY_ABI = [
    {"inputs": [], "name": "factory", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
]

UNISWAP_V2_FACTORY_ABI = [
    {"inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}],
     "name": "getPair", "outputs": [{"name": "pair", "type": "address"}],
     "stateMutability": "view", "type": "function"},
]

UNISWAP_V2_PAIR_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getReserves", "outputs": [
        {"name": "_reserve0", "type": "uint112"},
        {"name": "_reserve1", "type": "uint112"},
        {"name": "_blockTimestampLast", "type": "uint32"},
     ], "stateMutability": "view", "type": "function"},
]

# Factory générique "Uniswap-V3-style" : même signature `getPool(tokenA,
# tokenB, fee)` pour Uniswap V3 ET PancakeSwap V3 (fork quasi-identique) —
# réutilisée par `executor._best_v3_style_pool`, aussi utilisée pour Uniswap V3
# lui-même depuis le 08/10 via `_find_uniswap_v3_fee_tier` (voir son
# docstring).
PANCAKESWAP_V3_FACTORY_ABI = UNISWAP_V3_FACTORY_ABI

# Pool Uniswap-V3-style générique (même shape que UNISWAP_V3_POOL_ABI, utilisée
# aussi pour PancakeSwap V3 — fork quasi-identique).
PANCAKESWAP_V3_POOL_ABI = UNISWAP_V3_POOL_ABI

PANCAKESWAP_V3_ROUTER_ABI = UNISWAP_V3_ROUTER_ABI

# Aerodrome Slipstream (CL) : factory `getPool(tokenA, tokenB, tickSpacing)` +
# `tickSpacings()` (liste des tick spacings activés, utilisée pour ne pas
# deviner/fixer en dur une liste potentiellement incomplète), vérifiées via
# github.com/aerodrome-finance/slipstream (ICLFactory.sol).
AERODROME_SLIPSTREAM_FACTORY_ABI = [
    {"inputs": [
        {"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"},
        {"name": "tickSpacing", "type": "int24"},
     ], "name": "getPool", "outputs": [{"name": "pool", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "tickSpacings", "outputs": [{"name": "", "type": "int24[]"}],
     "stateMutability": "view", "type": "function"},
]

# Pool Slipstream (CL) : `slot0()` a une shape différente de Uniswap V3 (pas de
# `feeProtocol` final, mais ça ne change rien pour nous : seul `sqrtPriceX96`,
# premier élément, est utilisé) — vérifiée via ICLPoolState.sol.
AERODROME_SLIPSTREAM_POOL_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"},
        {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"},
        {"name": "unlocked", "type": "bool"},
     ], "stateMutability": "view", "type": "function"},
]

# Router Slipstream `ISwapRouter.ExactInputSingleParams` -- CONTRAIREMENT à
# Uniswap V3 SwapRouter02/PancakeSwap V3 (pas de `deadline`), ce struct EN A
# UN, et utilise `tickSpacing` (int24) au lieu de `fee` (uint24) -- vérifié via
# github.com/aerodrome-finance/slipstream (periphery/interfaces/ISwapRouter.sol).
# Voir kind=3 dans contracts/FlashArbitrage.sol (`ISlipstreamSwapRouter`).
AERODROME_SLIPSTREAM_ROUTER_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "tickSpacing", "type": "int24"},
                {"name": "recipient", "type": "address"},
                {"name": "deadline", "type": "uint256"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
            "name": "params", "type": "tuple",
        }],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    },
]
