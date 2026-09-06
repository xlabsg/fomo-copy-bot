import unittest
import time
from unittest.mock import patch
import bot

class TestRiskControl(unittest.TestCase):
    def setUp(self):
        bot.CFG["risk_control"] = {
            "enabled": True,
            "hard_stop_loss_pct": -45.0,
            "breakeven_trigger_pct": 50.0,
            "breakeven_stop_pct": 0.0,
            "price_check_interval": 0.0,  # instant checks in test
        }
        bot.CFG["live"] = False
        bot.STATE["positions"] = {}
        bot.STATE["closed"] = []

    def make_pos(self, symbol="TEST", buy_usd=100.0, initial_raw=1000000):
        return {
            "token": "0x1111111111111111111111111111111111111111",
            "symbol": symbol,
            "decimals": 18,
            "bought_at": time.time() - 60,
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

    def test_hard_stop_loss_triggered(self):
        pos = self.make_pos("RUG", buy_usd=100.0, initial_raw=10**18)
        now = time.time()
        
        # Simulate a quote of $50 (loss of -50%, which is <= -45%)
        quote_usdg = 50 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=quote_usdg), \
             patch("bot.sell") as mock_sell, \
             patch("bot.save_state"):
            triggered = bot.check_position_risk(pos, now)
            self.assertTrue(triggered)
            mock_sell.assert_called_once()
            args, _ = mock_sell.call_args
            self.assertEqual(args[0]["symbol"], "RUG")
            self.assertEqual(args[1], pos["remaining_raw"])
            self.assertIn("hard stop loss", args[2])
            self.assertTrue(pos["origin_done"])

    def test_normal_dip_survives(self):
        pos = self.make_pos("GEM", buy_usd=100.0, initial_raw=10**18)
        now = time.time()
        
        # Simulate a quote of $75 (loss of -25%, which is > -45%)
        quote_usdg = 75 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=quote_usdg), \
             patch("bot.sell") as mock_sell:
            triggered = bot.check_position_risk(pos, now)
            self.assertFalse(triggered)
            mock_sell.assert_not_called()
            self.assertFalse(pos["origin_done"])
            self.assertEqual(pos["peak_ret_pct"], 0.0)
            self.assertFalse(pos["breakeven_active"])

    def test_breakeven_activation_and_trigger(self):
        pos = self.make_pos("MOON", buy_usd=100.0, initial_raw=10**18)
        now = time.time()
        
        # Step 1: Price surges to $160 (+60% profit >= +50% trigger)
        quote_surge = 160 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=quote_surge), \
             patch("bot.sell") as mock_sell, \
             patch("bot.save_state"):
            triggered = bot.check_position_risk(pos, now)
            self.assertFalse(triggered)
            mock_sell.assert_not_called()
            self.assertEqual(pos["peak_ret_pct"], 60.0)
            self.assertTrue(pos["breakeven_active"])
            
        # Step 2: Price drops to $120 (+20% profit, above breakeven 0%)
        now += 10
        quote_pullback = 120 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=quote_pullback), \
             patch("bot.sell") as mock_sell:
            triggered = bot.check_position_risk(pos, now)
            self.assertFalse(triggered)
            mock_sell.assert_not_called()
            self.assertEqual(pos["peak_ret_pct"], 60.0)
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
            self.assertIn("peak +60.0%", args[2])
            self.assertTrue(pos["origin_done"])

    def test_risk_control_disabled(self):
        bot.CFG["risk_control"]["enabled"] = False
        pos = self.make_pos("DUMP", buy_usd=100.0, initial_raw=10**18)
        now = time.time()
        # Even with -90% dump, risk control should return False when disabled
        quote_dump = 10 * 10**bot.USDG_DEC
        with patch("bot.quote_route", return_value=quote_dump), \
             patch("bot.sell") as mock_sell:
            triggered = bot.check_position_risk(pos, now)
            self.assertFalse(triggered)
            mock_sell.assert_not_called()

if __name__ == "__main__":
    unittest.main()
