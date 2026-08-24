"""Unit tests for risk management logic."""

from __future__ import annotations

import pytest

from strategies.risk import (
    TradePlan,
    calculate_position,
    check_correlation,
    validate_trade,
)


class TestCalculatePosition:
    def test_long_basic(self):
        plan = calculate_position("SBER", "long", 250.0, atr=5.0, equity=1_000_000)
        assert plan.ticker == "SBER"
        assert plan.side == "long"
        assert plan.entry_px == 250.0
        assert plan.stop_px == 242.5  # 250 - 1.5*5 (STOP_LOSS_ATR_MULT)
        assert plan.target_px == 261.25  # 250 + 1.5*5*1.5 (TARGET_RR)
        assert plan.qty >= 1
        assert plan.risk_rub > 0
        assert plan.risk_pct <= 2.0

    def test_short_basic(self):
        plan = calculate_position("GAZP", "short", 200.0, atr=4.0, equity=1_000_000)
        assert plan.side == "short"
        assert plan.stop_px == 206.0  # 200 + 1.5*4
        assert plan.target_px == 191.0  # 200 - 1.5*4*1.5
        assert plan.qty >= 1

    def test_risk_reward(self):
        plan = calculate_position("SBER", "long", 250.0, atr=5.0, equity=1_000_000)
        rr = plan.risk_reward()
        assert rr is not None
        assert rr == pytest.approx(1.5)

    def test_zero_atr(self):
        plan = calculate_position("SBER", "long", 250.0, atr=0.0, equity=1_000_000)
        assert plan.qty == 1
        assert plan.stop_px == 250.0
        assert plan.risk_reward() is None

    def test_respects_max_pct(self):
        plan = calculate_position("SBER", "long", 250.0, atr=50.0, equity=1_000_000, max_pct=2.0)
        assert plan.risk_pct <= 2.0


class TestCheckCorrelation:
    def test_same_sector_warning(self):
        open_pos = [{"ticker": "SBER"}]
        same = check_correlation("VTBR", open_pos)
        assert same == ["SBER"]

    def test_different_sector_ok(self):
        open_pos = [{"ticker": "SBER"}]
        same = check_correlation("GAZP", open_pos)
        assert same == []

    def test_unknown_sector(self):
        # Both tickers map to 'unknown' sector, so they match
        open_pos = [{"ticker": "UNKNOWN"}]
        same = check_correlation("XXX", open_pos)
        assert same == ["UNKNOWN"]


class TestValidateTrade:
    def test_no_warnings_when_valid(self):
        plan = TradePlan("SBER", "long", 250.0, 240.0, 270.0, 100, 10_000, 1.0)
        warnings = validate_trade(plan, [], equity=1_000_000, max_positions=5, max_pct=2.0)
        assert warnings == []

    def test_too_many_positions(self):
        plan = TradePlan("SBER", "long", 250.0, 240.0, 270.0, 100, 10_000, 1.0)
        positions = [{"ticker": f"T{i}"} for i in range(5)]
        warnings = validate_trade(plan, positions, equity=1_000_000, max_positions=5)
        assert any("Слишком много" in w for w in warnings)

    def test_risk_too_high(self):
        plan = TradePlan("SBER", "long", 250.0, 240.0, 270.0, 100, 30_000, 3.0)
        warnings = validate_trade(plan, [], equity=1_000_000, max_pct=2.0)
        assert any("превышает лимит" in w for w in warnings)

    def test_bad_risk_reward(self):
        plan = TradePlan("SBER", "long", 250.0, 248.0, 251.0, 100, 200, 0.02)
        warnings = validate_trade(plan, [], equity=1_000_000)
        assert any("Риск/прибыль" in w for w in warnings)

    def test_sector_correlation(self):
        plan = TradePlan("VTBR", "long", 0.1, 0.09, 0.12, 1000, 10, 0.1)
        positions = [{"ticker": "SBER"}]
        warnings = validate_trade(plan, positions, equity=1_000_000)
        assert any("секторе" in w for w in warnings)
