"""Commission and cost calculator for Russian brokers.

Used by paper trading and (later) real broker adapter to decide whether
a signal is worth executing after fees are taken into account.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import (
    DEFAULT_BROKER_COMMISSION_PCT,
    MIN_PROFIT_PCT_AFTER_FEES,
    OVERNIGHT_CARRY_FEE_PCT,
    DEFAULT_SLIPPAGE_PCT,
    ILLIQUID_SLIPPAGE_PCT,
    ILLIQUID_SPREAD_PCT,
)


# T-Bank "Trader" fixed daily carry fee by position notional tier (RUB).
# Source: screenshot from user 2026-07-25. Free tiers omitted; robot uses
# borrowed money, so fee applies if notional is above the free threshold.
_TINKOFF_CARRY_FEE_TIERS = [
    (5_000, 0),          # up to 5k RUB on first 3 accounts free
    (50_000, 5),         # next bracket shown as "до 50 тыс ₽" -> 40 ₽,
                         # but we start from the tier just above 5k
    (100_000, 40),
    (250_000, 75),
    (500_000, 180),
    (1_000_000, 350),
    (2_500_000, 700),
    (5_000_000, 1_750),
    (10_000_000, 3_500),
    (25_000_000, 6_900),
    (50_000_000, 25_000_000 * 0.00068),
    (float("inf"), 50_000_000 * 0.00057),
]


def _fixed_carry_fee_rub(notional: float) -> float:
    """Return T-Bank fixed daily carry fee for a position notional (RUB)."""
    previous_limit = 0.0
    for limit, fee in _TINKOFF_CARRY_FEE_TIERS:
        if notional <= limit:
            return float(fee)
        previous_limit = limit
    return float(_TINKOFF_CARRY_FEE_TIERS[-1][1])


@dataclass(frozen=True)
class FeeEstimate:
    """Result of a commission/profit estimate."""

    entry_value: float
    exit_value: float
    commission_pct: float
    commission_buy_rub: float
    commission_sell_rub: float
    total_commission_rub: float
    carry_fee_rub: float
    hold_days: int
    gross_profit_pct: float | None
    net_profit_pct: float | None
    net_profit_rub: float | None
    worth_it: bool
    notes: list[str]


def estimate_trade_costs(
    entry_px: float,
    exit_px: float | None,
    qty: int,
    commission_pct: float | None = None,
    min_profit_pct_after_fees: float | None = None,
    side: str = "long",
    hold_days: int = 0,
    use_fixed_carry_fee: bool = True,
) -> FeeEstimate:
    """Estimate round-trip commission, carry fee and net profit for a trade.

    Args:
        entry_px: Entry price per share.
        exit_px: Expected exit price (None when only entry cost is known).
        qty: Number of shares.
        commission_pct: Broker commission as a percent of turnover.
            Defaults to core.config.DEFAULT_BROKER_COMMISSION_PCT.
        min_profit_pct_after_fees: Minimum net profit % required to consider
            the trade "worth it". Defaults to config value.
        side: Trade direction ('long', 'buy', 'short', 'sell').
        hold_days: Number of overnight carries (0 for intraday). For a trade
            opened today and closed tomorrow, use 1.
        use_fixed_carry_fee: If True, use T-Bank fixed tier fee per day.
            If False, fall back to OVERNIGHT_CARRY_FEE_PCT of notional.

    Returns:
        FeeEstimate with all cost breakdowns.
    """
    commission_pct = commission_pct if commission_pct is not None else DEFAULT_BROKER_COMMISSION_PCT
    min_profit_pct = min_profit_pct_after_fees if min_profit_pct_after_fees is not None else MIN_PROFIT_PCT_AFTER_FEES

    notes: list[str] = []

    hold_days = max(int(hold_days), 0)

    if entry_px <= 0 or qty <= 0:
        return FeeEstimate(
            entry_value=0.0,
            exit_value=0.0,
            commission_pct=commission_pct,
            commission_buy_rub=0.0,
            commission_sell_rub=0.0,
            total_commission_rub=0.0,
            carry_fee_rub=0.0,
            hold_days=hold_days,
            gross_profit_pct=None,
            net_profit_pct=None,
            net_profit_rub=None,
            worth_it=False,
            notes=["Некорректная цена или количество"],
        )

    entry_value = entry_px * qty
    commission_buy_rub = entry_value * (commission_pct / 100)

    if exit_px is None or exit_px <= 0:
        return FeeEstimate(
            entry_value=round(entry_value, 2),
            exit_value=0.0,
            commission_pct=commission_pct,
            commission_buy_rub=round(commission_buy_rub, 2),
            commission_sell_rub=0.0,
            total_commission_rub=round(commission_buy_rub, 2),
            carry_fee_rub=0.0,
            hold_days=hold_days,
            gross_profit_pct=None,
            net_profit_pct=None,
            net_profit_rub=None,
            worth_it=False,
            notes=["Цена выхода неизвестна — оценка только на вход"],
        )

    exit_value = exit_px * qty
    commission_sell_rub = exit_value * (commission_pct / 100)
    total_commission_rub = commission_buy_rub + commission_sell_rub

    # Overnight carry fee: fixed tier by default, percentage fallback.
    if use_fixed_carry_fee:
        daily_carry = _fixed_carry_fee_rub(entry_value)
    else:
        daily_carry = entry_value * (OVERNIGHT_CARRY_FEE_PCT / 100)
    carry_fee_rub = daily_carry * hold_days

    is_long = side in ("long", "buy")
    gross_profit_rub = (exit_value - entry_value) if is_long else (entry_value - exit_value)
    gross_profit_pct = (gross_profit_rub / entry_value) * 100
    net_profit_rub = gross_profit_rub - total_commission_rub - carry_fee_rub
    net_profit_pct = (net_profit_rub / entry_value) * 100

    worth_it = net_profit_pct >= min_profit_pct

    if not worth_it:
        notes.append(
            f"Чистая прибыль {net_profit_pct:.2f}% ниже минимума {min_profit_pct:.2f}% после комиссии и платы за перенос"
        )

    if carry_fee_rub > 0:
        notes.append(
            f"Плата за перенос: {carry_fee_rub:.2f} ₽ ({hold_days} дн × {daily_carry:.2f} ₽/день)"
        )

    return FeeEstimate(
        entry_value=round(entry_value, 2),
        exit_value=round(exit_value, 2),
        commission_pct=commission_pct,
        commission_buy_rub=round(commission_buy_rub, 2),
        commission_sell_rub=round(commission_sell_rub, 2),
        total_commission_rub=round(total_commission_rub, 2),
        carry_fee_rub=round(carry_fee_rub, 2),
        hold_days=hold_days,
        gross_profit_pct=round(gross_profit_pct, 2),
        net_profit_pct=round(net_profit_pct, 2),
        net_profit_rub=round(net_profit_rub, 2),
        worth_it=worth_it,
        notes=notes,
    )


def estimate_broker_commission(
    turnover_rub: float,
    commission_pct: float | None = None,
) -> float:
    """Return commission in rubles for a single turnover amount."""
    commission_pct = commission_pct if commission_pct is not None else DEFAULT_BROKER_COMMISSION_PCT
    return round(turnover_rub * (commission_pct / 100), 2)


def estimate_slippage_pct(
    spread_pct: float | None = None,
    base_slippage_pct: float | None = None,
) -> float:
    """Estimate one-way slippage % based on liquidity proxy.

    Args:
        spread_pct: Best bid/ask spread as percent of mid price. If unknown,
            the base slippage is used.
        base_slippage_pct: Default slippage for liquid stocks.

    Returns:
        Estimated one-way slippage percentage. For longs this worsens the
        entry price (entry_px * (1 + slippage)); for shorts it improves it
        slightly less (entry_px * (1 - slippage/2)) because short sales at
        the open usually get the bid.
    """
    base = base_slippage_pct if base_slippage_pct is not None else DEFAULT_SLIPPAGE_PCT
    if spread_pct is None:
        return base
    # Widen slippage when spread is wide; keep at least the base level.
    return max(base, min(spread_pct, ILLIQUID_SLIPPAGE_PCT))


def apply_slippage(
    price: float,
    side: str = "long",
    spread_pct: float | None = None,
) -> float:
    """Apply realistic one-way slippage to a planned fill price.

    Longs: fill slightly above the target (worse for buyer).
    Shorts: fill slightly below the target (worse for seller).
    """
    if price <= 0:
        return price
    slippage = estimate_slippage_pct(spread_pct=spread_pct)
    is_long = side in ("long", "buy")
    multiplier = 1 + slippage / 100 if is_long else 1 - slippage / 100
    return round(price * multiplier, 4)
