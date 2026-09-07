import unittest
import time
from unittest.mock import patch
import bot
from auto_discovery import TraderDiscovery


class TestPayoffAndFrequencyFilter(unittest.TestCase):
    def setUp(self):
        self.discovery = TraderDiscovery(
            min_trades=5,
            max_trades=600,
            min_volume_usd=2000.0,
            min_pnl_usd=100.0,
            min_payoff_ratio=0.8,
            trenches_min_win_rate=0.40,
        )

    def test_bad_payoff_ratio_rejected(self):
        # Like Quanterty: 62% win rate, but worst loss (-$54k) dwarfs best win ($15k) -> payoff 0.29 < 0.8
        trader = {
            "address": "0x" + "1" * 40,
            "label": "Quanterty",
            "pnlUsd": 15913.0,
            "realized_pnl": 15913.0,
            "volumeUsd": 500000.0,
            "trades": 120,
            "closed_trades": 8,
            "win_rate": 0.62,
            "best_trade": 15867.0,
            "worst_trade": -54565.0,
            "payoff_ratio": 0.29,
        }
        self.assertFalse(self.discovery.is_eligible(trader))

    def test_good_payoff_ratio_accepted(self):
        # Like remusofmars: 56% win rate, payoff 3.27
        trader = {
            "address": "0x" + "4" * 40,
            "label": "remusofmars",
            "pnlUsd": 38450.0,
            "realized_pnl": 38450.0,
            "volumeUsd": 300000.0,
            "trades": 252,
            "closed_trades": 79,
            "win_rate": 0.56,
            "best_trade": 11644.0,
            "worst_trade": -3559.0,
            "payoff_ratio": 3.27,
        }
        self.assertTrue(self.discovery.is_eligible(trader))

    def test_high_payoff_ratio_lower_winrate_accepted(self):
        # Lower win rate (38% < 40%), but exceptional payoff ratio (2.62 >= 2.0)
        trader = {
            "address": "0x" + "7" * 40,
            "label": "dylansdegens",
            "pnlUsd": 14661.0,
            "realized_pnl": 14661.0,
            "volumeUsd": 150000.0,
            "trades": 74,
            "closed_trades": 20,
            "win_rate": 0.38,
            "best_trade": 26514.0,
            "worst_trade": -10102.0,
            "payoff_ratio": 2.62,
        }
        self.assertTrue(self.discovery.is_eligible(trader))

    def test_high_frequency_bot_rejected(self):
        # Excessive fills / trades > max_trades (600)
        trader = {
            "address": "0x" + "5" * 40,
            "label": "MEV_Bot",
            "pnlUsd": 25000.0,
            "realized_pnl": 25000.0,
            "volumeUsd": 800000.0,
            "trades": 1250,
            "closed_trades": 100,
            "win_rate": 0.70,
            "payoff_ratio": 1.5,
        }
        self.assertFalse(self.discovery.is_eligible(trader))

    def test_negative_realized_pnl_rejected(self):
        # Like unipcs: 64% win rate, but -$223k realized
        trader = {
            "address": "0x" + "6" * 40,
            "label": "unipcs",
            "pnlUsd": 228683.0,
            "realized_pnl": -223387.0,
            "volumeUsd": 1000000.0,
            "trades": 308,
            "closed_trades": 25,
            "win_rate": 0.64,
            "payoff_ratio": 0.01,
        }
        self.assertFalse(self.discovery.is_eligible(trader))


class TestMinOriginUsdGuard(unittest.TestCase):
    def setUp(self):
        bot.CFG["live"] = False
        bot.CFG["min_origin_usd"] = 500
        bot.CFG["min_token_age_minutes"] = 0
        bot.CFG["min_trades_24h"] = 0
        bot.CFG["min_mcap_usd"] = 0
        bot.CFG["chinese_token_min_mcap_usd"] = 0
        bot.CFG["min_liquidity_usd"] = 0
        bot.CFG["min_reserve_liquidity_usd"] = 0
        bot.CFG["max_signal_age_s"] = 120
        bot.CFG["max_positions"] = 20
        bot.STATE["positions"] = {}
        bot.STATE["closed"] = []
        bot.STATE["paper_cash"] = 100000

    def make_event(self, token):
        return {
            "tx": "0x" + "a" * 64,
            "wallet": "0x" + "8" * 40,
            "token": token,
            "amount": 1000.0,
            "block": 100,
            "t_detect": time.time(),
        }

    @patch("bot.token_meta")
    @patch("bot.token_info")
    @patch("bot.balance_of", return_value=0)
    @patch("bot.block_time", return_value=time.time())
    @patch("bot.append_jsonl")
    def test_origin_buy_below_500_skipped(self, mock_append, mock_bt, mock_bal, mock_info, mock_meta):
        token = "0x" + "2" * 40
        ev = self.make_event(token)
        mock_meta.return_value = {"symbol": "ALPHA", "decimals": 18}
        # Leader bought 1000 tokens @ $0.30 = $300 origin_usd (< $500 min)
        mock_info.return_value = {
            "price": 0.30,
            "liquidity": 50000.0,
            "reserve_liquidity": 50000.0,
            "mcap": 500000.0,
            "pair_created_at": time.time() - 3600,
            "buys24": 200,
            "sells24": 150,
        }

        bot.handle_buy_signal(ev, token, 1000 * 10**18)
        self.assertNotIn(token.lower(), bot.STATE["positions"])
        self.assertTrue(mock_append.called)
        sig = mock_append.call_args[0][1]
        self.assertIn("origin buy only $300.00", sig["outcome"])

    @patch("bot.token_meta")
    @patch("bot.token_info")
    @patch("bot.discover_route")
    @patch("bot.quote_check")
    @patch("bot.balance_of", return_value=0)
    @patch("bot.block_time", return_value=time.time())
    @patch("bot.save_state")
    def test_origin_buy_above_500_proceeds(self, mock_save, mock_bt, mock_bal, mock_quote, mock_route, mock_info, mock_meta):
        token = "0x" + "3" * 40
        ev = self.make_event(token)
        mock_meta.return_value = {"symbol": "BETA", "decimals": 18}
        # Leader bought 1000 tokens @ $0.60 = $600 origin_usd (>= $500 min)
        mock_info.return_value = {
            "price": 0.60,
            "liquidity": 50000.0,
            "reserve_liquidity": 50000.0,
            "mcap": 500000.0,
            "pair_created_at": time.time() - 3600,
            "buys24": 200,
            "sells24": 150,
        }
        mock_route.return_value = ([], [], "route_desc", 10000)
        mock_quote.return_value = (100 * 10**18, 0.01, -0.02)

        bot.handle_buy_signal(ev, token, 1000 * 10**18)
        self.assertIn(token.lower(), bot.STATE["positions"])

if __name__ == "__main__":
    unittest.main()
