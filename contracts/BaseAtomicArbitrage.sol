// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address recipient, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
    function transferFrom(address sender, address recipient, uint256 amount) external returns (bool);
}

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

interface ISwapRouter {
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

contract BaseAtomicArbitrage {
    address public immutable owner;

    event ArbitrageExecuted(
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 netProfit
    );

    modifier onlyOwner() {
        require(msg.sender == owner, "NOT_OWNER");
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    function executeAtomicArbitrage(
        address aeroRouter,
        address uniRouter,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minNetProfit,
        bool aeroFirst,
        uint24 uniFeeTier,
        IAerodromeRouter.Route[] calldata aeroRoutes
    ) external onlyOwner returns (uint256 netProfit) {
        IERC20 tokenInContract = IERC20(tokenIn);
        IERC20 tokenOutContract = IERC20(tokenOut);

        uint256 startBalance = tokenInContract.balanceOf(address(this));
        
        // Transfer funds from sender to this contract
        require(tokenInContract.transferFrom(msg.sender, address(this), amountIn), "TRANSFER_FROM_FAILED");

        if (aeroFirst) {
            // Leg 1: Swap on Aerodrome (tokenIn -> tokenOut)
            tokenInContract.approve(aeroRouter, amountIn);
            IAerodromeRouter(aeroRouter).swapExactTokensForTokens(
                amountIn,
                1,
                aeroRoutes,
                address(this),
                block.timestamp
            );

            uint256 intermediateBalance = tokenOutContract.balanceOf(address(this));
            require(intermediateBalance > 0, "LEG1_FAILED");

            // Leg 2: Swap on Uniswap V3 (tokenOut -> tokenIn)
            tokenOutContract.approve(uniRouter, intermediateBalance);
            ISwapRouter(uniRouter).exactInputSingle(
                ISwapRouter.ExactInputSingleParams({
                    tokenIn: tokenOut,
                    tokenOut: tokenIn,
                    fee: uniFeeTier,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: intermediateBalance,
                    amountOutMinimum: 1,
                    sqrtPriceLimitX96: 0
                })
            );
        } else {
            // Leg 1: Swap on Uniswap V3 (tokenIn -> tokenOut)
            tokenInContract.approve(uniRouter, amountIn);
            uint256 intermediateBalance = ISwapRouter(uniRouter).exactInputSingle(
                ISwapRouter.ExactInputSingleParams({
                    tokenIn: tokenIn,
                    tokenOut: tokenOut,
                    fee: uniFeeTier,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: amountIn,
                    amountOutMinimum: 1,
                    sqrtPriceLimitX96: 0
                })
            );

            // Leg 2: Swap on Aerodrome (tokenOut -> tokenIn)
            tokenOutContract.approve(aeroRouter, intermediateBalance);
            IAerodromeRouter(aeroRouter).swapExactTokensForTokens(
                intermediateBalance,
                1,
                aeroRoutes,
                address(this),
                block.timestamp
            );
        }

        uint256 endBalance = tokenInContract.balanceOf(address(this));
        require(endBalance >= startBalance + amountIn + minNetProfit, "UNPROFITABLE_ARBITRAGE");

        netProfit = endBalance - (startBalance + amountIn);
        
        // Return capital + net profit to owner
        require(tokenInContract.transfer(owner, endBalance - startBalance), "TRANSFER_TO_OWNER_FAILED");

        emit ArbitrageExecuted(tokenIn, tokenOut, amountIn, netProfit);
    }

    // Emergency rescue funds
    function withdrawToken(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        if (bal > 0) IERC20(token).transfer(owner, bal);
    }

    function withdrawETH() external onlyOwner {
        payable(owner).transfer(address(this).balance);
    }

    receive() external payable {}
}
