// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title FlashArbitrage
/// @notice Contrat d'arbitrage on-chain finance par flashloan Aave V3.
///         Emprunte un actif via Aave, execute une sequence de swaps
///         (2 jambes classiques ou cycle triangulaire) sur des routers
///         de type Uniswap V2 / Aerodrome, rembourse le flashloan + prime,
///         et reverse le profit net au owner. Toute la sequence est atomique :
///         si la somme finale ne couvre pas le remboursement + profit minimum,
///         la transaction entiere revert (aucune perte de capital possible,
///         seul le gas de la tentative est perdu).
/// @dev La detection d'opportunites reste cote Python (app/trading/*) ; ce
///      contrat ne fait qu'executer une sequence de swaps deja calculee et
///      jugee rentable par le bot, ce qui permet de s'affranchir de la
///      contrainte de capital disponible sur le wallet (financement par
///      flashloan au lieu du solde propre).

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @notice Interface minimale du Pool Aave V3 (flashloan simple, un seul actif).
interface IAavePool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
}

/// @notice Router generique de type Uniswap V2 (Sushiswap, Pancakeswap, Baseswap, Alien Base...).
interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

/// @notice Router Aerodrome (Solidly-fork), routes typees avec flag stable/volatile + factory.
interface IAerodromeRouter {
    struct Route {
        address from;
        address to;
        bool stable;
        address factory;
    }

    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        Route[] calldata routes,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

/// @notice Router Uniswap V3 "SwapRouter02" (PAS le SwapRouter original : sur
///         Base, l'adresse utilisee par le bot est bien SwapRouter02, dont la
///         struct ExactInputSingleParams n'a PAS de champ `deadline`,
///         contrairement au SwapRouter V3 original -- voir
///         app/trading/routers.py::UNISWAP_V3_ROUTER_ABI, meme struct ici).
///         Un seul hop par jambe (exactInputSingle) ; le multi-hop
///         (exactInput, chemin bytes packe) reste hors scope.
interface IUniswapV3SwapRouter02 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

/// @notice Router Aerodrome Slipstream (pools a liquidite concentree, style
///         Uniswap V3, mais avec `tickSpacing` au lieu de `fee` et un champ
///         `deadline` explicite dans la struct -- voir
///         app/trading/routers.py::AERODROME_SLIPSTREAM_ROUTER_ABI, source
///         officielle aerodrome-finance/slipstream ISwapRouter.sol). Un seul
///         hop par jambe (exactInputSingle), comme pour Uniswap V3/PancakeSwap
///         V3 ci-dessus.
interface ISlipstreamSwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        int24 tickSpacing;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

/// @notice Router PancakeSwap V3 (`IV3SwapRouter` officiel de PancakeSwap,
///         PAS SwapRouter02 d'Uniswap) : struct ExactInputSingleParams AVEC
///         un champ `deadline` explicite, confirme on-chain le 2026 (appel
///         direct au router avec/sans `deadline` : la variante SANS
///         `deadline` revert en "no data" -- mauvais selecteur/layout --
///         alors que la variante AVEC `deadline` est bien reconnue, cf.
///         tmp_check29.py). Bug racine du "no data" universel sur TOUTES
///         les jambes PancakeSwap V3 (meme sur WETH/USDC, paire de controle
///         ultra-liquide) -- rien a voir avec BRETT/AERO specifiquement.
interface IPancakeV3SwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

contract FlashArbitrage {
    /// @notice Jambe generique du cycle d'arbitrage. kind=0 -> router V2 classique,
    ///         kind=1 -> router Aerodrome (necessite stable + factory),
    ///         kind=2 -> router Uniswap V3 uniquement (SwapRouter02, pas de
    ///         `deadline`) un seul hop (necessite `fee`, le fee tier de la
    ///         pool V3 concernee), kind=3 -> router Aerodrome Slipstream un
    ///         seul hop (le champ `fee` (uint24) est reutilise pour stocker
    ///         le `tickSpacing` (int24), toujours petit et positif, caste
    ///         explicitement dans `_executeLeg` -- aucun champ Leg additionnel),
    ///         kind=4 -> router PancakeSwap V3 (`IV3SwapRouter`, AVEC
    ///         `deadline`, ABI differente de kind=2 malgre l'apparence
    ///         similaire -- voir IPancakeV3SwapRouter ci-dessus).
    struct Leg {
        uint8 kind;
        address router;
        address tokenIn;
        address tokenOut;
        bool aerodromeStable;
        address aerodromeFactory;
        uint24 fee;
    }

    address public immutable owner;
    address public immutable aavePool;

    event ArbitrageExecuted(
        address indexed asset,
        uint256 amountBorrowed,
        uint256 premium,
        uint256 profit,
        address indexed beneficiary
    );

    error NotOwner();
    error NotPool();
    error NotSelfInitiated();
    error EmptyLegs();
    error InsufficientProfit(uint256 amountOwed, uint256 minRequired, uint256 balanceAfter);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(address _aavePool) {
        owner = msg.sender;
        aavePool = _aavePool;
    }

    /// @notice Declenche le cycle d'arbitrage : emprunte `amount` de `asset` via
    ///         Aave, puis execute `legs` en sequence dans le callback Aave.
    /// @param asset Actif emprunte (doit etre le token de depart ET d'arrivee du cycle).
    /// @param amount Montant emprunte (en plus petite unite du token).
    /// @param legs Sequence ordonnee des swaps a executer (doit boucler sur `asset`).
    /// @param minProfit Profit net minimum exige (en unite de `asset`) ; sinon revert total.
    function startArbitrage(
        address asset,
        uint256 amount,
        Leg[] calldata legs,
        uint256 minProfit
    ) external onlyOwner {
        if (legs.length == 0) revert EmptyLegs();
        bytes memory params = abi.encode(legs, minProfit, msg.sender);
        IAavePool(aavePool).flashLoanSimple(address(this), asset, amount, params, 0);
    }

    /// @notice Callback Aave V3, appele automatiquement par le Pool apres transfert
    ///         des fonds empruntes. Execute les swaps, verifie la rentabilite,
    ///         rembourse le pool, et transfere le profit au beneficiaire.
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        if (msg.sender != aavePool) revert NotPool();
        if (initiator != address(this)) revert NotSelfInitiated();

        (Leg[] memory legs, uint256 minProfit, address beneficiary) =
            abi.decode(params, (Leg[], uint256, address));

        uint256 amountIn = amount;
        for (uint256 i = 0; i < legs.length; i++) {
            amountIn = _executeLeg(legs[i], amountIn);
        }

        uint256 amountOwed = amount + premium;
        uint256 finalBalance = IERC20(asset).balanceOf(address(this));
        if (finalBalance < amountOwed + minProfit) {
            revert InsufficientProfit(amountOwed, minProfit, finalBalance);
        }

        // Remboursement du flashloan : Aave tire les fonds via allowance.
        IERC20(asset).approve(aavePool, amountOwed);

        uint256 profit = finalBalance - amountOwed;
        if (profit > 0) {
            IERC20(asset).transfer(beneficiary, profit);
        }

        emit ArbitrageExecuted(asset, amount, premium, profit, beneficiary);
        return true;
    }

