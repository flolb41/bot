// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Mocks utilises UNIQUEMENT pour les tests locaux (anvil) de
///         FlashArbitrage.sol. Ne jamais deployer sur testnet/mainnet :
///         ils simulent un Pool Aave et des routers DEX avec des taux de
///         change fixes/arbitraires, sans aucune verification de securite
///         reelle (pas d'oracle, pas de liquidite reelle).

interface IERC20Min {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

contract MockERC20 {
    string public name;
    string public symbol;
    uint8 public decimals = 18;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory _name, string memory _symbol) {
        name = _name;
        symbol = _symbol;
    }

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        totalSupply += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "allowance too low");
        allowance[from][msg.sender] = allowed - amount;
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "balance too low");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
    }
}

/// @notice Simule le Pool Aave V3 : pret un actif deja detenu par le mock,
///         appelle executeOperation sur le receiver, puis tire le
///         remboursement (amount + premium) via transferFrom.
contract MockAavePool {
    uint256 public premiumBps = 5; // 0.05%, comme Aave V3 reel

    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 /* referralCode */
    ) external {
        uint256 premium = (amount * premiumBps) / 10000;
        IERC20Min(asset).transfer(receiverAddress, amount);

        (bool ok, bytes memory ret) = receiverAddress.call(
            abi.encodeWithSignature(
                "executeOperation(address,uint256,uint256,address,bytes)",
                asset,
                amount,
                premium,
                receiverAddress,
                params
            )
        );
        require(ok, "executeOperation failed");
        bool success = abi.decode(ret, (bool));
        require(success, "executeOperation returned false");

        IERC20Min(asset).transferFrom(receiverAddress, address(this), amount + premium);
    }
}

/// @notice Simule un router DEX avec un taux de change fixe configurable
///         (en basis points, 10000 = parite 1:1). Permet de construire des
///         scenarios rentables ou perdants de maniere deterministe.
contract MockRouter {
    mapping(address => mapping(address => uint256)) public rateBps;

    function setRate(address tokenIn, address tokenOut, uint256 bps) external {
        rateBps[tokenIn][tokenOut] = bps;
    }

    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 /* amountOutMin */,
        address[] calldata path,
        address to,
        uint256 /* deadline */
    ) external returns (uint256[] memory amounts) {
        address tokenIn = path[0];
        address tokenOut = path[path.length - 1];
        uint256 bps = rateBps[tokenIn][tokenOut];
        require(bps > 0, "rate not set");

        IERC20Min(tokenIn).transferFrom(msg.sender, address(this), amountIn);
        uint256 amountOut = (amountIn * bps) / 10000;
        IERC20Min(tokenOut).transfer(to, amountOut);

        amounts = new uint256[](2);
        amounts[0] = amountIn;
        amounts[1] = amountOut;
    }
}
