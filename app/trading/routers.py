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

Ne JAMAIS ajouter une adresse ici sans la vérifier via au moins deux sources
indépendantes (cf. pratique établie dans app/trading/arbitrage.py pour les
seed_tokens) : une adresse de router erronée ferait perdre les fonds envoyés.
"""
from __future__ import annotations

CHAIN_ID = 8453  # Base mainnet

# DEX dont l'exécution réelle est implémentée. Les autres DEX whitelistés pour
# la DÉTECTION (voir config.yaml trading.dex_whitelist) peuvent rester dans la
# comparaison de prix sans jamais être utilisés comme buy_dex/sell_dex réel.
EXECUTABLE_DEXES = frozenset({"uniswap", "aerodrome", "sushiswap", "pancakeswap"})

ROUTER_ADDRESSES: dict[str, str] = {
    "uniswap": "0x2626664c2603336E57B271c5C0b26F421741e481",     # SwapRouter02
    "aerodrome": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",    # Router.sol
    "sushiswap": "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891",    # UniswapV2Router02-style
    "pancakeswap": "0x8cFe327CEc66d1C090Dd72bd0FF11d690C33a2Eb",  # PancakeRouter.sol v2 (UniswapV2Router02-style)
}

UNISWAP_V3_FACTORY_ADDRESS = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V3_FEE_TIERS = (500, 3000, 10000, 100)  # ordre d'essai : les plus courants d'abord

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
