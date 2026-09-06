import unittest
import time
from unittest.mock import patch
import bot

class TestChineseTokenFilter(unittest.TestCase):
    def setUp(self):
        bot.CFG["live"] = False
        bot.CFG["min_token_age_minutes"] = 0
        bot.CFG["min_trades_24h"] = 0
        bot.CFG["min_mcap_usd"] = 150000
        bot.CFG["chinese_token_min_mcap_usd"] = 10000000
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
            "tx": "0x11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
            "wallet": "0x0121525f755c9e7bbc525bba6672716ab46ced57",
            "token": "0xd9acff3c2bb500d581a6bbe8f5a571a236353336",
            "amount": 100000.0,
            "block": 100,
            "t_detect": time.time(),
        }

    def test_is_chinese_token_detection(self):
        # Symbol with Chinese characters
        self.assertTrue(bot.is_chinese_token({"symbol": "富贵", "name": "富贵"}))
        # Name with Chinese characters but English symbol
        self.assertTrue(bot.is_chinese_token({"symbol": "FG", "name": "富贵代币"}))
        # Dex pair baseToken with Chinese
        self.assertTrue(bot.is_chinese_token(
            {"symbol": "TEST", "name": "Test"},
            {"symbol": "TEST", "pairs": [{"baseToken": {"symbol": "哈基米", "name": "Hajimi"}}]}
        ))
        # English token
        self.assertFalse(bot.is_chinese_token({"symbol": "ZEAL", "name": "Zeal Token"}))
        self.assertFalse(bot.is_chinese_token({"symbol": "GRASS", "name": "Grass"}))
        self.assertFalse(bot.is_chinese_token({"symbol": "BONER", "name": "Boner"}))

    def test_chinese_token_under_10m_skipped(self):
        ev = self.make_event()
        now = time.time()
        fake_info = {
            "price": 1.0,
            "liquidity": 100000.0,
            "symbol": "富贵",
            "buys24": 500,
            "sells24": 500,
            "pairs": [{"pairCreatedAt": int((now - 3600) * 1000), "fdv": 1640000}],
            "pair_created_at": now - 3600,
            "age_seconds": 3600,
            "mcap": 1640000.0,  # $1.64M < $10M
        }

        with patch("bot.token_meta", return_value={"symbol": "富贵", "name": "富贵", "decimals": 18}), \
             patch("bot.token_info", return_value=fake_info), \
             patch("bot.block_time", return_value=now), \
             patch("bot.balance_of", return_value=0), \
             patch("bot.honeypot_reason", return_value=None), \
             patch("bot.log") as mock_log:
            bot.handle_buy_signal(ev, ev["token"], 1000 * 10**18)
            skip_logs = [call_args[0][0] for call_args in mock_log.call_args_list if "[skip]" in str(call_args)]
            self.assertTrue(any("chinese token restricted: market cap $1,640,000.00 below min $10,000,000.00" in s for s in skip_logs))

    def test_chinese_token_above_10m_passes_chinese_filter(self):
        ev = self.make_event()
        now = time.time()
        fake_info = {
            "price": 0.012,
            "liquidity": 1000000.0,
            "symbol": "富贵",
            "buys24": 500,
            "sells24": 500,
            "pairs": [{"pairCreatedAt": int((now - 3600) * 1000), "fdv": 12000000}],
            "pair_created_at": now - 3600,
            "age_seconds": 3600,
            "mcap": 12000000.0,  # $12M >= $10M
        }

        with patch("bot.token_meta", return_value={"symbol": "富贵", "name": "富贵", "decimals": 18}), \
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
            self.assertFalse(any("chinese token restricted" in s for s in skip_logs))

    def test_english_token_not_affected_by_chinese_restriction(self):
        ev = self.make_event()
        now = time.time()
        fake_info = {
            "price": 1.0,
            "liquidity": 100000.0,
            "symbol": "ZEAL",
            "buys24": 500,
            "sells24": 500,
            "pairs": [{"pairCreatedAt": int((now - 3600) * 1000), "fdv": 1900000}],
            "pair_created_at": now - 3600,
            "age_seconds": 3600,
            "mcap": 1900000.0,  # $1.9M (< $10M, but NOT Chinese, and >= $150k min_mcap)
        }

        with patch("bot.token_meta", return_value={"symbol": "ZEAL", "name": "Zeal Token", "decimals": 18}), \
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
            self.assertFalse(any("chinese token restricted" in s for s in skip_logs))

if __name__ == "__main__":
    unittest.main()
