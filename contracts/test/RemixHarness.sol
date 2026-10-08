// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../FlashArbitrage.sol";
import "./Mocks.sol";

/// @title FlashArbitrageHarness
/// @notice Harnais de test a utiliser dans Remix IDE (Remix VM, simulateur
///         EVM dans le navigateur) pour valider FlashArbitrage.sol SANS
///         AUCUN RISQUE : aucun reseau reel, aucun fonds reel, tout tourne
///         en memoire dans l'onglet du navigateur.
///
///         Mode d'emploi (5 minutes) :
///         1. Aller sur https://remix.ethereum.org
///         2. Dans l'explorateur de fichiers (icone dossier a gauche),
///            cliquer sur l'icone "Clone" (logo GitHub) et coller :
///            https://github.com/flolb41/bot.git
///         3. Ouvrir contracts/test/RemixHarness.sol
///         4. Onglet "Solidity Compiler" (icone S) : choisir le compilateur
///            0.8.20, cliquer "Compile RemixHarness.sol"
///         5. Onglet "Deploy & Run Transactions" (icone Ethereum) :
///            - Environment = "Remix VM (Cancun)" (par defaut, pas besoin
///              de wallet ni de fonds reels)
///            - Selectionner "FlashArbitrageHarness" dans la liste
///            - Cliquer "Deploy"
///         6. Dans "Deployed Contracts", deplier l'instance deployee :
///            - Cliquer "runProfitableScenario" (bouton orange) -> doit
///              reussir (pas d'erreur dans la console du bas). Puis
///              cliquer "usdc" (bleu) pour verifier son adresse, et
///              utiliser "balanceOf" avec l'adresse du harnais (affichee
///              en haut de l'instance deployee) : le solde doit avoir
///              augmente (profit recupere).
///            - Redeployer une nouvelle instance (etape 5) puis cliquer
///              "runLosingScenario" -> DOIT afficher une erreur
///              "VM error: revert" avec la raison InsufficientProfit dans
///              la console du bas. C'est le comportement attendu : la
///              transaction perdante est bloquee, aucune perte de fonds.
///            - Redeployer une nouvelle instance (etape 5) puis cliquer
///              "runV3LegScenario" -> doit reussir exactement comme
///              "runProfitableScenario" (meme taux, une jambe passe juste
///              par le router V3 mock au lieu du router V2 mock) : valide
///              la nouvelle jambe `kind=2` (Uniswap V3 SwapRouter02) du
///              contrat FlashArbitrage.
///            - Pour tester le controle d'acces : copier l'adresse
///              affichee par "flashArb" (bouton bleu), aller dans l'onglet
///              compilateur, selectionner "FlashArbitrage" dans la liste
///              deroulante, revenir dans "Deploy & Run", utiliser "At
///              Address" avec cette adresse pour charger l'instance,
///              changer le compte dans le menu deroulant "Account" en haut
///              (choisir un compte different de celui utilise pour le
///              deploiement), puis appeler "startArbitrage" -> doit
///              revert avec NotOwner.
contract FlashArbitrageHarness {
    MockERC20 public usdc;
    MockERC20 public weth;
    MockAavePool public pool;
    MockRouter public routerA;
    MockRouter public routerB;
    MockV3Router public routerV3;
    FlashArbitrage public flashArb;

    uint24 public constant FEE_TIER = 500;

    constructor() {
        usdc = new MockERC20("Mock USDC", "mUSDC");
        weth = new MockERC20("Mock WETH", "mWETH");
        pool = new MockAavePool();
        routerA = new MockRouter();
        routerB = new MockRouter();
        routerV3 = new MockV3Router();
        flashArb = new FlashArbitrage(address(pool));

        // Liquidite du pool Aave simule : 1 000 000 mUSDC disponibles a preter.
        usdc.mint(address(pool), 1_000_000 ether);

        // Taux par defaut : scenario rentable (+1% puis +1.5%, net positif
        // apres la prime Aave simulee de 0.05%).
        routerA.setRate(address(usdc), address(weth), 10100);
        routerB.setRate(address(weth), address(usdc), 10150);

        // Meme scenario rentable, mais jambe 1 (achat) via le router V3 mock
        // (kind=2) au lieu du router V2 mock -> valide `_executeLeg`/kind=2.
        routerV3.setRate(address(usdc), address(weth), FEE_TIER, 10100);

        // Liquidite des routers mocks pour qu'ils puissent payer les swaps.
        weth.mint(address(routerA), 1_000_000 ether);
        usdc.mint(address(routerB), 1_000_000 ether);
        weth.mint(address(routerV3), 1_000_000 ether);
    }

    function _legs() private view returns (FlashArbitrage.Leg[] memory legs) {
        legs = new FlashArbitrage.Leg[](2);
        legs[0] = FlashArbitrage.Leg({
            kind: 0,
            router: address(routerA),
            tokenIn: address(usdc),
            tokenOut: address(weth),
            aerodromeStable: false,
            aerodromeFactory: address(0),
            fee: 0
        });
        legs[1] = FlashArbitrage.Leg({
            kind: 0,
            router: address(routerB),
            tokenIn: address(weth),
            tokenOut: address(usdc),
            aerodromeStable: false,
            aerodromeFactory: address(0),
            fee: 0
        });
    }

    /// @notice Memes taux que `_legs()`, mais la jambe d'achat (USDC->WETH)
    ///         passe par le router V3 mock (`kind=2`) au lieu du router V2 mock.
    function _legsWithV3Leg() private view returns (FlashArbitrage.Leg[] memory legs) {
        legs = new FlashArbitrage.Leg[](2);
        legs[0] = FlashArbitrage.Leg({
            kind: 2,
            router: address(routerV3),
            tokenIn: address(usdc),
            tokenOut: address(weth),
            aerodromeStable: false,
            aerodromeFactory: address(0),
            fee: FEE_TIER
        });
        legs[1] = FlashArbitrage.Leg({
            kind: 0,
            router: address(routerB),
            tokenIn: address(weth),
            tokenOut: address(usdc),
            aerodromeStable: false,
            aerodromeFactory: address(0),
            fee: 0
        });
    }

    /// @notice Cycle rentable (+1% puis +1.5%) : doit reussir et transferer
    ///         le profit net au harnais (beneficiaire = msg.sender de
    ///         startArbitrage, ici ce contrat lui-meme).
    function runProfitableScenario() external {
        flashArb.startArbitrage(address(usdc), 10_000 ether, _legs(), 1 ether);
    }

    /// @notice Cycle perdant (routerB passe a -2%) : DOIT revert
    ///         entierement (InsufficientProfit), aucun fonds deplace.
    function runLosingScenario() external {
        routerB.setRate(address(weth), address(usdc), 9800);
        flashArb.startArbitrage(address(usdc), 10_000 ether, _legs(), 1 ether);
    }

    /// @notice Cycle rentable identique a `runProfitableScenario`, mais avec
    ///         une jambe Uniswap V3 (kind=2, via le router V3 mock) : doit
    ///         reussir et transferer le meme profit net que le scenario V2.
    function runV3LegScenario() external {
        flashArb.startArbitrage(address(usdc), 10_000 ether, _legsWithV3Leg(), 1 ether);
    }

    function usdcBalanceOfSelf() external view returns (uint256) {
        return usdc.balanceOf(address(this));
    }
}
