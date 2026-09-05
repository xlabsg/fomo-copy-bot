// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
    function balanceOf(address who) external view returns (uint256);
}

interface ISwapRouter02 {
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }
    function exactInput(ExactInputParams calldata params) external payable returns (uint256 amountOut);
}

struct PoolKey {
    address currency0;
    address currency1;
    uint24 fee;
    int24 tickSpacing;
    address hooks;
}

interface IPoolManager {
    struct SwapParams {
        bool zeroForOne;
        int256 amountSpecified;
        uint160 sqrtPriceLimitX96;
    }
    function unlock(bytes calldata data) external returns (bytes memory);
    function swap(PoolKey memory key, SwapParams memory params, bytes calldata hookData) external returns (int256 delta);
    function sync(address currency) external;
    function settle() external payable returns (uint256);
    function take(address currency, address to, uint256 amount) external;
}

/// Stateless swap router for rh-copybot. The caller passes the full route
/// (a sequence of Uniswap V3 multihop legs and/or single V4 pools, hooked
/// pools included) with every call, so there is no owner-set config and any
/// wallet can use it. The route is validated leg-by-leg for token continuity
/// and minOut is enforced on the final output. Holds no balances between calls.
///
/// v2: V4 pools quoted in NATIVE ETH (currency address(0)) are supported. A leg
/// that outputs ETH leaves it in this contract for the next leg, which settles
/// it with msg.value. The first leg's input and the last leg's output must be
/// ERC-20s (USDG on one side, the token on the other).
contract CopyRouter {
    struct Leg {
        uint8 kind; // 0 = v3 path via SwapRouter02, 1 = v4 single pool
        bytes v3Path; // kind 0 only
        PoolKey key; // kind 1 only
        bool zeroForOne; // kind 1 only
    }

    ISwapRouter02 public immutable swapRouter;
    IPoolManager public immutable poolManager;
    address public immutable owner;

    uint160 internal constant MIN_SQRT_PRICE = 4295128739;
    uint160 internal constant MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342;

    bool internal locked;

    error BadRoute();
    error NotPoolManager();
    error Reentrancy();
    error NotOwner();

    receive() external payable {} // ETH from PoolManager.take on native legs

    constructor(ISwapRouter02 swapRouter_, IPoolManager poolManager_) {
        swapRouter = swapRouter_;
        poolManager = poolManager_;
        owner = msg.sender;
    }

    modifier nonReentrant() {
        if (locked) revert Reentrancy();
        locked = true;
        _;
        locked = false;
    }

    /// Execute `legs` in order starting from `amountIn` of the first leg's input
    /// token (pulled from msg.sender); the final output goes to `to`.
    function swap(Leg[] calldata legsIn, uint256 amountIn, uint256 minOut, address to)
        external
        nonReentrant
        returns (uint256 amt)
    {
        if (legsIn.length == 0) revert BadRoute();
        Leg[] memory legs = legsIn;
        address tokenIn = _legInput(legs[0]);
        if (tokenIn == address(0)) revert BadRoute(); // must start from an ERC-20 (USDG)
        address expect = tokenIn;
        for (uint256 i = 0; i < legs.length; i++) {
            if (legs[i].kind > 1) revert BadRoute();
            if (_legInput(legs[i]) != expect) revert BadRoute();
            expect = _legOutput(legs[i]);
            if (legs[i].kind == 0 && (legs[i].v3Path.length < 43 || (legs[i].v3Path.length - 20) % 23 != 0)) {
                revert BadRoute();
            }
        }
        address tokenOut = expect;
        if (tokenOut == address(0)) revert BadRoute(); // must end in an ERC-20

        require(IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn), "pull");
        amt = amountIn;
        for (uint256 i = 0; i < legs.length; i++) {
            if (legs[i].kind == 0) {
                IERC20(_legInput(legs[i])).approve(address(swapRouter), amt);
                amt = swapRouter.exactInput(
                    ISwapRouter02.ExactInputParams({
                        path: legs[i].v3Path,
                        recipient: address(this),
                        amountIn: amt,
                        amountOutMinimum: 0 // route-level minOut enforced below
                    })
                );
            } else {
                amt = _v4Swap(legs[i].key, legs[i].zeroForOne, amt);
            }
        }
        require(amt >= minOut, "slippage");
        require(IERC20(tokenOut).transfer(to, amt), "payout");
    }

    /// Recover dust from partial fills or accidental transfers (token 0 = ETH).
    function sweep(address token, address to) external {
        if (msg.sender != owner) revert NotOwner();
        if (token == address(0)) {
            (bool ok,) = to.call{value: address(this).balance}("");
            require(ok, "sweep eth");
            return;
        }
        require(IERC20(token).transfer(to, IERC20(token).balanceOf(address(this))), "sweep");
    }

    // ---------------------------------------------------------------- v4

    function _v4Swap(PoolKey memory key, bool zeroForOne, uint256 amountIn) internal returns (uint256 amountOut) {
        bytes memory result = poolManager.unlock(abi.encode(key, zeroForOne, amountIn));
        amountOut = abi.decode(result, (uint256));
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        (PoolKey memory key, bool zeroForOne, uint256 amountIn) = abi.decode(data, (PoolKey, bool, uint256));

        int256 delta = poolManager.swap(
            key,
            IPoolManager.SwapParams({
                zeroForOne: zeroForOne,
                amountSpecified: -int256(amountIn),
                sqrtPriceLimitX96: zeroForOne ? MIN_SQRT_PRICE + 1 : MAX_SQRT_PRICE - 1
            }),
            ""
        );
        int128 amount0 = int128(delta >> 128);
        int128 amount1 = int128(delta);
        (address currencyIn, int128 inDelta, address currencyOut, int128 outDelta) = zeroForOne
            ? (key.currency0, amount0, key.currency1, amount1)
            : (key.currency1, amount1, key.currency0, amount0);
        require(inDelta < 0 && outDelta > 0, "v4 delta");
        uint256 owed = uint256(uint128(-inDelta));
        uint256 amountOut = uint256(uint128(outDelta));

        if (currencyIn == address(0)) {
            // native ETH held from the previous leg: settle with value, no sync
            poolManager.settle{value: owed}();
        } else {
            poolManager.sync(currencyIn);
            require(IERC20(currencyIn).transfer(address(poolManager), owed), "settle transfer");
            poolManager.settle();
        }
        poolManager.take(currencyOut, address(this), amountOut); // native: ETH arrives via receive()
        return abi.encode(amountOut);
    }

    // ---------------------------------------------------------------- legs

    function _legInput(Leg memory leg) internal pure returns (address a) {
        if (leg.kind == 1) return leg.zeroForOne ? leg.key.currency0 : leg.key.currency1;
        bytes memory p = leg.v3Path;
        assembly {
            a := shr(96, mload(add(p, 32)))
        }
    }

    function _legOutput(Leg memory leg) internal pure returns (address a) {
        if (leg.kind == 1) return leg.zeroForOne ? leg.key.currency1 : leg.key.currency0;
        bytes memory p = leg.v3Path;
        uint256 len = p.length;
        assembly {
            a := shr(96, mload(add(add(p, 32), sub(len, 20))))
        }
    }
}
