"""Technical indicators and screener for Russian stocks.

Uses pandas + pandas-ta for calculations. The screener ranks stocks
by a composite score based on volume, momentum, volatility, and trend.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd

from core.config import WATCHLIST, MIN_AVG_VOLUME
from strategies.signals import recommend_direction


try:
    import pandas_ta  # noqa: F401 — registers .ta accessor
    _HAS_PANDAS_TA = True
except Exception:
    _HAS_PANDAS_TA = False


def df_from_candles(candles: list[dict]) -> pd.DataFrame:
    """Convert MOEX candle dicts to a pandas DataFrame."""
    df = pd.DataFrame(candles)
    if df.empty:
        return df
    # MOEX uses 'begin' for timestamp, DB uses 'ts' — normalize
    if "begin" in df.columns:
        df = df.rename(columns={"begin": "ts"})
    elif "end" in df.columns and "ts" not in df.columns:
        df = df.rename(columns={"end": "ts"})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "value" in df.columns:
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def resample_1m_to_5m(candles_1m: list[dict]) -> list[dict]:
    """Build 5-minute OHLCV candles from 1-minute MOEX candles."""
    if not candles_1m:
        return []

    df = df_from_candles(candles_1m)
    if df.empty:
        return []

    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if "value" in df.columns:
        agg["value"] = "sum"

    resampled = df.resample("5min").agg(agg)
    resampled = resampled.dropna(subset=["open", "high", "low", "close"])

    records = []
    for ts, row in resampled.iterrows():
        record = {"begin": ts.strftime("%Y-%m-%d %H:%M:%S")}
        for col in ("open", "high", "low", "close", "volume", "value"):
            if col in row.index and pd.notna(row[col]):
                record[col] = float(row[col])
        records.append(record)
    return records


# ── Indicators ────────────────────────────────────────────────


def _add_indicators_manual(df: pd.DataFrame) -> pd.DataFrame:
    """Hand-rolled indicators when pandas-ta is unavailable."""
    c = df["close"]
    h = df["high"]
    lo = df["low"]
    v = df["volume"]

    # RSI(14)
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14, min_periods=14).mean()
    avg_loss = loss.rolling(14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD(12, 26, 9)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # ATR(14)
    tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14, min_periods=14).mean()

    # ADX(14) with DI+ and DI-
    high_prev = h.shift(1)
    low_prev = lo.shift(1)
    close_prev = c.shift(1)
    plus_dm = ((h - high_prev) > (low_prev - lo)) * (h - high_prev)
    plus_dm = plus_dm.clip(lower=0)
    minus_dm = ((low_prev - lo) > (h - high_prev)) * (low_prev - lo)
    minus_dm = minus_dm.clip(lower=0)
    tr1 = h - lo
    tr2 = (h - close_prev).abs()
    tr3 = (lo - close_prev).abs()
    tr_adx = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr_adx = tr_adx.rolling(14, min_periods=14).mean()
    plus_di = 100 * (plus_dm.rolling(14, min_periods=14).mean() / atr_adx.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(14, min_periods=14).mean() / atr_adx.replace(0, np.nan))
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    df["adx"] = dx.rolling(14, min_periods=14).mean()
    df["di_plus"] = plus_di
    df["di_minus"] = minus_di

    # Bollinger Bands(20, 2)
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    df["bb_upper"] = bb_mid + 2 * bb_std
    df["bb_lower"] = bb_mid - 2 * bb_std
    df["bb_pct"] = (c - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)

    # SMA
    df["sma_20"] = c.rolling(20).mean()
    df["sma_50"] = c.rolling(50).mean()

    # Volume ratio
    avg_vol = v.rolling(20).mean()
    df["vol_ratio"] = v / avg_vol.replace(0, np.nan)

    # VWAP
    typical = (h + lo + c) / 3
    df["vwap"] = (typical * v).cumsum() / v.cumsum().replace(0, np.nan)
    return df


def _add_indicators_ta(df: pd.DataFrame) -> pd.DataFrame:
    """pandas-ta based indicators (preferred when available)."""
    c = df["close"]
    h = df["high"]
    lo = df["low"]
    v = df["volume"]

    df.ta.rsi(length=14, append=True)
    df["rsi"] = df.get("RSI_14", pd.Series(np.nan, index=df.index))

    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df["macd"] = df.get("MACD_12_26_9", pd.Series(np.nan, index=df.index))
    df["macd_signal"] = df.get("MACDs_12_26_9", pd.Series(np.nan, index=df.index))
    df["macd_hist"] = df.get("MACDh_12_26_9", pd.Series(np.nan, index=df.index))

    df.ta.atr(length=14, append=True)
    df["atr"] = df.get("ATRr_14", pd.Series(np.nan, index=df.index))

    df.ta.adx(length=14, append=True)
    df["adx"] = df.get("ADX_14", pd.Series(np.nan, index=df.index))
    df["di_plus"] = df.get("DMP_14", pd.Series(np.nan, index=df.index))
    df["di_minus"] = df.get("DMN_14", pd.Series(np.nan, index=df.index))

    df.ta.bbands(length=20, std=2, append=True)
    df["bb_upper"] = df.get("BBU_20_2.0_2.0", df.get("BBU_20_2.0", pd.Series(np.nan, index=df.index)))
    df["bb_lower"] = df.get("BBL_20_2.0_2.0", df.get("BBL_20_2.0", pd.Series(np.nan, index=df.index)))
    bb_range = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
    df["bb_pct"] = (c - df["bb_lower"]) / bb_range

    df.ta.sma(length=20, append=True)
    df.ta.sma(length=50, append=True)
    df["sma_20"] = df.get("SMA_20", pd.Series(np.nan, index=df.index))
    df["sma_50"] = df.get("SMA_50", pd.Series(np.nan, index=df.index))

    avg_vol = v.rolling(20).mean()
    df["vol_ratio"] = v / avg_vol.replace(0, np.nan)

    typical = (h + lo + c) / 3
    df["vwap"] = (typical * v).cumsum() / v.cumsum().replace(0, np.nan)

    _ta_drop = [
        "RSI_14",
        "MACD_12_26_9", "MACDh_12_26_9", "MACDs_12_26_9",
        "ATR_14",
        "BBU_20_2.0", "BBL_20_2.0", "BBM_20_2.0", "BBB_20_2.0", "BBP_20_2.0",
        "SMA_20", "SMA_50",
        "ADX_14", "DMP_14", "DMN_14",
    ]
    df = df.drop(columns=[col for col in _ta_drop if col in df.columns])
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add standard technical indicators to a candle DataFrame.

    Prefers pandas-ta when available, falls back to hand-rolled formulas.
    Requires at least: open, high, low, close, volume columns.
    """
    if len(df) < 30:
        return df
    if _HAS_PANDAS_TA:
        return _add_indicators_ta(df)
    return _add_indicators_manual(df)


