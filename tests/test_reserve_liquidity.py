import unittest
import time
from unittest.mock import patch
import bot

class TestReserveLiquidity(unittest.TestCase):
    def setUp(self):
        bot.CFG["live"] = False
        bot.CFG["min_token_age_minutes"] = 0
        bot.CFG["min_trades_24h"] = 0
        bot.CFG["min_mcap_usd"] = 0
        bot.CFG["chinese_token_min_mcap_usd"] = 0
        bot.CFG["min_liquidity_usd"] = 10000
        bot.CFG["min_reserve_liquidity_usd"] = 10000
        bot.CFG["min_origin_usd"] = 50
        bot.CFG["thin_liquidity_usd"] = 0
        bot.CFG["max_signal_age_s"] = 120
        bot.CFG["max_positions"] = 20
        bot.STATE["positions"] = {}
        bot.STATE["closed"] = []
        bot.STATE["paper_cash"] = 100000

    def make_event(self, tok_addr):
        return {
            "tx": "0x" + "aa" * 32,
            "wallet": "0x0121525f755c9e7bbc525bba6672716ab46ced57",
            "token": tok_addr,
            "amount": 1000.0,
            "block": 100,
            "t_detect": time.time(),
        }

    def test_fake_liquidity_pair_rejected(self):
        tok_addr = "0x" + "1c" * 20
        ev = self.make_event(tok_addr)
        now = time.time()
        # Simulated DexScreener response:
        # Pair 1: CLIT/LIT with fake $114k liquidity (LIT is non-reserve token)
        # Pair 2: CLIT/USDG with real $340 liquidity
        fake_pairs = [
            {
                "chainId": "robinhood",
                "pairAddress": "0x" + "7d" * 20,
                "baseToken": {"address": tok_addr, "symbol": "CLIT"},
                "quoteToken": {"address": "0x" + "ee" * 20, "symbol": "LIT"},
                "priceUsd": "0.0002188",
                "liquidity": {"usd": 114377.0},
                "fdv": 218809.0,
                "txns": {"h24": {"buys": 500, "sells": 500}},
            },
            {
                "chainId": "robinhood",
                "pairAddress": "0x" + "80" * 20,
                "baseToken": {"address": tok_addr, "symbol": "CLIT"},
                "quoteToken": {"address": bot.USDG, "symbol": "USDG"},
                "priceUsd": "1.0",
                "liquidity": {"usd": 340.0},
                "fdv": 218849.0,
                "txns": {"h24": {"buys": 10, "sells": 10}},
            }
        ]

        with patch("bot.dex_get", return_value={"pairs": fake_pairs}):
            info = bot.token_info(tok_addr, fresh=True)
            self.assertEqual(info["liquidity"], 340.0)
            self.assertEqual(info["reserve_liquidity"], 340.0)

        with patch("bot.token_meta", return_value={"symbol": "CLIT", "name": "Clit", "decimals": 18}), \
             patch("bot.token_info", return_value=info), \
             patch("bot.block_time", return_value=now), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, tok_addr, 100000 * 10**18)
            skip_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[skip]" in str(call_args)]
            self.assertTrue(any("reserve liquidity $340.00 in USDG/ETH below min $10,000.00" in s for s in skip_logs))

    def test_genuine_reserve_liquidity_passes(self):
        tok_addr = "0x" + "9f" * 20
        ev = self.make_event(tok_addr)
        now = time.time()
        fake_pairs = [
            {
                "chainId": "robinhood",
                "pairAddress": "0x" + "11" * 20,
                "baseToken": {"address": tok_addr, "symbol": "ZEAL"},
                "quoteToken": {"address": bot.ZERO, "symbol": "ETH"},
                "priceUsd": "0.00193",
                "liquidity": {"usd": 129000.0},
                "fdv": 1970000.0,
                "txns": {"h24": {"buys": 2000, "sells": 2000}},
            }
        ]

        with patch("bot.dex_get", return_value={"pairs": fake_pairs}):
            info = bot.token_info(tok_addr, fresh=True)
            self.assertEqual(info["liquidity"], 129000.0)
            self.assertEqual(info["reserve_liquidity"], 129000.0)

        with patch("bot.token_meta", return_value={"symbol": "ZEAL", "name": "Zeal", "decimals": 18}), \
             patch("bot.token_info", return_value=info), \
             patch("bot.block_time", return_value=now), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.discover_route", return_value=([], [], "route", 10000)), \
             patch("bot.quote_check", return_value=(100, 0.01, -0.02)), \
             patch("bot.save_state"), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, tok_addr, 100000 * 10**18)
            skip_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[skip]" in str(call_args)]
            self.assertFalse(any("reserve liquidity" in s for s in skip_logs))

    def test_dex_pairs_prioritizes_reserve_tokens(self):
        tok_addr = "0x" + "ee" * 20
        fake_pairs = [
            {
                "chainId": "robinhood",
                "dexId": "uniswap",
                "labels": ["v4"],
                "pairAddress": "0x" + "22" * 20,
                "baseToken": {"address": tok_addr, "symbol": "TEST"},
                "quoteToken": {"address": "0x" + "33" * 20, "symbol": "SHIT"},
                "liquidity": {"usd": 100000.0},
            },
            {
                "chainId": "robinhood",
                "dexId": "uniswap",
                "labels": ["v4"],
                "pairAddress": "0x" + "44" * 20,
                "baseToken": {"address": tok_addr, "symbol": "TEST"},
                "quoteToken": {"address": bot.USDG, "symbol": "USDG"},
                "liquidity": {"usd": 20000.0},
            }
        ]

        with patch("bot.token_info", return_value={"pairs": fake_pairs}):
            dp = bot.dex_pairs(tok_addr, "v4")
            self.assertEqual(len(dp), 2)
            # The USDG pair must come first despite lower nominal liquidity than the shitcoin pair
            self.assertEqual(dp[0][0].lower(), bot.USDG.lower())
            self.assertEqual(dp[1][0].lower(), ("0x" + "33" * 20).lower())

if __name__ == "__main__":
    unittest.main()
