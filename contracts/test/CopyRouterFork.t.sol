// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

import {CopyRouter, PoolKey, IERC20, ISwapRouter02, IPoolManager} from "../src/CopyRouter.sol";

interface Vm {
    function prank(address) external;
    function startPrank(address) external;
    function stopPrank() external;
    function deal(address, uint256) external;
}

/// Runs against a Robinhood Chain mainnet fork:  forge test --fork-url <rpc> -vv
/// Uses the bot wallet's real USDG via prank (read-only fork, nothing is broadcast).
contract CopyRouterForkTest {
    Vm constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);
    address constant WALLET = 0xBf4777C71D2dbE842c36621C446C6F2b64f87233;
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address constant SWAP_ROUTER = 0xCaf681a66D020601342297493863E78C959E5cb2;
    address constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address constant WIKT = 0x4823c4542FE97E1C46d49d56b0D600ef1bCF624d;
    address constant MOO = 0xD9dB30BB0D2b8d2eae3826A1372117E058791e18;
    address constant PONS = 0x39dBED3a2bd333467115dE45665cC57F813C4571;
    address constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;

    CopyRouter router;

    function setUp() public {
        router = new CopyRouter(ISwapRouter02(SWAP_ROUTER), IPoolManager(POOL_MANAGER));
        vm.deal(WALLET, 1 ether);
    }

    function usdgEth() internal pure returns (PoolKey memory) {
        return PoolKey(address(0), USDG, 8388608, 10, 0x06a889870C8f83640D6816319f72e2aA579b6080);
    }

    function wiktEth() internal pure returns (PoolKey memory) {
        return PoolKey(address(0), WIKT, 0, 200, 0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044);
    }

    function leg4(PoolKey memory k, bool zf) internal pure returns (CopyRouter.Leg memory) {
        return CopyRouter.Leg(1, "", k, zf);
    }

    function leg3(bytes memory path) internal pure returns (CopyRouter.Leg memory) {
        return CopyRouter.Leg(0, path, PoolKey(address(0), address(0), 0, 0, address(0)), false);
    }

    /// USDG -> ETH (native v4) -> WIKT (native v4), then all the way back.
    function testNativeEthRouteRoundTrip() public {
        vm.startPrank(WALLET);
        IERC20(USDG).approve(address(router), type(uint256).max);
        CopyRouter.Leg[] memory buy = new CopyRouter.Leg[](2);
        buy[0] = leg4(usdgEth(), false); // USDG is currency1 -> oneForZero
        buy[1] = leg4(wiktEth(), true);  // ETH is currency0 -> zeroForOne
        uint256 before = IERC20(WIKT).balanceOf(WALLET);
        uint256 got = router.swap(buy, 100e6, 1, WALLET);
        require(got > 0 && IERC20(WIKT).balanceOf(WALLET) - before == got, "buy: no WIKT received");
        require(address(router).balance == 0, "router kept ETH after buy");

        IERC20(WIKT).approve(address(router), type(uint256).max);
        CopyRouter.Leg[] memory sell = new CopyRouter.Leg[](2);
        sell[0] = leg4(wiktEth(), false);
        sell[1] = leg4(usdgEth(), true);
        uint256 usdgBefore = IERC20(USDG).balanceOf(WALLET);
        uint256 back = router.swap(sell, got, 1, WALLET);
        require(IERC20(USDG).balanceOf(WALLET) - usdgBefore == back, "sell: USDG not paid");
        require(back > 85e6 && back < 100e6, "sell: round trip out of the expected 85-100 USDG range");
        require(address(router).balance == 0 && IERC20(WIKT).balanceOf(address(router)) == 0, "router kept funds");
        vm.stopPrank();
    }

    /// A route may not start or end in native ETH.
    function testRejectsNativeEndpoints() public {
        vm.startPrank(WALLET);
        IERC20(USDG).approve(address(router), type(uint256).max);
        CopyRouter.Leg[] memory legs = new CopyRouter.Leg[](1);
        legs[0] = leg4(usdgEth(), false); // ends in ETH
        (bool ok,) = address(router).call(abi.encodeCall(router.swap, (legs, 10e6, 1, WALLET)));
        require(!ok, "route ending in ETH must revert");
        vm.stopPrank();
    }

    /// Regression: ERC-20 v4 hooked pool (MOO via USDG/WETH v3 prefix is what the bot builds);
    /// here the simplest: v3 USDG->WETH->PONS path still works through the new contract.
    function testV3PathStillWorks() public {
        vm.startPrank(WALLET);
        IERC20(USDG).approve(address(router), type(uint256).max);
        CopyRouter.Leg[] memory buy = new CopyRouter.Leg[](1);
        buy[0] = leg3(abi.encodePacked(USDG, uint24(100), WETH, uint24(10000), PONS));
        uint256 got = router.swap(buy, 25e6, 1, WALLET);
        require(got > 0, "v3 buy failed");
        vm.stopPrank();
    }
}