# ── Candlestick pattern detection ────────────────────────────


def detect_candle_patterns(df: pd.DataFrame) -> list[str]:
    """Detect candlestick patterns on the latest candle."""
    patterns: list[str] = []
    if len(df) < 2:
        return patterns

    last = df.iloc[-1]
    prev = df.iloc[-2]

    o = last.get("open")
    c = last.get("close")
    h = last.get("high")
    lo = last.get("low")
    prev_o = prev.get("open")
    prev_c = prev.get("close")

    if o is None or c is None or h is None or lo is None:
        return patterns

    body = abs(c - o)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - lo
    total_range = h - lo

    if total_range <= 0:
        return patterns

    # Doji: body < 5% of total range
    if body / total_range < 0.05:
        patterns.append("DOJI")

    # Hammer: small body in upper half, long lower shadow (> 2x body)
    if lower_shadow > body * 2 and upper_shadow < body * 0.5 and c >= lo + total_range * 0.5:
        patterns.append("HAMMER")

    # Hanging man: same shape as hammer but in uptrend context
    # (caller decides uptrend via SMA context)
    if lower_shadow > body * 2 and upper_shadow < body * 0.5 and c < lo + total_range * 0.5:
        patterns.append("HANGING_MAN")

    # Bullish engulfing: current body fully covers previous, bullish close
    if prev_o is not None and prev_c is not None:
        if o < prev_c and c > prev_o and c > o:
            patterns.append("BULLISH_ENGULFING")
        # Bearish engulfing: current body fully covers previous, bearish close
        if o > prev_c and c < prev_o and c < o:
            patterns.append("BEARISH_ENGULFING")

    return patterns


