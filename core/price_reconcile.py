"""Pure helpers for reconciling a position's stop/take with the real fill price.

When a proposal is generated, stop_px and take_px are computed against the
planned entry. Between the proposal and the actual fill at the broker (or
even just between the daily open and the slippage-adjusted entry in paper
trading) the price can move. Without a shift, the position opens with the
stale stop/take — distorting the R:R the user accepted.

These helpers are pure (no I/O, no async) so they can be unit-tested
without spinning up the executor pipeline. Async call sites live in
core/broker_executor.py, execution/paper_execution.py and
execution/evening.py.
"""
from __future__ import annotations

from typing import Tuple

# 26.08.2026: максимальное расхождение между запланированной ценой входа
# и реальной ценой исполнения, при превышении которого stop и take
# сдвигаются на дельту. Один и тот же порог для ISS, broker-execution и
# paper-execution. Изменять в одном месте, чтобы поведение не разъезжалось.
DEFAULT_DRIFT_THRESHOLD = 0.003  # 0.3 %


def shift_stop_take_by_fill(
    planned_entry_px: float,
    fill_px: float,
    stop_px: float | None,
    take_px: float | None,
    threshold: float = DEFAULT_DRIFT_THRESHOLD,
) -> Tuple[float | None, float | None, bool]:
    """Shift stop/take by the same delta as fill_px - planned_entry_px.

    Returns (new_stop, new_take, shifted). When shifted is False the inputs
    are returned unchanged. The shift preserves absolute distances so the
    R:R ratio the strategy picked stays the same.

    Rules:
      - all of fill_px, planned_entry_px, stop_px, take_px must be positive
      - threshold defaults to DEFAULT_DRIFT_THRESHOLD (0.3 %)
      - when |fill - planned| / planned <= threshold: no shift (returns
        inputs unchanged, shifted=False)
      - new_stop = stop_px + (fill_px - planned_entry_px), same for take
      - results are rounded to 4 decimals to match the precision used in
        broker_executor / broker_positions schema
    """
    if not (planned_entry_px and fill_px and stop_px and take_px):
        return stop_px, take_px, False
    if planned_entry_px <= 0 or fill_px <= 0:
        return stop_px, take_px, False
    drift_pct = abs(fill_px - planned_entry_px) / planned_entry_px
    if drift_pct <= threshold:
        return stop_px, take_px, False
    delta = fill_px - planned_entry_px
    return (
        round(stop_px + delta, 4),
        round(take_px + delta, 4),
        True,
    )


def describe_shift(
    proposal_id: int | str,
    ticker: str,
    side: str,
    planned_entry_px: float,
    fill_px: float,
    old_stop: float,
    old_take: float,
    new_stop: float,
    new_take: float,
    source: str = "broker",
) -> str:
    """Standard one-liner for the executor log; same shape across modules."""
    drift_pct = abs(fill_px - planned_entry_px) / planned_entry_px
    return (
        f"{source} proposal {proposal_id} {ticker} {side} fill drift "
        f"{drift_pct*100:.2f}% > {DEFAULT_DRIFT_THRESHOLD*100:.1f}% "
        f"(planned={planned_entry_px:.4f} fill={fill_px:.4f}); "
        f"stop {old_stop:.4f}->{new_stop:.4f}, "
        f"take {old_take:.4f}->{new_take:.4f}"
    )
