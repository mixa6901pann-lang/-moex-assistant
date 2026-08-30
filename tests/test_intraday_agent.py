import pandas as pd
import numpy as np
from core.intraday_agent import IntradayAgent

agent = IntradayAgent()

def test_technical_signal_bounce_up():
    # Setup data to trigger bounce_up
    # price is near daily_low, rsi is oversold
    price = 100.0
    daily_low = 99.5  # dist = 0.5% < 1.0%
    daily_high = 110.0
    rsi = 30  # < 35
    vwap = 101.0
    vol_ratio = 1.0
    adx = 20.0

    df = pd.DataFrame({"close": [100.0], "bb_pct": [0.05]}) # bb_oversold < 0.1

    res = agent._technical_signal(
        df=df,
        price=price,
        daily_high=daily_high,
        daily_low=daily_low,
        vwap=vwap,
        rsi=rsi,
        vol_ratio=vol_ratio,
        adx=adx,
        prev_close=99.0,
        prev_high=101.0,
        prev_low=98.0,
        current_high=100.5,
        current_low=99.8,
        metrics={},
    )

    assert res["signal"] == "bounce_up"
    assert res["direction"] == "long"
    assert res["confidence"] > 0
    assert "near_daily_low" in res["signals_used"]
    assert "rsi_oversold" in res["signals_used"]

def test_technical_signal_bounce_down():
    # Setup data to trigger bounce_down
    price = 110.0
    daily_high = 110.5 # dist = 0.45% < 1.5%
    daily_low = 100.0
    rsi = 65  # > 60
    vwap = 109.0
    vol_ratio = 1.0
    adx = 20.0

    df = pd.DataFrame({"close": [110.0], "bb_pct": [0.95]}) # bb_overbought > 0.9

    res = agent._technical_signal(
        df=df,
        price=price,
        daily_high=daily_high,
        daily_low=daily_low,
        vwap=vwap,
        rsi=rsi,
        vol_ratio=vol_ratio,
        adx=adx,
        prev_close=109.0,
        prev_high=111.0,
        prev_low=108.0,
        current_high=110.2,
        current_low=109.8,
        metrics={},
    )

    assert res["signal"] == "bounce_down"
    assert res["direction"] == "short"
    assert res["confidence"] > 0
    assert "near_daily_high" in res["signals_used"]
    assert "rsi_overbought" in res["signals_used"]

def test_technical_signal_no_signal():
    # Balanced data
    price = 105.0
    daily_high = 120.0
    daily_low = 90.0
    rsi = 50
    vwap = 105.0
    vol_ratio = 1.0
    adx = 20.0

    df = pd.DataFrame({"close": [105.0], "bb_pct": [0.5]})

    res = agent._technical_signal(
        df=df,
        price=price,
        daily_high=daily_high,
        daily_low=daily_low,
        vwap=vwap,
        rsi=rsi,
        vol_ratio=vol_ratio,
        adx=adx,
        prev_close=104.0,
        prev_high=106.0,
        prev_low=103.0,
        current_high=105.5,
        current_low=104.5,
        metrics={},
    )

    assert res["signal"] == "no_signal"
    assert res["direction"] == "neutral"
    assert res["confidence"] == 0

def test_calibrate_confidence():
    # Test confidence reduction
    initial_conf = 80

    # 1. Few candles (< 20) -> max 70
    conf_few = agent._calibrate_confidence(initial_conf, 10, None, {})
    assert conf_few <= 70

    # 2. No order book -> max 60
    conf_no_ob = agent._calibrate_confidence(initial_conf, 30, None, {})
    assert conf_no_ob <= 60

    # 3. High spread (> 0.3%) -> -15
    metrics = {"spread_pct": 0.5}
    conf_high_spread = agent._calibrate_confidence(initial_conf, 30, {"error": False}, metrics)
    assert conf_high_spread == 80 - 15