def compute_relative_strength(stock_df: pd.DataFrame, index_df: pd.DataFrame, lookback: int = 20) -> float | None:
    """Compute relative strength: stock return % - index return % over N periods.

    Positive = stock outperformed the index.
    Negative = stock underperformed the index.
    """
    if len(stock_df) < lookback + 1 or len(index_df) < lookback + 1:
        return None

    stock_now = float(stock_df["close"].iloc[-1])
    stock_then = float(stock_df["close"].iloc[-lookback - 1])
    index_now = float(index_df["close"].iloc[-1])
    index_then = float(index_df["close"].iloc[-lookback - 1])

    if stock_then <= 0 or index_then <= 0:
        return None

    stock_return = (stock_now / stock_then - 1) * 100
    index_return = (index_now / index_then - 1) * 100

    return round(stock_return - index_return, 2)


# ── Signal detection ──────────────────────────────────────────


def detect_signals(df: pd.DataFrame) -> list[str]:
    """Detect active signals from the latest candle. Returns signal names."""
    signals: list[str] = []
    if len(df) < 2:
        return signals

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # RSI extremes
    if last.get("rsi") is not None:
        if last["rsi"] < 30:
            signals.append("RSI_oversold")
        elif last["rsi"] > 70:
            signals.append("RSI_overbought")

    # MACD cross
    if last.get("macd_hist") is not None and prev.get("macd_hist") is not None:
        if prev["macd_hist"] < 0 and last["macd_hist"] >= 0:
            signals.append("MACD_bullish_cross")
        elif prev["macd_hist"] > 0 and last["macd_hist"] <= 0:
            signals.append("MACD_bearish_cross")

    # Bollinger squeeze / breakout
    if last.get("bb_pct") is not None:
        if last["bb_pct"] < 0.05:
            signals.append("BB_lower_touch")
        elif last["bb_pct"] > 0.95:
            signals.append("BB_upper_touch")

    # Volume spike
    if last.get("vol_ratio") is not None and last["vol_ratio"] > 2.5:
        signals.append("VOLUME_spike")

    # Trend: price above/below SMA 20 and 50
    if last.get("sma_20") and last.get("sma_50"):
        if last["close"] > last["sma_20"] > last["sma_50"]:
            signals.append("UPTREND")
        elif last["close"] < last["sma_20"] < last["sma_50"]:
            signals.append("DOWNTREND")

    # ADX trend strength and direction
    adx = last.get("adx")
    di_plus = last.get("di_plus")
    di_minus = last.get("di_minus")
    if adx is not None and not pd.isna(adx):
        if adx > 25:
            signals.append("ADX_strong_trend")
            if di_plus is not None and di_minus is not None and not pd.isna(di_plus) and not pd.isna(di_minus):
                if di_plus > di_minus:
                    signals.append("ADX_bullish_trend")
                else:
                    signals.append("ADX_bearish_trend")
        elif adx < 20:
            signals.append("ADX_weak_trend")

    # Candlestick patterns
    candle_patterns = detect_candle_patterns(df)
    for pat in candle_patterns:
        signals.append(pat)

    # Fibonacci levels bounce / rejection
    fib_levels = compute_fibonacci_levels(df)
    if fib_levels:
        fib_signals = detect_fib_signals(df, fib_levels)
        for sig in fib_signals:
            signals.append(sig)

    # Gap detection
    if prev.get("close") and last.get("open"):
        gap_pct = (last["open"] - prev["close"]) / prev["close"] * 100
        if gap_pct > 1.5:
            signals.append("GAP_up")
        elif gap_pct < -1.5:
            signals.append("GAP_down")

    return signals


# ── Screener scoring ──────────────────────────────────────────


