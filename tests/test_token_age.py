import unittest
import time
from unittest.mock import patch
import bot

class TestTokenAgeFilter(unittest.TestCase):
    def setUp(self):
        bot.CFG["live"] = False
        bot.CFG["min_token_age_minutes"] = 30
        bot.CFG["min_trades_24h"] = 100
        bot.CFG["min_mcap_usd"] = 100000
        bot.CFG["min_origin_usd"] = 50
        bot.CFG["min_liquidity_usd"] = 1000
        bot.CFG["thin_liquidity_usd"] = 0
        bot.CFG["max_signal_age_s"] = 120
        bot.CFG["max_positions"] = 20
        bot.STATE["positions"] = {}
        bot.STATE["closed"] = []
        bot.STATE["paper_cash"] = 100000

    def make_event(self):
        return {
            "tx": "0xaaaabbbbccccddddeeeeffff0000111122223333444455556666777788889999",
            "wallet": "0x0121525f755c9e7bbc525bba6672716ab46ced57",
            "token": "0x1111111111111111111111111111111111111111",
            "amount": 1000.0,
            "block": 100,
            "t_detect": time.time(),
        }

    def test_fresh_token_skipped(self):
        ev = self.make_event()
        now = time.time()
        # Pair created 15 minutes ago (< 30 min)
        created_at = now - (15 * 60)
        fake_info = {
            "price": 1.0,
            "liquidity": 100000.0,
            "symbol": "FRESH",
            "buys24": 500,
            "sells24": 500,
            "pairs": [{"pairCreatedAt": int(created_at * 1000), "fdv": 500000}],
            "pair_created_at": created_at,
            "age_seconds": 15 * 60,
            "mcap": 500000.0,
        }

        with patch("bot.token_meta", return_value={"symbol": "FRESH", "decimals": 18}), \
             patch("bot.token_info", return_value=fake_info), \
             patch("bot.block_time", return_value=now), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, ev["token"], 1000 * 10**18)
            skip_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[skip]" in str(call_args)]
            self.assertTrue(any("token age 15.0m < min 30m" in s for s in skip_logs))

    def test_mature_token_passes(self):
        ev = self.make_event()
        now = time.time()
        # Pair created 60 minutes ago (>= 30 min)
        created_at = now - (60 * 60)
        fake_info = {
            "price": 1.0,
            "liquidity": 100000.0,
            "symbol": "MATURE",
            "buys24": 500,
            "sells24": 500,
            "pairs": [{"pairCreatedAt": int(created_at * 1000), "fdv": 500000}],
            "pair_created_at": created_at,
            "age_seconds": 60 * 60,
            "mcap": 500000.0,
        }

        with patch("bot.token_meta", return_value={"symbol": "MATURE", "decimals": 18}), \
             patch("bot.token_info", return_value=fake_info), \
             patch("bot.block_time", return_value=now), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.discover_route", return_value=([], [], "route", 10000)), \
             patch("bot.quote_check", return_value=(100, 0.01, -0.02)), \
             patch("bot.save_state"), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, ev["token"], 1000 * 10**18)
            skip_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[skip]" in str(call_args)]
            self.assertFalse(any("fresh launch" in s for s in skip_logs))

    def test_unknown_age_skipped_when_min_age_configured(self):
        ev = self.make_event()
        now = time.time()
        fake_info = {
            "price": 1.0,
            "liquidity": 100000.0,
            "symbol": "UNKNOWN",
            "buys24": 500,
            "sells24": 500,
            "pairs": [],
            "pair_created_at": None,
            "age_seconds": None,
            "mcap": 500000.0,
        }

        with patch("bot.token_meta", return_value={"symbol": "UNKNOWN", "decimals": 18}), \
             patch("bot.token_info", return_value=fake_info), \
             patch("bot.block_time", return_value=now), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, ev["token"], 1000 * 10**18)
            skip_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[skip]" in str(call_args)]
            self.assertTrue(any("unknown pair launch time" in s for s in skip_logs))

    def test_low_mcap_skipped(self):
        ev = self.make_event()
        now = time.time()
        created_at = now - (60 * 60)
        fake_info = {
            "price": 1.0,
            "liquidity": 100000.0,
            "symbol": "MICROMEME",
            "buys24": 500,
            "sells24": 500,
            "pairs": [{"pairCreatedAt": int(created_at * 1000), "fdv": 4000}],
            "pair_created_at": created_at,
            "age_seconds": 60 * 60,
            "mcap": 4000.0,
        }

        with patch("bot.token_meta", return_value={"symbol": "MICROMEME", "decimals": 18}), \
             patch("bot.token_info", return_value=fake_info), \
             patch("bot.block_time", return_value=now), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, ev["token"], 1000 * 10**18)
            skip_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[skip]" in str(call_args)]
            self.assertTrue(any("market cap $4,000.00 below min $100,000.00" in s for s in skip_logs))

    def test_low_trades_24h_skipped(self):
        ev = self.make_event()
        now = time.time()
        created_at = now - (60 * 60)
        fake_info = {
            "price": 1.0,
            "liquidity": 1000000.0,
            "symbol": "DEADPOOL",
            "buys24": 20,
            "sells24": 15,
            "pairs": [{"pairCreatedAt": int(created_at * 1000), "fdv": 500000}],
            "pair_created_at": created_at,
            "age_seconds": 60 * 60,
            "mcap": 500000.0,
        }

        with patch("bot.token_meta", return_value={"symbol": "DEADPOOL", "decimals": 18}), \
             patch("bot.token_info", return_value=fake_info), \
             patch("bot.block_time", return_value=now), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, ev["token"], 1000 * 10**18)
            skip_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[skip]" in str(call_args)]
            self.assertTrue(any("low activity: only 35 trades in 24h < min 100" in s for s in skip_logs))

if __name__ == "__main__":
    unittest.main()
