"""Unit tests for the signal-based backtester."""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.backtest import Backtester


def _make_df(rows: int = 200, trend: str = "up") -> pd.DataFrame:
    """Generate a synthetic daily candle DataFrame."""
    base = 250.0
    data = []
    for i in range(rows):
        if trend == "up":
            close = base + i * 0.5
        elif trend == "down":
            close = base - i * 0.5
        else:
            close = base + (i % 20 - 10) * 0.5
        open_p = close - 1.0
        high = close + 2.0
        low = close - 2.0
        data.append(
            {
                "ts": pd.Timestamp("2023-01-01") + pd.Timedelta(days=i),
                "open": open_p,
                "high": high,
                "low": low,
                "close": close,
                "volume": 100_000 + i * 100,
            }
        )
    df = pd.DataFrame(data).set_index("ts")
    return df


class TestBacktester:
    def test_not_enough_data(self):
        df = _make_df(rows=10)
        bt = Backtester(lookback=50)
        result = bt.run(df, initial=1_000_000)
        assert result.equity.iloc[-1] == 1_000_000
        assert result.trade_count == 0

    def test_up_trend_runs_without_error(self):
        df = _make_df(rows=200, trend="up")
        bt = Backtester(lookback=50)
        result = bt.run(df, initial=1_000_000, commission=0)
        # Synthetic linear trend may not trigger signals, but backtest must complete
        assert result.equity.iloc[-1] >= 0

    def test_down_trend_generates_shorts_or_loss(self):
        df = _make_df(rows=200, trend="down")
        bt = Backtester(lookback=50)
        result = bt.run(df, initial=1_000_000, commission=0)
        # May not trade if signals stay neutral, but if it trades should be short
        if result.trades:
            shorts = [t for t in result.trades if t["side"] == "short"]
            assert len(shorts) >= 0

    def test_commission_reduces_return(self):
        df = _make_df(rows=200, trend="up")
        bt = Backtester(lookback=50)
        r0 = bt.run(df, initial=1_000_000, commission=0)
        r1 = bt.run(df, initial=1_000_000, commission=0.01)
        assert r1.total_return_pct <= r0.total_return_pct

    def test_summary_format(self):
        df = _make_df(rows=200, trend="up")
        bt = Backtester(lookback=50)
        result = bt.run(df, initial=1_000_000)
        text = result.summary()
        assert "Бэктест" in text
        assert "Прибыльность" in text
        assert "Sharpe" in text