def score_stock(df: pd.DataFrame, higher_tf_trend: str | None = None, div_yield: float | None = None, rs_vs_index: float | None = None) -> dict:
    """Compute composite screener score for a single stock.

    Args:
        df: DataFrame with daily candles.
        higher_tf_trend: Trend from higher timeframe (e.g. weekly).
            One of: 'UPTREND', 'DOWNTREND', 'NEUTRAL', or None.
        div_yield: Annual dividend yield in percent (e.g. 5.5).
        rs_vs_index: Relative strength vs index (stock_return % - index_return %).

    Returns dict with score, signals, and indicator details.
    """
    if len(df) < 50:
        return {"score": 0, "signals": [], "details": {}}

    df = add_indicators(df.copy())
    signals = detect_signals(df)

    last = df.iloc[-1]
    score = 0.0

    # Volume activity (0-30 points)
    vol_ratio = last.get("vol_ratio", 1.0) or 1.0
    score += min(vol_ratio * 10, 30)

    # Momentum via MACD (0-20 points)
    hist = last.get("macd_hist", 0) or 0
    atr = last.get("atr", 0)
    if pd.isna(atr) or atr == 0:
        atr = 1
    score += min(abs(hist) / atr * 5, 20)

    # RSI deviation from 50 (0-15 points)
    rsi = last.get("rsi", 50) or 50
    score += abs(rsi - 50) / 50 * 15

    # Trend strength (0-20 points)
    if "UPTREND" in signals:
        score += 20
    elif "DOWNTREND" in signals:
        score += 15  # shorts are interesting too

    # ADX trend filter
    adx = last.get("adx")
    di_plus = last.get("di_plus")
    di_minus = last.get("di_minus")
    if adx is not None and not pd.isna(adx):
        if adx > 25:
            # Boost score when trend direction matches signal direction
            if "ADX_bullish_trend" in signals and ("UPTREND" in signals or "MACD_bullish_cross" in signals or "RSI_oversold" in signals):
                score += 10
            elif "ADX_bearish_trend" in signals and ("DOWNTREND" in signals or "MACD_bearish_cross" in signals or "RSI_overbought" in signals):
                score += 10
        elif adx < 20:
            # Flat market penalty — signals less reliable
            score -= 10

    # Multi-timeframe trend filter
    if higher_tf_trend:
        if higher_tf_trend == "UPTREND":
            if "UPTREND" in signals or "MACD_bullish_cross" in signals or "RSI_oversold" in signals:
                score += 10  # daily signal aligned with weekly uptrend
            elif "DOWNTREND" in signals or "MACD_bearish_cross" in signals or "RSI_overbought" in signals:
                score -= 15  # short signal against weekly uptrend
        elif higher_tf_trend == "DOWNTREND":
            if "DOWNTREND" in signals or "MACD_bearish_cross" in signals or "RSI_overbought" in signals:
                score += 10  # daily signal aligned with weekly downtrend
            elif "UPTREND" in signals or "MACD_bullish_cross" in signals or "RSI_oversold" in signals:
                score -= 15  # long signal against weekly downtrend

    # Fundamental filter: dividend yield
    if div_yield is not None:
        if div_yield > 7:
            score += 10  # high yield = potential undervaluation
        elif div_yield > 4:
            score += 5
        elif div_yield < 1:
            score -= 15  # low yield = potential overvaluation
        elif div_yield < 2:
            score -= 5

    # Overvaluation guard: low div yield + high RSI = danger
    rsi_guard = last.get("rsi")
    if div_yield is not None and rsi_guard is not None and not pd.isna(rsi_guard):
        if div_yield < 1 and rsi_guard > 70:
            score -= 20

    # Relative strength vs index
    if rs_vs_index is not None:
        if rs_vs_index > 5:
            score += 10  # strong outperformance
        elif rs_vs_index > 0:
            score += 5  # mild outperformance
        elif rs_vs_index < -5:
            score -= 10  # strong underperformance
        elif rs_vs_index < 0:
            score -= 5  # mild underperformance

    # Fibonacci bounce bonus
    fib_bull = any(s.startswith("FIB_bounce") for s in signals)
    fib_bear = any(s.startswith("FIB_reject") for s in signals)
    if fib_bull:
        score += 12
    if fib_bear:
        score += 8  # shorts get less bonus (safer)

    # Signal bonuses (5 pts each)
    score += len(signals) * 5

    # Predict bounce probability using daily indicators and trend weakening
    bounce_pred = predict_bounce(df)

    details = {
        "close": round(last.get("close", 0), 2),
        "rsi": round(rsi, 1),
        "macd_hist": round(hist, 4),
        "atr": round(last.get("atr", 0), 2),
        "vol_ratio": round(vol_ratio, 2),
        "bb_pct": round(last.get("bb_pct", 0), 2),
        "sma_20": round(last.get("sma_20", 0), 2),
        "sma_50": round(last.get("sma_50", 0), 2),
        "adx": round(adx, 1) if adx is not None and not pd.isna(adx) else None,
        "di_plus": round(di_plus, 1) if di_plus is not None and not pd.isna(di_plus) else None,
        "di_minus": round(di_minus, 1) if di_minus is not None and not pd.isna(di_minus) else None,
        "div_yield": round(div_yield, 2) if div_yield is not None else None,
        "rs_vs_index": rs_vs_index,
        "fib_levels": compute_fibonacci_levels(df),
        "bounce": bounce_pred,
    }

    # Compute directional recommendation aligned with detailed analysis
    advice = recommend_direction(
        ticker="",
        signals=signals,
        score=score,
        rsi=float(rsi),
        macd_hist=float(hist),
        bb_pct=float(last.get("bb_pct", 0.5)) if not pd.isna(last.get("bb_pct")) else 0.5,
        sma_20=float(last.get("sma_20", 0)) if not pd.isna(last.get("sma_20")) else 0,
        sma_50=float(last.get("sma_50", 0)) if not pd.isna(last.get("sma_50")) else 0,
        close=float(last.get("close", 0)) if not pd.isna(last.get("close")) else 0,
        vol_ratio=float(vol_ratio),
        macro_bullish=True,
        adx=float(adx) if adx is not None and not pd.isna(adx) else None,
        di_plus=float(di_plus) if di_plus is not None and not pd.isna(di_plus) else None,
        di_minus=float(di_minus) if di_minus is not None and not pd.isna(di_minus) else None,
        higher_tf_trend=higher_tf_trend,
        div_yield=float(div_yield) if div_yield is not None else None,
    )

    # Liquidity filter — skip illiquid stocks based on absolute average volume
    avg_vol = df["volume"].rolling(20).mean().iloc[-1]
    details["avg_volume_20d"] = round(float(avg_vol), 0) if avg_vol is not None else None
    if avg_vol and avg_vol < MIN_AVG_VOLUME:
        score *= 0.2  # heavily penalize low liquidity
        warnings_list = details.get("warnings", [])
        if not isinstance(warnings_list, list):
            warnings_list = []
        warnings_list.append(f"Низкая ликвидность: средний объём {avg_vol:,.0f} лотов (порог {MIN_AVG_VOLUME:,})")
        details["warnings"] = warnings_list

    if advice.direction == "long":
        rec_text = "ЛОНГ (сильно)" if score >= 70 else "ЛОНГ (умеренно)" if score >= 55 else "ЛОНГ (слабо)"
    elif advice.direction == "short":
        rec_text = "ШОРТ (сильно)" if score <= 30 else "ШОРТ (умеренно)" if score <= 45 else "ШОРТ (слабо)"
    else:
        rec_text = "НЕЙТРАЛЬНО"

    return {
        "score": float(round(score, 1)),
        "recommendation": rec_text,
        "signals": signals,
        "direction": advice.direction,
        "strength": advice.strength,
        "reason": advice.reason,
        "warnings": advice.warnings,
        "bull_score": advice.bull_score,
        "bear_score": advice.bear_score,
        "details": details,
    }


