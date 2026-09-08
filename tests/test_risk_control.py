import unittest
import time
from unittest.mock import patch
import bot

class TestRiskControl(unittest.TestCase):
    def setUp(self):
        bot.CFG["risk_control"] = {
            "enabled": True,
            "hard_stop_loss_enabled": True,
            "hard_stop_loss_pct": -45.0,
            "breakeven_enabled": True,
            "breakeven_trigger_pct": 35.0,
            "breakeven_stop_pct": 0.0,
            "stagnant_timeout_hours": 6.0,
            "stagnant_max_txns_24h": 80,
            "price_check_interval": 0.0,
        }
        bot.CFG["live"] = False
        bot.STATE["positions"] = {}
        bot.STATE["closed"] = []

    def make_pos(self, symbol="TEST", buy_usd=100.0, initial_raw=1000000, age_seconds=60):
        return {
            "token": "0x1111111111111111111111111111111111111111",
            "symbol": symbol,
            "decimals": 18,
            "bought_at": time.time() - age_seconds,
            "buy_usd": buy_usd,
            "initial_raw": initial_raw,
            "remaining_raw": initial_raw,
            "legs_sell": [],
            "stages_done": [],
            "origin_exiting": False,
            "origin_done": False,
            "usdg_out": 0.0,
            "sells": [],
            "paper": True,
            "peak_ret_pct": 0.0,
            "breakeven_active": False,
            "_last_price_check": 0.0,
        }

    def test_hard_stop_loss_triggered_when_enabled(self):
        bot.CFG["risk_control"]["hard_stop_loss_enabled"] = True
        pos = self.make_pos("RUG", buy_usd=100.0, initial_raw=10**18)
        now = time.time()
        
        quote_usdg = 50 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=quote_usdg), \
             patch("bot.sell") as mock_sell, \
             patch("bot.save_state"):
            triggered = bot.check_position_risk(pos, now)
            self.assertTrue(triggered)
            mock_sell.assert_called_once()
            args, _ = mock_sell.call_args
            self.assertEqual(args[0]["symbol"], "RUG")
            self.assertIn("hard stop loss", args[2])
            self.assertTrue(pos["origin_done"])

    def test_hard_stop_loss_ignored_when_disabled(self):
        bot.CFG["risk_control"]["hard_stop_loss_enabled"] = False
        pos = self.make_pos("DIP", buy_usd=100.0, initial_raw=10**18)
        now = time.time()
        
        # -50% loss
        quote_usdg = 50 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=quote_usdg), \
             patch("bot.sell") as mock_sell:
            triggered = bot.check_position_risk(pos, now)
            self.assertFalse(triggered)
            mock_sell.assert_not_called()
            self.assertFalse(pos["origin_done"])

    def test_breakeven_activation_and_trigger(self):
        pos = self.make_pos("MOON", buy_usd=100.0, initial_raw=10**18)
        now = time.time()
        
        # Step 1: Price surges to $140 (+40% profit >= +35% trigger)
        quote_surge = 140 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=quote_surge), \
             patch("bot.sell") as mock_sell, \
             patch("bot.save_state"):
            triggered = bot.check_position_risk(pos, now)
            self.assertFalse(triggered)
            mock_sell.assert_not_called()
            self.assertEqual(pos["peak_ret_pct"], 40.0)
            self.assertTrue(pos["breakeven_active"])
            
        # Step 2: Price drops to $110 (+10% profit, above breakeven 0%)
        now += 10
        quote_pullback = 110 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=quote_pullback), \
             patch("bot.sell") as mock_sell:
            triggered = bot.check_position_risk(pos, now)
            self.assertFalse(triggered)
            mock_sell.assert_not_called()
            self.assertTrue(pos["breakeven_active"])
            
        # Step 3: Price drops to $99 (-1%, <= breakeven 0%)
        now += 20
        quote_dump = 99 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=quote_dump), \
             patch("bot.sell") as mock_sell, \
             patch("bot.save_state"):
            triggered = bot.check_position_risk(pos, now)
            self.assertTrue(triggered)
            mock_sell.assert_called_once()
            args, _ = mock_sell.call_args
            self.assertIn("breakeven stop", args[2])
            self.assertTrue(pos["origin_done"])

    def test_stagnant_cleanup_triggered(self):
        # Position held for 7 hours (> 6h timeout)
        pos = self.make_pos("DEAD", buy_usd=100.0, initial_raw=10**18, age_seconds=7 * 3600)
        now = time.time()
        
        # Token has only 20 trades in 24h (<= 80 max stagnant txns)
        fake_info = {
            "price": 1.0,
            "buys24": 12,
            "sells24": 8,
        }
        with patch("bot.token_info", return_value=fake_info), \
             patch("bot.sell") as mock_sell, \
             patch("bot.save_state"):
            triggered = bot.check_position_risk(pos, now)
            self.assertTrue(triggered)
            mock_sell.assert_called_once()
            args, _ = mock_sell.call_args
            self.assertIn("stagnant cleanup", args[2])
            self.assertTrue(pos["origin_done"])

    def test_stagnant_cleanup_spares_active_tokens(self):
        # Position held for 7 hours (> 6h timeout), but token has 5000 trades in 24h
        pos = self.make_pos("ACTIVE", buy_usd=100.0, initial_raw=10**18, age_seconds=7 * 3600)
        now = time.time()
        
        fake_info = {
            "price": 1.0,
            "buys24": 2500,
            "sells24": 2500,
        }
        quote_usdg = 100 * 10**bot.USDG_DEC
        with patch("bot.token_info", return_value=fake_info), \
             patch("bot.quote_route", return_value=quote_usdg), \
             patch("bot.sell") as mock_sell:
            triggered = bot.check_position_risk(pos, now)
            self.assertFalse(triggered)
            mock_sell.assert_not_called()

    def test_trailing_stop_tiers(self):
        bot.CFG["risk_control"]["trailing_stop_enabled"] = True
        pos = self.make_pos("TRAIL", buy_usd=100.0, initial_raw=10**18)
        now = time.time()

        # Step 1: surges +40% -> locks in +15%
        q1 = 140 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=q1), patch("bot.sell") as mock_sell, patch("bot.save_state"):
            bot.check_position_risk(pos, now)
            self.assertEqual(pos["trailing_stop_pct"], 15.0)

        # Step 2: surges +70% -> locks in +35%
        now += 10
        q2 = 170 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=q2), patch("bot.sell") as mock_sell, patch("bot.save_state"):
            bot.check_position_risk(pos, now)
            self.assertEqual(pos["trailing_stop_pct"], 35.0)

        # Step 3: drops to +30% (<= +35% lock line) -> triggers trailing stop
        now += 10
        q3 = 130 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=q3), patch("bot.sell") as mock_sell, patch("bot.save_state"):
            triggered = bot.check_position_risk(pos, now)
            self.assertTrue(triggered)
            mock_sell.assert_called_once()
            args, _ = mock_sell.call_args
            self.assertIn("trailing stop", args[2])

    def test_trailing_stop_super_pump(self):
        # Like PRISM +693%: trails 25% from peak
        bot.CFG["risk_control"]["trailing_stop_enabled"] = True
        pos = self.make_pos("PRISM", buy_usd=100.0, initial_raw=10**18)
        now = time.time()

        # Surges to +200% -> stop line raised to 200 - 25 = 175%
        q1 = 300 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=q1), patch("bot.sell") as mock_sell, patch("bot.save_state"):
            bot.check_position_risk(pos, now)
            self.assertEqual(pos["trailing_stop_pct"], 175.0)

        # Pulls back to +170% (<= 175%) -> trailing stop locks in +175% profit!
        now += 10
        q2 = 270 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=q2), patch("bot.sell") as mock_sell, patch("bot.save_state"):
            triggered = bot.check_position_risk(pos, now)
            self.assertTrue(triggered)
            mock_sell.assert_called_once()
            args, _ = mock_sell.call_args
            self.assertIn("trailing stop", args[2])

if __name__ == "__main__":
    unittest.main()
