import unittest
import time
from unittest.mock import patch, MagicMock
import bot

class TestWhaleRatioAndPreQuote(unittest.TestCase):
    def setUp(self):
        bot.CFG["min_origin_usd"] = 500
        bot.CFG["min_reserve_liquidity_usd"] = 10000
        bot.CFG["max_origin_to_reserve_ratio_pct"] = 5.0
        bot.CFG["sell_pre_quote_enabled"] = True
        bot.CFG["max_sell_price_impact_pct"] = 12.0
        bot.CFG["sell_stress_test_multiplier"] = 1.5
        bot.CFG["max_sell_stress_impact_pct"] = 20.0
        bot.CFG["live"] = False
        bot.CFG["buy_usd"] = 50
        bot.CFG["min_token_age_minutes"] = 0
        bot.CFG["min_trades_24h"] = 0
        bot.CFG["min_mcap_usd"] = 0
        bot.CFG["max_positions"] = 20
        bot.STATE["positions"] = {}
        bot.STATE["closed"] = []
        bot.STATE["paper_cash"] = 100000

    def test_whale_ratio_blocks_high_ratio(self):
        # Case like p402: reserve_liquidity = $42,861, single-sided reserve = $21,430
        # Whale buys $1,710 -> 1,710 / 21,430 = 7.98% > 5.0% -> skip!
        tok_addr = "0x" + "aa" * 20
        ev = {
            "tx": "0x" + "11" * 32,
            "wallet": "0x0121525f755c9e7bbc525bba6672716ab46ced57",
            "token": tok_addr,
            "amount": 1000.0 * 10**18,
            "block": 100,
            "t_detect": time.time(),
        }
        info = {
            "price": 1.71033, # origin_usd = $1,710.33
            "liquidity": 42861.0,
            "reserve_liquidity": 42861.0,
            "buys24": 500,
            "sells24": 400,
            "pair_created_at": time.time() - 3600,
            "mcap": 200000.0,
        }
        with patch("bot.token_meta", return_value={"symbol": "P402", "name": "P402", "decimals": 18}), \
             patch("bot.token_info", return_value=info), \
             patch("bot.block_time", return_value=time.time()), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, tok_addr, 1000 * 10**18)
            skip_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[skip]" in str(call_args)]
            self.assertTrue(any("high dump slippage risk" in s for s in skip_logs))

    def test_whale_ratio_allows_healthy_ratio(self):
        # Case like PRISM: reserve_liquidity = $203,318, single-sided reserve = $101,659
        # Whale buys $945 -> 945 / 101,659 = 0.93% <= 5.0% -> passes ratio check!
        tok_addr = "0x" + "bb" * 20
        ev = {
            "tx": "0x" + "22" * 32,
            "wallet": "0x0121525f755c9e7bbc525bba6672716ab46ced57",
            "token": tok_addr,
            "amount": 1000.0 * 10**18,
            "block": 100,
            "t_detect": time.time(),
        }
        info = {
            "price": 0.94592, # origin_usd = $945.92
            "liquidity": 203318.0,
            "reserve_liquidity": 203318.0,
            "buys24": 500,
            "sells24": 400,
            "pair_created_at": time.time() - 3600,
            "mcap": 1500000.0,
        }
        with patch("bot.token_meta", return_value={"symbol": "PRISM", "name": "PRISM", "decimals": 18}), \
             patch("bot.token_info", return_value=info), \
             patch("bot.block_time", return_value=time.time()), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.discover_route", return_value=([], [], "v4 pool", 200000)), \
             patch("bot.quote_check", return_value=(1000 * 10**18, 0.001, -0.01)), \
             patch("bot.quote_route", side_effect=[49.5 * 10**6, 0.495 * 10**6, 74.0 * 10**6]), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, tok_addr, 1000 * 10**18)
            buy_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[BUY" in str(call_args)]
            self.assertTrue(any("PRISM" in s for s in buy_logs))

    def test_honeypot_reason_tightened(self):
        # 1. 20 buys, 1 sell -> near-zero sell honeypot
        self.assertIn("near-zero sell honeypot", bot.honeypot_reason({"buys24": 20, "sells24": 1}))
        # 2. 25 buys, 2 sells (ratio 12.5x > 8x) -> ratio skew near-unsellable
        self.assertIn("ratio 12.5x > 8x", bot.honeypot_reason({"buys24": 25, "sells24": 2}))
        # 3. 50 buys, 10 sells (ratio 5x <= 8x) -> healthy, None
        self.assertIsNone(bot.honeypot_reason({"buys24": 50, "sells24": 10}))

    def test_sell_pre_quote_blocks_revert(self):
        # When sell quote reverts (like STARK / unroutable / drained pool), order is skipped
        tok_addr = "0x" + "cc" * 20
        ev = {
            "tx": "0x" + "33" * 32,
            "wallet": "0x0121525f755c9e7bbc525bba6672716ab46ced57",
            "token": tok_addr,
            "amount": 1000.0 * 10**18,
            "block": 100,
            "t_detect": time.time(),
        }
        info = {
            "price": 1.0,
            "liquidity": 100000.0,
            "reserve_liquidity": 100000.0,
            "buys24": 100,
            "sells24": 100,
            "pair_created_at": time.time() - 3600,
            "mcap": 1000000.0,
        }
        with patch("bot.token_meta", return_value={"symbol": "STARK", "name": "STARK", "decimals": 18}), \
             patch("bot.token_info", return_value=info), \
             patch("bot.block_time", return_value=time.time()), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.discover_route", return_value=([], [], "v4 pool", 100000)), \
             patch("bot.quote_check", return_value=(1000 * 10**18, 0.001, -0.01)), \
             patch("bot.quote_route", side_effect=RuntimeError("NotEnoughLiquidity")), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, tok_addr, 1000 * 10**18)
            skip_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[skip]" in str(call_args)]
            self.assertTrue(any("sell pre-quote reverted" in s for s in skip_logs))

if __name__ == "__main__":
    unittest.main()