def compute_fibonacci_levels(df: pd.DataFrame, impulse_pct: float = 10.0) -> dict[str, float] | None:
    """Compute Fibonacci retracement levels after a strong impulse.

    Finds the most recent swing high/low with > impulse_pct move
    and returns 38.2%, 50%, 61.8% retracement levels.
    """
    if len(df) < 20:
        return None

    highs = df["high"]
    lows = df["low"]
    closes = df["close"]

    # Find last significant swing
    # Simple approach: find local max/min and check move > impulse_pct
    for i in range(len(df) - 1, 19, -1):
        window = df.iloc[i - 19:i + 1]
        high = window["high"].max()
        low = window["low"].min()
        swing_pct = (high - low) / low * 100

        if swing_pct >= impulse_pct:
            # Determine direction: was it up or down impulse?
            # Use close at swing start vs end
            swing_start_close = window["close"].iloc[0]
            swing_end_close = window["close"].iloc[-1]
            if swing_end_close > swing_start_close:
                # Bullish impulse: retracements below high
                diff = high - low
                return {
                    "0": round(high, 2),
                    "38.2": round(high - diff * 0.382, 2),
                    "50": round(high - diff * 0.5, 2),
                    "61.8": round(high - diff * 0.618, 2),
                    "100": round(low, 2),
                    "direction": "up",
                }
            else:
                # Bearish impulse: retracements above low
                diff = high - low
                return {
                    "0": round(low, 2),
                    "38.2": round(low + diff * 0.382, 2),
                    "50": round(low + diff * 0.5, 2),
                    "61.8": round(low + diff * 0.618, 2),
                    "100": round(high, 2),
                    "direction": "down",
                }

    return None


