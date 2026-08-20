"""Unit tests for signal generation and directional advice."""

from __future__ import annotations

import pytest

from strategies.signals import (
    DirectionAdvice,
    _bullish_score,
    _bearish_score,
    _conflicting,
    recommend_direction,
    format_direction_emoji,
)


class TestBullishScore:
    def test_empty_signals(self):
        assert _bullish_score([], rsi=50, macd_hist=0, bb_pct=0.5) == 0

    def test_rsi_oversold(self):
        # RSI_oversold = 30, rsi=25 triggers +10 bonus
        s = _bullish_score(["RSI_oversold"], rsi=25, macd_hist=0, bb_pct=0.5)
        assert s == 40

    def test_rsi_deep_oversold_bonus(self):
        # RSI_oversold = 30, rsi=15 triggers +15 bonus
        s = _bullish_score(["RSI_oversold"], rsi=15, macd_hist=0, bb_pct=0.5)
        assert s == 45

    def test_capped_at_100(self):
        s = _bullish_score(
            ["RSI_oversold", "MACD_bullish_cross", "BB_lower_touch", "UPTREND", "VOLUME_spike", "GAP_down"],
            rsi=10, macd_hist=1, bb_pct=0.0,
        )
        assert s == 100


class TestBearishScore:
    def test_empty_signals(self):
        assert _bearish_score([], rsi=50, macd_hist=0, bb_pct=0.5) == 0

    def test_rsi_overbought(self):
        # RSI_overbought = 30, rsi=75 triggers +10 bonus
        s = _bearish_score(["RSI_overbought"], rsi=75, macd_hist=0, bb_pct=0.5)
        assert s == 40

    def test_capped_at_100(self):
        s = _bearish_score(
            ["RSI_overbought", "MACD_bearish_cross", "BB_upper_touch", "DOWNTREND", "VOLUME_spike", "GAP_up"],
            rsi=90, macd_hist=-1, bb_pct=1.0,
        )
        assert s == 100


class TestConflicting:
    def test_no_conflict(self):
        assert _conflicting(["RSI_oversold", "MACD_bullish_cross"]) is False

    def test_conflict(self):
        assert _conflicting(["RSI_oversold", "RSI_overbought"]) is True

    def test_empty(self):
        assert _conflicting([]) is False


class TestRecommendDirection:
    def test_strong_long(self):
        adv = recommend_direction(
            "SBER",
            signals=["RSI_oversold", "MACD_bullish_cross", "UPTREND"],
            score=0,
            rsi=25,
            macd_hist=1.0,
            bb_pct=0.1,
            sma_20=240,
            sma_50=230,
            close=250,
            vol_ratio=1.5,
            macro_bullish=True,
        )
        assert adv.direction == "long"
        assert adv.strength == "strong"
        assert adv.risk_reward is not None
        assert adv.stop_pct is not None
        assert "RSI_oversold" in adv.signals_used

    def test_strong_short(self):
        adv = recommend_direction(
            "GAZP",
            signals=["RSI_overbought", "MACD_bearish_cross", "DOWNTREND"],
            score=0,
            rsi=80,
            macd_hist=-1.0,
            bb_pct=0.95,
            sma_20=300,
            sma_50=310,
            close=280,
            vol_ratio=2.0,
            macro_bullish=True,
        )
        assert adv.direction == "short"
        assert adv.strength == "strong"

    def test_neutral_when_conflict_balanced(self):
        adv = recommend_direction(
            "LKOH",
            signals=["RSI_oversold", "RSI_overbought"],
            score=0,
            rsi=50,
            macd_hist=0,
            bb_pct=0.5,
            sma_20=250,
            sma_50=250,
            close=250,
            vol_ratio=1.0,
        )
        assert adv.direction == "neutral"
        assert adv.strength == "weak"

    def test_blue_chip_short_discouraged(self):
        adv = recommend_direction(
            "SBER",
            signals=["RSI_overbought"],
            score=0,
            rsi=55,
            macd_hist=-0.1,
            bb_pct=0.6,
            sma_20=250,
            sma_50=250,
            close=250,
            vol_ratio=1.0,
        )
        # Blue-chip short should be penalized, likely neutral or weak short
        assert adv.direction != "short" or adv.strength == "weak"
        assert any("голубая фишка" in w for w in adv.warnings)

    def test_macro_filter_warning(self):
        adv = recommend_direction(
            "SBER",
            signals=["UPTREND"],
            score=0,
            rsi=50,
            macd_hist=0.5,
            bb_pct=0.5,
            sma_20=240,
            sma_50=230,
            close=250,
            vol_ratio=1.0,
            macro_bullish=False,
        )
        assert any("слабый" in w for w in adv.warnings)


class TestFormatDirectionEmoji:
    def test_long_strong(self):
        assert "ЛОНГ" in format_direction_emoji("long", "strong")

    def test_short_strong(self):
        assert "ШОРТ" in format_direction_emoji("short", "strong")

    def test_neutral(self):
        assert format_direction_emoji("neutral", "weak") == "⚪ ЖДАТЬ"
