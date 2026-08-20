"""Tests for the fee estimator used by paper/real trading."""

import pytest

from strategies.fees import estimate_trade_costs


def test_positive_trade_worth_it():
    est = estimate_trade_costs(entry_px=100.0, exit_px=105.0, qty=10)
    assert est.commission_buy_rub == 1.0
    assert est.commission_sell_rub == 1.05
    assert est.total_commission_rub == pytest.approx(2.05, rel=1e-3)
    assert est.gross_profit_pct == 5.0
    assert est.net_profit_pct > 4.7
    assert est.worth_it is True


def test_small_move_not_worth_it():
    est = estimate_trade_costs(entry_px=100.0, exit_px=100.4, qty=10)
    assert est.gross_profit_pct == 0.4
    assert est.worth_it is False


def test_zero_exit_returns_partial_estimate():
    est = estimate_trade_costs(entry_px=100.0, exit_px=0.0, qty=10)
    assert est.worth_it is False
    assert "выхода неизвестна" in est.notes[0]


def test_invalid_input():
    est = estimate_trade_costs(entry_px=-10.0, exit_px=11.0, qty=0)
    assert est.worth_it is False