def detect_fib_signals(df: pd.DataFrame, fib_levels: dict | None = None) -> list[str]:
    """Detect signals based on price bouncing off Fibonacci levels."""
    signals: list[str] = []
    if fib_levels is None or len(df) < 3:
        return signals

    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = last.get("close")
    prev_close = prev.get("close")
    rsi = last.get("rsi")

    if close is None or prev_close is None:
        return signals

    direction = fib_levels.get("direction")

    # Bullish impulse: look for bounce from 61.8% or 50%
    if direction == "up":
        for level_key in ["61.8", "50"]:
            level = fib_levels.get(level_key)
            if level and prev_close <= level * 1.01 and close >= level * 0.99:
                # Price touched and bounced off Fib level
                if rsi is not None and not pd.isna(rsi) and rsi < 50:
                    signals.append(f"FIB_bounce_{level_key}")
                break

    # Bearish impulse: look for rejection at 61.8% or 50%
    elif direction == "down":
        for level_key in ["61.8", "50"]:
            level = fib_levels.get(level_key)
            if level and prev_close >= level * 0.99 and close <= level * 1.01:
                if rsi is not None and not pd.isna(rsi) and rsi > 50:
                    signals.append(f"FIB_reject_{level_key}")
                break

    return signals


def compute_volume_profile(df: pd.DataFrame, bins: int = 10) -> dict[str, float] | None:
    """Simplified volume profile from intraday candles (1h or 10m).

    Groups price range into bins and assigns volume to each bin.
    Returns Point of Control (price with most volume) and
    approximate Value Area High/Low (70% of volume).
    """
    if len(df) < 5 or "volume" not in df.columns:
        return None

    # Use typical price (high+low+close)/3 as representative price per candle
    typical = (df["high"] + df["low"] + df["close"]) / 3
    min_p = typical.min()
    max_p = typical.max()
    if min_p is None or max_p is None or max_p <= min_p:
        return None

    bin_edges = np.linspace(min_p, max_p, bins + 1)
    bin_volumes = np.zeros(bins)

    for i in range(len(df)):
        price = typical.iloc[i]
        vol = df["volume"].iloc[i]
        # Find bin index
        idx = int((price - min_p) / (max_p - min_p) * bins)
        idx = min(idx, bins - 1)
        bin_volumes[idx] += vol

    # POC = center of bin with max volume
    poc_idx = int(np.argmax(bin_volumes))
    poc = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2

    # Value Area = 70% of total volume around POC
    total_vol = bin_volumes.sum()
    target_vol = total_vol * 0.70
    # Expand outward from POC until we reach 70%
    left = poc_idx
    right = poc_idx
    accumulated = bin_volumes[poc_idx]
    while accumulated < target_vol and (left > 0 or right < bins - 1):
        if left > 0:
            left -= 1
            accumulated += bin_volumes[left]
        if right < bins - 1:
            right += 1
            accumulated += bin_volumes[right]

    val_low = bin_edges[left]
    val_high = bin_edges[right + 1]

    return {
        "poc": round(float(poc), 2),
        "value_area_low": round(float(val_low), 2),
        "value_area_high": round(float(val_high), 2),
        "total_volume": round(float(total_vol), 0),
    }