    function _executeLeg(Leg memory leg, uint256 amountIn) private returns (uint256) {
        IERC20(leg.tokenIn).approve(leg.router, amountIn);

        if (leg.kind == 2) {
            return IUniswapV3SwapRouter02(leg.router).exactInputSingle(
                IUniswapV3SwapRouter02.ExactInputSingleParams({
                    tokenIn: leg.tokenIn,
                    tokenOut: leg.tokenOut,
                    fee: leg.fee,
                    recipient: address(this),
                    amountIn: amountIn,
                    amountOutMinimum: 0,
                    sqrtPriceLimitX96: 0
                })
            );
        }

        if (leg.kind == 3) {
            return ISlipstreamSwapRouter(leg.router).exactInputSingle(
                ISlipstreamSwapRouter.ExactInputSingleParams({
                    tokenIn: leg.tokenIn,
                    tokenOut: leg.tokenOut,
                    tickSpacing: int24(leg.fee),
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: amountIn,
                    amountOutMinimum: 0,
                    sqrtPriceLimitX96: 0
                })
            );
        }

        if (leg.kind == 4) {
            return IPancakeV3SwapRouter(leg.router).exactInputSingle(
                IPancakeV3SwapRouter.ExactInputSingleParams({
                    tokenIn: leg.tokenIn,
                    tokenOut: leg.tokenOut,
                    fee: leg.fee,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: amountIn,
                    amountOutMinimum: 0,
                    sqrtPriceLimitX96: 0
                })
            );
        }

        uint256[] memory amountsOut;
        if (leg.kind == 1) {
            IAerodromeRouter.Route[] memory routes = new IAerodromeRouter.Route[](1);
            routes[0] = IAerodromeRouter.Route({
                from: leg.tokenIn,
                to: leg.tokenOut,
                stable: leg.aerodromeStable,
                factory: leg.aerodromeFactory
            });
            amountsOut = IAerodromeRouter(leg.router).swapExactTokensForTokens(
                amountIn, 0, routes, address(this), block.timestamp
            );
        } else {
            address[] memory path = new address[](2);
            path[0] = leg.tokenIn;
            path[1] = leg.tokenOut;
            amountsOut = IUniswapV2Router(leg.router).swapExactTokensForTokens(
                amountIn, 0, path, address(this), block.timestamp
            );
        }
        return amountsOut[amountsOut.length - 1];
    }

    /// @notice Recupere des tokens restes bloques sur le contrat (ex: tentative
    ///         avortee hors flashloan, ou envoi accidentel). Reserve au owner.
    function rescueTokens(address token, uint256 amount) external onlyOwner {
        IERC20(token).transfer(owner, amount);
    }
}
