from core.price_reconcile import shift_stop_take_by_fill, DEFAULT_DRIFT_THRESHOLD

def test_shift_no_drift():
    # Planned: 100, Fill: 100.1 (drift = 0.1% < 0.3%)
    # Should NOT shift
    planned = 100.0
    fill = 100.1
    stop = 95.0
    take = 110.0
    new_stop, new_take, shifted = shift_stop_take_by_fill(planned, fill, stop, take)
    assert new_stop == stop
    assert new_take == take
    assert shifted is False

def test_shift_with_drift():
    # Planned: 100, Fill: 101 (drift = 1% > 0.3%)
    # Should shift by +1.0
    planned = 100.0
    fill = 101.0
    stop = 95.0
    take = 110.0
    new_stop, new_take, shifted = shift_stop_take_by_fill(planned, fill, stop, take)
    assert new_stop == 96.0
    assert new_take == 111.0
    assert shifted is True

def test_shift_negative_drift():
    # Planned: 100, Fill: 98 (drift = 2% > 0.3%)
    # Should shift by -2.0
    planned = 100.0
    fill = 98.0
    stop = 95.0
    take = 110.0
    new_stop, new_take, shifted = shift_stop_take_by_fill(planned, fill, stop, take)
    assert new_stop == 93.0
    assert new_take == 108.0
    assert shifted is True

def test_shift_none_values():
    # If any are None, should return inputs and shifted=False
    assert shift_stop_take_by_fill(100.0, 101.0, None, 110.0) == (None, 110.0, False)
    assert shift_stop_take_by_fill(100.0, 101.0, 95.0, None) == (95.0, None, False)

def test_shift_invalid_prices():
    # Negative or zero prices should not shift
    assert shift_stop_take_by_fill(-100.0, 101.0, 95.0, 110.0)[2] is False
    assert shift_stop_take_by_fill(100.0, -101.0, 95.0, 110.0)[2] is False
    assert shift_stop_take_by_fill(0, 101.0, 95.0, 110.0)[2] is False

def test_shift_precision():
    # Test rounding to 4 decimals
    planned = 100.0
    fill = 101.123456
    stop = 95.123456
    take = 110.123456
    new_stop, new_take, shifted = shift_stop_take_by_fill(planned, fill, stop, take)
    # delta = 1.123456
    # stop = 95.123456 + 1.123456 = 96.246912 -> 96.2469
    assert new_stop == 96.2469
    assert new_take == 111.2469