def predict_bounce(df: pd.DataFrame, lookback: int = 10) -> dict[str, Any]:
    """Predict probability and direction of a bounce/reversal.

    Combines oversold/overbought conditions, Bollinger Bands position,
    MACD convergence/divergence, volume, and — critically — trend
    weakening (ADX rolling over, DI+/- convergence).

    Returns dict:
        probability: int (0-100)
        direction: "up" | "down" | "none"
        confidence_label: "high" | "moderate" | "low"
        factors: list of active factor names
        reasons: list of human-readable reasons
    """
    if len(df) < lookback + 5:
        return {"probability": 0, "direction": "none", "confidence_label": "low", "factors": [], "reasons": ["Недостаточно данных"]}

    last = df.iloc[-1]
    prev = df.iloc[-2]

    rsi = last.get("rsi")
    bb_pct = last.get("bb_pct")
    macd_hist = last.get("macd_hist")
    macd_hist_prev = prev.get("macd_hist")
    adx = last.get("adx")
    adx_prev = df.iloc[-3:-1]["adx"].mean() if len(df) >= 3 else None
    di_plus = last.get("di_plus")
    di_minus = last.get("di_minus")
    vol_ratio = last.get("vol_ratio")
    close = last.get("close")
    atr = last.get("atr")

    if close is None or pd.isna(close):
        return {"probability": 0, "direction": "none", "confidence_label": "low", "factors": [], "reasons": ["Нет цены"]}

    probability = 0
    factors: list[str] = []
    reasons: list[str] = []

    # 1. RSI extremes — classic mean-reversion signal
    if rsi is not None and not pd.isna(rsi):
        if rsi < 25:
            probability += 25
            factors.append("rsi_deep_oversold")
            reasons.append(f"RSI {rsi:.1f} — глубокая перепроданность")
        elif rsi < 35:
            probability += 15
            factors.append("rsi_oversold")
            reasons.append(f"RSI {rsi:.1f} — перепроданность")
        elif rsi > 75:
            probability += 25
            factors.append("rsi_deep_overbought")
            reasons.append(f"RSI {rsi:.1f} — глубокая перекупленность")
        elif rsi > 65:
            probability += 15
            factors.append("rsi_overbought")
            reasons.append(f"RSI {rsi:.1f} — перекупленность")

    # 2. Bollinger Bands position
    if bb_pct is not None and not pd.isna(bb_pct):
        if bb_pct < 0.1:
            probability += 20
            factors.append("bb_lower_touch")
            reasons.append("Цена у нижней полосы Боллинджера")
        elif bb_pct > 0.9:
            probability += 20
            factors.append("bb_upper_touch")
            reasons.append("Цена у верхней полосы Боллинджера")

    # 3. MACD histogram convergence (divergence from trend weakening)
    if macd_hist is not None and not pd.isna(macd_hist) and macd_hist_prev is not None and not pd.isna(macd_hist_prev):
        # Price made new low but MACD histogram is less negative = bullish divergence
        recent_lows = df["low"].tail(lookback)
        is_new_local_low = close <= recent_lows.min() * 1.005
        is_new_local_high = close >= recent_lows.max() * 0.995

        if is_new_local_low and macd_hist > macd_hist_prev:
            probability += 15
            factors.append("macd_bullish_divergence")
            reasons.append("Новый минимум цены, но MACD замедляется — бычья дивергенция")
        elif is_new_local_high and macd_hist < macd_hist_prev:
            probability += 15
            factors.append("macd_bearish_divergence")
            reasons.append("Новый максимум цены, но MACD замедляется — медвежья дивергенция")
        elif macd_hist > macd_hist_prev:
            # Simple histogram narrowing in any direction
            probability += 5
            factors.append("macd_convergence")
            reasons.append("Гистограмма MACD сужается")

    # 4. Trend weakening — KEY filter: only trust bounce probability when trend is losing strength
    trend_weakening = False
    if adx is not None and not pd.isna(adx) and adx_prev is not None and not pd.isna(adx_prev):
        if adx < 20:
            # Weak/no trend: mean-reversion is more reliable
            probability += 10
            factors.append("adx_weak_trend")
            reasons.append("ADX низкий — тренд слабый, отскок вероятнее")
            trend_weakening = True
        elif adx < adx_prev:
            # ADX rolling over: trend is weakening
            probability += 15
            factors.append("adx_rolling_over")
            reasons.append(f"ADX падает ({adx:.1f} < {adx_prev:.1f}) — тренд ослабевает")
            trend_weakening = True
        elif adx > 30:
            # Very strong trend: reduce bounce probability (counter-trend dangerous)
            probability = max(0, probability - 20)
            factors.append("adx_strong_trend")
            reasons.append(f"ADX высокий ({adx:.1f}) — сильный тренд, отскок рискован")

    # 5. DI+ / DI- convergence = trend losing directional momentum
    if di_plus is not None and di_minus is not None and not pd.isna(di_plus) and not pd.isna(di_minus):
        di_diff = abs(di_plus - di_minus)
        di_diff_prev = None
        if len(df) >= 3:
            prev_row = df.iloc[-2]
            di_plus_prev = prev_row.get("di_plus")
            di_minus_prev = prev_row.get("di_minus")
            if di_plus_prev is not None and di_minus_prev is not None:
                di_diff_prev = abs(di_plus_prev - di_minus_prev)
        if di_diff_prev is not None and di_diff < di_diff_prev:
            probability += 10
            factors.append("di_convergence")
            reasons.append("DI+/DI- сходятся — направленный импульс ослабевает")
            trend_weakening = True

    # 6. Volume confirmation
    if vol_ratio is not None and not pd.isna(vol_ratio):
        if vol_ratio > 2.0:
            probability += 10
            factors.append("volume_spike")
            reasons.append("Объём выше среднего в 2x — подтверждение интереса")
        elif vol_ratio > 1.2:
            probability += 5
            factors.append("volume_above_avg")
            reasons.append("Объём выше среднего")
        elif vol_ratio < 0.5:
            probability = max(0, probability - 10)
            factors.append("volume_low")
            reasons.append("Низкий объём — отскок менее надёжен")

    # 7. Distance from recent extreme: only count if price is near recent high/low
    recent_low = df["low"].tail(lookback).min()
    recent_high = df["high"].tail(lookback).max()
    if recent_low and recent_high and recent_low > 0:
        dist_to_low = (close / recent_low - 1) * 100
        dist_to_high = (close / recent_high - 1) * 100
        if dist_to_low <= 1.0:
            probability += 5
            factors.append("near_recent_low")
            reasons.append(f"Цена у недавнего минимума ({dist_to_low:.1f}%)")
        elif dist_to_high >= -1.0:
            probability += 5
            factors.append("near_recent_high")
            reasons.append(f"Цена у недавнего максимума ({dist_to_high:.1f}%)")

    # 8. Candlestick reversal patterns
    patterns = detect_candle_patterns(df)
    if "HAMMER" in patterns:
        probability += 15
        factors.append("hammer")
        reasons.append("Молот — разворотная свеча")
    if "BULLISH_ENGULFING" in patterns:
        probability += 15
        factors.append("bullish_engulfing")
        reasons.append("Бычье поглощение")
    if "HANGING_MAN" in patterns:
        probability += 10
        factors.append("hanging_man")
        reasons.append("Повешенный — медвежий разворот")
    if "BEARISH_ENGULFING" in patterns:
        probability += 10
        factors.append("bearish_engulfing")
        reasons.append("Медвежье поглощение")

    # Determine direction
    direction = "none"
    if rsi is not None and not pd.isna(rsi):
        if rsi < 45 and bb_pct is not None and bb_pct < 0.5:
            direction = "up"
        elif rsi > 55 and bb_pct is not None and bb_pct > 0.5:
            direction = "down"
    if direction == "none" and bb_pct is not None and not pd.isna(bb_pct):
        direction = "up" if bb_pct < 0.3 else "down" if bb_pct > 0.7 else "none"

    # Trend-weakening bonus: when ADX is falling, probability is more trustworthy
    if trend_weakening:
        probability = min(100, int(probability * 1.15))
        factors.append("trend_weakening_confirmed")
        reasons.append("Тренд ослабевает — сигнал отскока более надёжен")
    else:
        # Without trend weakening, cap probability
        probability = min(60, probability)
        reasons.append("Тренд не ослабевает — вероятность отскока ограничена")

    probability = max(0, min(100, probability))

    if probability >= 70:
        label = "high"
    elif probability >= 45:
        label = "moderate"
    else:
        label = "low"

    return {
        "probability": int(probability),
        "direction": direction,
        "confidence_label": label,
        "factors": factors,
        "reasons": reasons,
    }


