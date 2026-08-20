"""Risk management: position sizing, stop-loss, drawdown limits."""

from __future__ import annotations

from dataclasses import dataclass

from core.config import MAX_POSITION_PCT, MAX_OPEN_POSITIONS, STOP_LOSS_ATR_MULT, TARGET_RR, TICKER_SECTORS


@dataclass
class TradePlan:
    """Result of risk calculation for a potential trade."""
    ticker: str
    side: str  # "long" or "short"
    entry_px: float
    stop_px: float
    target_px: float
    qty: int
    risk_rub: float
    risk_pct: float

    def risk_reward(self) -> float | None:
        if self.stop_px == self.entry_px:
            return None
        risk = abs(self.entry_px - self.stop_px)
        reward = abs(self.target_px - self.entry_px)
        return reward / risk if risk > 0 else None


def calculate_position(
    ticker: str,
    side: str,
    entry_px: float,
    atr: float,
    equity: float,
    target_rr: float = TARGET_RR,
    max_pct: float = MAX_POSITION_PCT,
    atr_mult: float = STOP_LOSS_ATR_MULT,
) -> TradePlan:
    """Calculate position size and stop/target based on ATR and risk rules.

    Args:
        ticker: Stock ticker
        side: 'long' or 'short'
        entry_px: Planned entry price
        atr: Current ATR(14) value
        equity: Total portfolio equity in RUB
        target_rr: Minimum risk/reward ratio for target
        max_pct: Max % of equity to risk per trade
        atr_mult: ATR multiplier for stop-loss distance
    """
    risk_per_share = atr * atr_mult

    if side == "long":
        stop_px = entry_px - risk_per_share
        target_px = entry_px + risk_per_share * target_rr
    else:
        stop_px = entry_px + risk_per_share
        target_px = entry_px - risk_per_share * target_rr

    max_risk_rub = equity * (max_pct / 100)
    qty = int(max_risk_rub / risk_per_share) if risk_per_share > 0 else 0
    qty = max(qty, 1)  # at least 1 share

    actual_risk_rub = risk_per_share * qty
    actual_risk_pct = actual_risk_rub / equity * 100 if equity > 0 else 0

    return TradePlan(
        ticker=ticker,
        side=side,
        entry_px=round(entry_px, 2),
        stop_px=round(stop_px, 2),
        target_px=round(target_px, 2),
        qty=qty,
        risk_rub=round(actual_risk_rub, 2),
        risk_pct=round(actual_risk_pct, 2),
    )


def check_correlation(
    ticker: str,
    open_positions: list[dict],
    sector_map: dict[str, str] | None = None,
) -> list[str]:
    """Warn if new position correlates with existing ones.

    Uses a simple sector-based check. For proper correlation you'd
    need a correlation matrix of returns.
    """
    if sector_map is None:
        sector_map = TICKER_SECTORS

    new_sector = sector_map.get(ticker, "unknown")
    same_sector = [
        p["ticker"] for p in open_positions
        if sector_map.get(p.get("ticker", ""), "unknown") == new_sector
    ]
    return same_sector


def validate_trade(
    plan: TradePlan,
    open_positions: list[dict],
    equity: float,
    max_positions: int = MAX_OPEN_POSITIONS,
    max_pct: float = MAX_POSITION_PCT,
) -> list[str]:
    """Validate a trade plan against risk rules. Returns list of warnings."""
    warnings: list[str] = []

    # Too many open positions
    if len(open_positions) >= max_positions:
        warnings.append(f"Слишком много открытых позиций ({len(open_positions)}/{max_positions})")

    # Risk per trade too high
    if plan.risk_pct > max_pct:
        warnings.append(f"Риск на сделку {plan.risk_pct}% превышает лимит {max_pct}%")

    # Bad risk/reward
    rr = plan.risk_reward()
    if rr is not None and rr < 1.5:
        warnings.append(f"Риск/прибыль {rr:.1f} ниже минимального 1.5")

    # Correlated positions
    same_sector = check_correlation(plan.ticker, open_positions)
    if same_sector:
        warnings.append(f"Уже есть позиции в том же секторе: {', '.join(same_sector)}")

    return warnings