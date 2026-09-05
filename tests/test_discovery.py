import json
import pytest
from pathlib import Path
from auto_discovery import TraderDiscovery

def test_score_calculation():
    d = TraderDiscovery()
    
    # Trader A: Massive PnL and volume, verified
    trader_a = {
        "pnlUsd": 500000,
        "volumeUsd": 200000,
        "trades": 200,
        "followers": 5000,
        "verified": True,
    }
    score_a = d.calculate_score(trader_a)
    assert 70.0 <= score_a <= 100.0

    # Trader B: Zero PnL
    trader_b = {
        "pnlUsd": 0,
        "volumeUsd": 1000,
        "trades": 2,
        "followers": 10,
        "verified": False,
    }
    score_b = d.calculate_score(trader_b)
    assert score_b < 20.0
    assert score_a > score_b

def test_eligibility_filter():
    d = TraderDiscovery(min_trades=5, min_volume_usd=2000, min_pnl_usd=100)

    # Valid trader
    valid = {
        "address": "0x0a6ebed0155edb4b21d92ad02897a626cd90119e",
        "pnlUsd": 5000,
        "volumeUsd": 10000,
        "trades": 15,
    }
    assert d.is_eligible(valid) is True

    # Invalid address
    bad_addr = dict(valid, address="not-an-address")
    assert d.is_eligible(bad_addr) is False

    # Low trades
    low_trades = dict(valid, trades=2)
    assert d.is_eligible(low_trades) is False

    # Negative PnL
    neg_pnl = dict(valid, pnlUsd=-500)
    assert d.is_eligible(neg_pnl) is False

def test_export_wallets_json(tmp_path):
    d = TraderDiscovery()
    traders = [
        {
            "address": "0x0a6ebed0155edb4b21d92ad02897a626cd90119e",
            "label": "unipcs",
            "score": 98.5,
            "pnlUsd": 1500000,
            "volumeUsd": 3000000,
            "trades": 500,
            "windows_present": ["24h", "7d"],
        }
    ]
    out_file = tmp_path / "wallets.json"
    d.export_to_wallets_json(traders, output_path=out_file)

    assert out_file.exists()
    loaded = json.loads(out_file.read_text())
    assert len(loaded) == 1
    assert loaded[0]["address"] == "0x0a6ebed0155edb4b21d92ad02897a626cd90119e"
    assert loaded[0]["label"] == "unipcs"
    assert loaded[0]["score"] == 98.5

def test_hot_reload_detection(tmp_path, monkeypatch):
    import bot
    wallets_file = tmp_path / "wallets.json"
    wallets_file.write_text(json.dumps([{"address": "0x0a6ebed0155edb4b21d92ad02897a626cd90119e", "label": "unipcs"}]))
    
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr(bot, "_wallets_mtime", 0.0)
    monkeypatch.setattr(bot, "_last_wallets_check", 0.0)
    monkeypatch.setattr(bot, "WALLETS", {})
    
    bot.check_wallets_reload()
    assert "0x0a6ebed0155edb4b21d92ad02897a626cd90119e" in bot.WALLETS
    assert bot.WALLETS["0x0a6ebed0155edb4b21d92ad02897a626cd90119e"] == "unipcs"
