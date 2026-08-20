"""Shared execution pipeline: signal -> sizing -> fee/risk check -> proposal/order.

This module extracts the common decision path duplicated across intraday,
evening, and medium-term flows in main.py. It keeps the same behavior but
makes the steps explicit, reusable, and easier to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

import core.config as app_config
from core.config import (
    PAPER_STARTING_CAPITAL,
    PAPER_TRADING,
    SEMI_AUTO_TRADING,
    TINKOFF_SANDBOX,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_SIZE_PCT,
    MIN_POSITION_SIZE_PCT,
    STOP_LOSS_ATR_MULT,
    TRAILING_STOP_ATR_MULT,
    DEFAULT_BROKER_COMMISSION_PCT,
    MIN_PROFIT_PCT_AFTER_FEES,
    TICKER_SECTORS,
)
from core import db
from strategies.fees import estimate_trade_costs, FeeEstimate
from strategies.risk import calculate_position


def _sector_for_ticker(ticker: str) -> str | None:
    return TICKER_SECTORS.get(ticker.upper())


def _sector_impact(geo: dict | None, sector: str | None) -> dict | None:
    if not sector or not geo:
        return None
    for s in geo.get("affected_sectors") or []:
        if isinstance(s, dict) and s.get("sector") == sector:
            return s
    return None


@dataclass
class SizingPlan:
    """Result of position sizing for a planned trade."""

    qty: int
    stop_px: float | None
    take_px: float | None
    initial_atr: float | None
    atr_mult: float


@dataclass
class PipelineResult:
    """Result of running the execution pipeline."""

    ok: bool
    reason: str
    proposal_id: int | None = None
    fee_estimate: FeeEstimate | None = None
    sizing: SizingPlan | None = None
    proposal_mode: str | None = None


class ExecutionPipeline:
    """Shared helper for turning a directional signal into a robot proposal.

    The pipeline does NOT execute real broker orders — it only creates or
    rejects proposals. Broker execution remains in main.py `broker_order_executor`.
    """

    def __init__(self, moex_client, tinkoff_client):
        self.moex = moex_client
        self.tinkoff = tinkoff_client

    async def size_position(
        self,
        ticker: str,
        side: str,
        entry_px: float,
        atr: float | None,
        equity: float | None = None,
        atr_mult: float = STOP_LOSS_ATR_MULT,
        use_trailing_stop: bool = True,
    ) -> SizingPlan:
        """Compute qty, stop and take prices from risk rules.

        Caps notional exposure at MAX_POSITION_SIZE_PCT and filters out positions
        below MIN_POSITION_SIZE_PCT of equity.
        """
        if equity is None:
            equity = PAPER_STARTING_CAPITAL

        effective_atr = atr if atr and atr > 0 else entry_px * 0.03

        plan = calculate_position(
            ticker, side, entry_px, effective_atr, equity, atr_mult=atr_mult
        )
        planned_qty = max(plan.qty, 1)

        # Cap at max position value % of equity.
        max_value = equity * (MAX_POSITION_SIZE_PCT / 100)
        if entry_px > 0 and planned_qty * entry_px > max_value:
            planned_qty = max(int(max_value / entry_px), 1)

        # Enforce minimum position value % of equity.
        min_value = equity * (MIN_POSITION_SIZE_PCT / 100)
        if entry_px > 0 and planned_qty * entry_px < min_value:
            planned_qty = max(int(min_value / entry_px), 1)

        return SizingPlan(
            qty=planned_qty,
            stop_px=plan.stop_px,
            take_px=plan.target_px,
            initial_atr=effective_atr,
            atr_mult=TRAILING_STOP_ATR_MULT if use_trailing_stop else atr_mult,
        )

    async def check_fees(
        self,
        entry_px: float,
        take_px: float | None,
        qty: int,
        side: str,
        hold_days: int = 0,
    ) -> FeeEstimate:
        """Estimate fees including overnight carry when hold_days > 0."""
        if take_px is None or take_px <= 0:
            return estimate_trade_costs(
                entry_px, None, qty, side=side, hold_days=hold_days
            )
        return estimate_trade_costs(
            entry_px,
            take_px,
            qty,
            side=side,
            hold_days=hold_days,
            commission_pct=DEFAULT_BROKER_COMMISSION_PCT,
            min_profit_pct_after_fees=MIN_PROFIT_PCT_AFTER_FEES,
        )

    async def resolve_equity(self, proposal_mode: str) -> float:
        """Return equity to use for position sizing.

        For paper proposals use the configured paper capital. For real
        proposals try to fetch the broker portfolio value.
        """
        if PAPER_TRADING or proposal_mode == "paper":
            return PAPER_STARTING_CAPITAL

        try:
            portfolio = await self.tinkoff.get_portfolio()
            return portfolio.total_value_rub or PAPER_STARTING_CAPITAL
        except Exception as exc:
            logger.warning(f"Could not fetch Tinkoff portfolio for equity: {exc}")
            return PAPER_STARTING_CAPITAL

    def resolve_proposal_mode(self) -> str:
        """Determine proposal mode from global trading configuration.

        Sandbox is never allowed to emit fully-automatic "live" proposals,
        because "live" means real-money broker orders. When connected to the
        Tinkoff sandbox, the most aggressive mode is semi-auto so the user
        confirms before the sandbox broker adapter places an order.
        """
        if PAPER_TRADING:
            return "paper"
        if TINKOFF_SANDBOX:
            return "semi_auto"
        if SEMI_AUTO_TRADING:
            return "semi_auto"
        if app_config.AUTO_TRADING_ENABLED:
            return "live"
        return "semi_auto"

    async def run(
        self,
        ticker: str,
        side: str,
        entry_px: float,
        take_px: float | None,
        stop_px: float | None,
        atr: float | None,
        source: str,
        signal: str,
        confidence: int,
        reason: str,
        horizon: str | None = None,
        hold_days: int = 0,
        atr_mult: float = STOP_LOSS_ATR_MULT,
        use_trailing_stop: bool = True,
        proposal_mode: str | None = None,
        extra_db_kwargs: dict[str, Any] | None = None,
    ) -> PipelineResult:
        """Run the full signal-to-proposal pipeline.

        Steps:
        1. Check global open-position limit.
        2. Check GeoRisk sector signal (block counter-geo trades).
        3. Check short availability for new shorts.
        4. Determine equity and size the position.
        5. Estimate fees (commission + carry if hold_days > 0).
        6. Skip if fees eat profit.
        7. Save a robot proposal.

        Returns PipelineResult with ok/reason and proposal_id when saved.
        """
        # 1. Global open-position limit.
        open_positions = await db.get_open_paper_positions()
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            return PipelineResult(
                ok=False,
                reason=f"Open position limit reached ({MAX_OPEN_POSITIONS})",
            )

        # 2. GeoRisk sector guard: do not fight the geo signal.
        geo = await db.get_latest_georisk()
        sector = _sector_for_ticker(ticker)
        impact = _sector_impact(geo, sector)
        sector_dir = impact.get("direction", 0) if impact else 0
        overall_direction = geo.get("overall_direction", 0) if geo else 0

        if side in ("long", "buy"):
            if sector_dir == -1:
                return PipelineResult(
                    ok=False,
                    reason=f"GeoRisk: {sector} is negatively impacted, blocking long {ticker}",
                )
            if overall_direction == -1 and sector_dir != 1:
                return PipelineResult(
                    ok=False,
                    reason=f"GeoRisk: overall direction is bearish, blocking long {ticker}",
                )

        if side in ("short", "sell"):
            if sector_dir == 1:
                return PipelineResult(
                    ok=False,
                    reason=f"GeoRisk: {sector} is positively impacted, blocking short {ticker}",
                )
            if overall_direction == 1 and sector_dir != -1:
                return PipelineResult(
                    ok=False,
                    reason=f"GeoRisk: overall direction is bullish, blocking short {ticker}",
                )

        # 3. Short availability guard (live only; sandbox often reports short_enabled=false).
        if side in ("short", "sell") and not self.tinkoff.sandbox:
            instr = await self.tinkoff.find_instrument(ticker)
            if instr and not instr.get("short_enabled", False):
                return PipelineResult(
                    ok=False,
                    reason=f"Short selling not enabled for {ticker}",
                )

        # 4. Proposal mode and equity.
        if proposal_mode is None:
            proposal_mode = self.resolve_proposal_mode()
        equity = await self.resolve_equity(proposal_mode)

        # 5. Size position.
        sizing = await self.size_position(
            ticker=ticker,
            side=side,
            entry_px=entry_px,
            atr=atr,
            equity=equity,
            atr_mult=atr_mult,
            use_trailing_stop=use_trailing_stop,
        )
        if stop_px is not None:
            sizing.stop_px = stop_px

        # 6. Fee check.
        fee_est = await self.check_fees(
            entry_px=entry_px,
            take_px=sizing.take_px,
            qty=sizing.qty,
            side=side,
            hold_days=hold_days,
        )
        if not fee_est.worth_it:
            return PipelineResult(
                ok=False,
                reason=(
                    f"Fees eat profit/carry ({fee_est.net_profit_pct}% net, "
                    f"commission {fee_est.total_commission_rub} RUB, "
                    f"carry {fee_est.carry_fee_rub} RUB)"
                ),
                fee_estimate=fee_est,
                sizing=sizing,
                proposal_mode=proposal_mode,
            )

        # 7. Save proposal.
        db_kwargs = {
            "ticker": ticker,
            "side": side,
            "source": source,
            "signal": signal,
            "entry_px": entry_px,
            "qty": sizing.qty,
            "stop_px": sizing.stop_px,
            "take_px": sizing.take_px,
            "confidence": confidence,
            "reason": reason,
            "fee_rub": fee_est.total_commission_rub,
            "net_profit_pct": fee_est.net_profit_pct,
            "horizon": horizon,
            "proposal_mode": proposal_mode,
            "initial_atr": sizing.initial_atr,
            "atr_mult": sizing.atr_mult,
        }
        if extra_db_kwargs:
            db_kwargs.update(extra_db_kwargs)

        proposal_id = await db.save_robot_proposal(**db_kwargs)
        return PipelineResult(
            ok=True,
            reason="Proposal saved",
            proposal_id=proposal_id,
            fee_estimate=fee_est,
            sizing=sizing,
            proposal_mode=proposal_mode,
        )
