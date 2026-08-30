"""Simple signal-based backtester for MOEX strategies.

Runs detect_signals + recommend_direction on historical candles,
simulates daily-rebalanced long/short/neutral exposure,
and reports equity curve + basic stats.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategies.indicators import add_indicators, detect_signals, score_stock
from strategies.signals import recommend_direction


def _last_values(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    return {
        "rsi": last.get("rsi", 50) or 50,
        "macd_hist": last.get("macd_hist", 0) or 0,
        "bb_pct": last.get("bb_pct", 0.5) or 0.5,
        "sma_20": last.get("sma_20", 0) or 0,
        "sma_50": last.get("sma_50", 0) or 0,
        "close": last.get("close", 0) or 0,
        "vol_ratio": last.get("vol_ratio", 1.0) or 1.0,
        "adx": last.get("adx") if not pd.isna(last.get("adx")) else None,
        "di_plus": last.get("di_plus") if not pd.isna(last.get("di_plus")) else None,
        "di_minus": last.get("di_minus") if not pd.isna(last.get("di_minus")) else None,
        "higher_tf_trend": None,  # backtest runs on single TF only
    }


@dataclass
class BacktestResult:
    """Container for backtest output."""

    equity: pd.Series
    trades: list[dict] = field(default_factory=list)

    @property
    def total_return_pct(self) -> float:
        if self.equity.empty or self.equity.iloc[0] == 0:
            return 0.0
        return (self.equity.iloc[-1] / self.equity.iloc[0] - 1) * 100

    @property
    def max_drawdown_pct(self) -> float:
        if self.equity.empty:
            return 0.0
        peak = self.equity.cummax()
        dd = (self.equity - peak) / peak * 100
        return dd.min()

    @property
    def sharpe(self) -> float | None:
        if len(self.equity) < 2:
            return None
        returns = self.equity.pct_change().dropna()
        if returns.std() == 0:
            return None
        return (returns.mean() / returns.std()) * (252 ** 0.5)

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.get("pnl", 0) > 0)
        return wins / len(self.trades) * 100

    @property
    def expectancy(self) -> float | None:
        """Average expected return per trade."""
        if not self.trades:
            return None
        pnls = [t["pnl"] for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / len(pnls) if pnls else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0
        if avg_loss == 0:
            return avg_win if wins else 0
        return round(avg_win * win_rate - avg_loss * (1 - win_rate), 2)

    def signal_stats(self) -> dict[str, dict]:
        """Win rate per signal type. Returns dict: signal -> {trades, wins, win_rate, avg_pnl}."""
        from collections import defaultdict
        stats: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnls": []})

        for trade in self.trades:
            pnl = trade.get("pnl", 0)
            for sig in trade.get("signals", []):
                stats[sig]["trades"] += 1
                stats[sig]["pnls"].append(pnl)
                if pnl > 0:
                    stats[sig]["wins"] += 1

        result = {}
        for sig, data in stats.items():
            if data["trades"] >= 3:  # minimum sample size
                result[sig] = {
                    "trades": data["trades"],
                    "wins": data["wins"],
                    "win_rate": round(data["wins"] / data["trades"] * 100, 1),
                    "avg_pnl": round(sum(data["pnls"]) / len(data["pnls"]), 2),
                }
        return result

    def summary(self) -> str:
        lines = [
            "📈 Бэктест",
            f"Прибыльность: {self.total_return_pct:+.2f}%",
            f"Макс. просадка: {self.max_drawdown_pct:.2f}%",
            f"Sharpe: {self.sharpe:.2f}" if self.sharpe is not None else "Sharpe: N/A",
            f"Сделок: {self.trade_count}",
            f"Win-rate: {self.win_rate:.1f}%",
        ]
        exp = self.expectancy
        if exp is not None:
            lines.append(f"Expectancy: {exp:+.2f} RUB")
        sig_stats = self.signal_stats()
        if sig_stats:
            lines.append("\n📊 Win-rate по сигналам:")
            for sig, st in sorted(sig_stats.items(), key=lambda x: x[1]["win_rate"], reverse=True):
                lines.append(f"  {sig}: {st['win_rate']:.0f}% ({st['wins']}/{st['trades']}, avg {st['avg_pnl']:+.1f})")
        return "\n".join(lines)


class Backtester:
    """Run a signal-based backtest on a daily candle DataFrame."""

    def __init__(self, lookback: int = 50):
        self.lookback = lookback

    def run(
        self,
        df: pd.DataFrame,
        ticker: str = "TEST",
        initial: float = 1_000_000,
        commission: float = 0.0005,
        macro_bullish: bool = True,
    ) -> BacktestResult:
        """Simulate daily-rebalanced strategy.

        Args:
            df: DataFrame with open, high, low, close, volume
            ticker: Ticker name (passed to recommend_direction)
            initial: Starting capital
            commission: Per-trade commission as fraction (e.g. 0.0005 = 0.05%)
            macro_bullish: Passed to direction advisor
        """
        if len(df) < self.lookback + 2:
            return BacktestResult(pd.Series([initial], index=df.index[:1]))

        rows: list[dict] = []
        for i in range(self.lookback, len(df)):
            slice_df = df.iloc[: i + 1].copy()
            slice_df = add_indicators(slice_df)
            signals = detect_signals(slice_df)
            vals = _last_values(slice_df)
            scored = score_stock(slice_df, higher_tf_trend=vals.get("higher_tf_trend"))

            advice = recommend_direction(
                ticker=ticker,
                signals=signals,
                score=scored["score"],
                **vals,
                macro_bullish=macro_bullish,
            )

            rows.append(
                {
                    "date": df.index[i],
                    "close": float(df["close"].iloc[i]),
                    "prev_close": float(df["close"].iloc[i - 1]),
                    "advice": advice.direction,
                    "signals": signals,
                }
            )

        signals_df = pd.DataFrame(rows).set_index("date")
        exposure_map = {"long": 1, "short": -1, "neutral": 0}
        signals_df["exposure"] = signals_df["advice"].map(exposure_map).fillna(0)

        # Daily returns (signal known at close, traded next day — simplified to same-day close)
        signals_df["price_return"] = signals_df["close"] / signals_df["prev_close"] - 1
        signals_df["strategy_return"] = signals_df["exposure"].shift(1) * signals_df["price_return"]

        # Commission on exposure change
        signals_df["turnover"] = signals_df["exposure"].diff().abs()
        signals_df["strategy_return"] -= signals_df["turnover"] * commission

        equity = initial * (1 + signals_df["strategy_return"].fillna(0)).cumprod()

        # Build trades log with signals for per-signal analytics
        trades: list[dict] = []
        in_trade = False
        entry_px: float | None = None
        entry_date = None
        side: str | None = None
        entry_signals: list[str] = []

        for date, row in signals_df.iterrows():
            if not in_trade and row["exposure"] != 0:
                in_trade = True
                entry_px = row["close"]
                entry_date = date
                side = "long" if row["exposure"] > 0 else "short"
                # Store signals that were present at entry
                entry_signals = row.get("signals", [])
            elif in_trade and row["exposure"] == 0:
                assert entry_px is not None and side is not None
                pnl = row["close"] - entry_px if side == "long" else entry_px - row["close"]
                trades.append(
                    {
                        "entry_date": entry_date,
                        "exit_date": date,
                        "side": side,
                        "entry_px": entry_px,
                        "exit_px": row["close"],
                        "pnl": pnl,
                        "signals": list(entry_signals),
                    }
                )
                in_trade = False
                entry_px = None
                side = None
                entry_signals = []

        return BacktestResult(equity, trades)