def compute_levels(direction: str, close: float, atr: float) -> dict[str, float] | None:
    """Return entry / stop / take-profit levels based on direction and ATR.

    For strong/moderate signals the entry is kept within ~0.5% of current
    price so the order actually has a chance to fill.
    """
    if close <= 0 or not close:
        return None
    if atr is None or pd.isna(atr) or atr <= 0:
        atr = close * 0.01  # fallback 1%

    if direction == "long":
        # Long: buy slightly below current price
        entry = close - min(atr * 0.3, close * 0.005)
        stop = close - atr * 1.5
        take = close + atr * 2.0
    elif direction == "short":
        # Short: sell slightly above current price
        entry = close + min(atr * 0.3, close * 0.005)
        stop = close + atr * 1.5
        take = close - atr * 2.0
    else:
        return None

    return {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "take": round(take, 2),
    }


async def run_screener(
    fetch_candles_fn,
    tickers: list[str] | None = None,
    top_n: int = 15,
    index_candles: list[dict] | None = None,
) -> list[dict]:
    """Run screener across multiple tickers concurrently.

    fetch_candles_fn: async callable(ticker) -> list[dict]
    index_candles: optional IMOEX/RTSI candles for relative strength calculation.
    Returns top-N scored results sorted by score descending.
    """
    if tickers is None:
        tickers = WATCHLIST

    index_df = df_from_candles(index_candles) if index_candles else None

    async def _screen_one(ticker: str) -> dict | None:
        try:
            candles = await fetch_candles_fn(ticker)
            df = df_from_candles(candles)
            rs = compute_relative_strength(df, index_df) if index_df is not None else None
            result = score_stock(df, rs_vs_index=rs)
            result["ticker"] = ticker
            return result
        except Exception:
            return None

    coros = [_screen_one(t) for t in tickers]
    raw = await asyncio.gather(*coros, return_exceptions=True)
    results = [r for r in raw if isinstance(r, dict)]

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_n]