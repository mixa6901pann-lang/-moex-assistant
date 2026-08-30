"""Transport-agnostic bot service with all business logic.

Commands:
- screener: run technical screener over watchlist
- ticker: detailed indicator analysis for a single ticker
- trade: record a new trade in the journal
- positions: list open trades
- close: close a trade by ID
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from core import db
from core.moex import MoexClient
from core.fundamentals import fetch_fundamentals
from core.macro import get_macro_snapshot, compute_macro_bullish
from core.config import LLM_PROVIDER, PAPER_STARTING_CAPITAL
from strategies.indicators import df_from_candles, add_indicators, run_screener, detect_signals, score_stock
from strategies.backtest import Backtester
from strategies.risk import calculate_position, validate_trade, TradePlan
from strategies.signals import recommend_direction, DirectionAdvice, format_direction_emoji


@dataclass
class ScreenerResult:
    """DTO for screener output."""
    ticker: str
    score: float
    signals: list[str]
    details: dict


@dataclass
class TickerAnalysis:
    """DTO for ticker analysis output."""
    ticker: str
    close: float
    rsi: float
    macd_hist: float
    atr: float
    vol_ratio: float
    bb_pct: float
    sma_20: float
    sma_50: float
    signals: list[str]
    score: float
    trade_plan: TradePlan | None
    direction_advice: DirectionAdvice | None


@dataclass
class TradeRecord:
    """DTO for recorded trade."""
    trade_id: int
    ticker: str
    side: str
    entry_px: float
    stop_px: float | None
    target_px: float | None
    qty: int
    warnings: list[str]


@dataclass
class PositionSummary:
    """DTO for open position."""
    id: int
    ts: str
    ticker: str
    side: str
    entry_px: float
    stop_px: float | None
    target_px: float | None
    qty: int
    reason: str | None
    current_px: float | None = None
    pnl_pct: float | None = None
    pnl_rub: float | None = None


@dataclass
class CloseResult:
    """DTO for closed trade."""
    trade_id: int
    exit_px: float
    open_count: int


class BotService:
    """Core service with all command logic, independent of messenger."""

    def __init__(self):
        self.moex = MoexClient()

    async def close(self):
        await self.moex.close()

    # ── Commands ───────────────────────────────────────────────

    async def cmd_screener(self) -> list[ScreenerResult]:
        """Run screener and return top results."""
        async def fetch(ticker: str):
            return await self.moex.candles_recent(ticker, count=100)

        results = await run_screener(fetch)
        await db.save_screener(results)
        return [ScreenerResult(
            ticker=r["ticker"],
            score=r["score"],
            signals=r.get("signals", []),
            details=r.get("details", {}),
        ) for r in results]

    async def _macro_context(self) -> tuple[bool, dict]:
        """Fetch real macro indicators and decide if the broad market is bullish."""
        try:
            snapshot = await get_macro_snapshot()
            return compute_macro_bullish(snapshot), snapshot
        except Exception:
            return True, {}  # default to bullish if macro fetch fails

    async def cmd_ticker(self, ticker: str) -> TickerAnalysis | None:
        """Analyze single ticker and return structured data."""
        # Fetch daily candles, weekly candles, and fundamentals in parallel
        candles_task = self.moex.candles_recent(ticker, interval="1d", count=100)
        weekly_task = self.moex.candles_recent(ticker, interval="1w", count=50)
        candles, weekly_candles = await asyncio.gather(candles_task, weekly_task)
        if not candles:
            return None

        fund = await fetch_fundamentals(ticker)

        # Determine higher timeframe trend from weekly SMAs
        higher_tf_trend = None
        if weekly_candles and len(weekly_candles) >= 20:
            wdf = df_from_candles(weekly_candles)
            wdf = add_indicators(wdf)
            wlast = wdf.iloc[-1]
            if wlast.get("sma_20") and wlast.get("sma_50"):
                if wlast["close"] > wlast["sma_20"] > wlast["sma_50"]:
                    higher_tf_trend = "UPTREND"
                elif wlast["close"] < wlast["sma_20"] < wlast["sma_50"]:
                    higher_tf_trend = "DOWNTREND"
                else:
                    higher_tf_trend = "NEUTRAL"

        df = df_from_candles(candles)
        df = add_indicators(df)
        signals = detect_signals(df)
        result = score_stock(df, higher_tf_trend=higher_tf_trend, div_yield=fund.div_yield)
        d = result["details"]

        # Smart direction recommendation
        macro_bullish, macro_snapshot = await self._macro_context()
        advice = recommend_direction(
            ticker=ticker,
            signals=signals,
            score=result["score"],
            rsi=d.get("rsi", 50),
            macd_hist=d.get("macd_hist", 0),
            bb_pct=d.get("bb_pct", 0.5),
            sma_20=d.get("sma_20", 0),
            sma_50=d.get("sma_50", 0),
            close=d.get("close", 0),
            vol_ratio=d.get("vol_ratio", 1.0),
            macro_bullish=macro_bullish,
            adx=d.get("adx"),
            di_plus=d.get("di_plus"),
            di_minus=d.get("di_minus"),
            higher_tf_trend=higher_tf_trend,
            div_yield=fund.div_yield,
        )
        await db.save_prediction(
            ticker=ticker,
            predicted_direction=advice.direction,
            predicted_price=d.get("close", 0) or 0,
            predicted_strength=advice.strength,
            higher_tf_trend=higher_tf_trend,
            signals_used=advice.signals_used,
            llm_provider=LLM_PROVIDER,
            environment="paper",
        )

        # Paper trading: open/close on signal change
        if advice.direction in ("long", "short"):
            existing = await db.get_open_paper_position(ticker)
            current_price = d.get("close", 0) or 0
            if existing and existing["side"] != advice.direction:
                await db.close_paper_position(existing["id"], current_price, "signal_reverse")
                await db.open_paper_position(ticker, advice.direction, current_price, advice.signals_used)
            elif not existing:
                await db.open_paper_position(ticker, advice.direction, current_price, advice.signals_used)

        trade_plan = None
        if advice.direction in ("long", "short") and d.get("atr"):
            trade_plan = calculate_position(ticker, advice.direction, d["close"], d["atr"], equity=PAPER_STARTING_CAPITAL)

        return TickerAnalysis(
            ticker=ticker,
            close=d.get("close", 0),
            rsi=d.get("rsi", 0),
            macd_hist=d.get("macd_hist", 0),
            atr=d.get("atr", 0),
            vol_ratio=d.get("vol_ratio", 0),
            bb_pct=d.get("bb_pct", 0),
            sma_20=d.get("sma_20", 0),
            sma_50=d.get("sma_50", 0),
            signals=signals,
            score=result["score"],
            trade_plan=trade_plan,
            direction_advice=advice,
        )

    async def cmd_advice(self, ticker: str) -> str:
        """Concise trading advice for a single ticker."""
        analysis = await self.cmd_ticker(ticker)
        if analysis is None or analysis.direction_advice is None:
            return f"Нет данных по {ticker}"

        adv = analysis.direction_advice
        text = f"📊 Совет по {ticker}\n\n"
        text += format_direction_emoji(adv.direction, adv.strength) + "\n"
        text += f"Причина: {adv.reason}\n"
        if adv.risk_reward:
            text += f"Риск/прибыль: 1:{adv.risk_reward}\n"
        if adv.stop_pct:
            text += f"Рекомендуемый стоп: -{adv.stop_pct}% от входа\n"

        if analysis.trade_plan:
            plan = analysis.trade_plan
            text += f"\n💡 План:\n"
            text += f"  Вход: {plan.entry_px}₽ | Стоп: {plan.stop_px}₽ | Цель: {plan.target_px}₽"
            text += f"\n  Кол-во: {plan.qty} | Риск: {plan.risk_rub}₽ ({plan.risk_pct}%)"

        if adv.warnings:
            text += "\n\n⚠️ Важно:\n"
            for w in adv.warnings:
                text += f"• {w}\n"

        text += f"\nТехнические сигналы: {', '.join(adv.signals_used) if adv.signals_used else 'нет'}"
        return text

    async def cmd_trade(
        self,
        ticker: str,
        side: str,
        entry_px: float,
        stop_px: float | None = None,
        target_px: float | None = None,
        qty: int = 1,
    ) -> TradeRecord:
        """Record a new trade and validate risk."""
        trade_id = await db.add_trade(ticker, side, entry_px, qty, stop_px, target_px)

        positions = await db.open_positions()
        plan = TradePlan(ticker, side, entry_px, stop_px or 0, target_px or 0, qty, 0, 0)
        warnings = validate_trade(plan, positions, equity=PAPER_STARTING_CAPITAL)

        return TradeRecord(
            trade_id=trade_id,
            ticker=ticker,
            side=side,
            entry_px=entry_px,
            stop_px=stop_px,
            target_px=target_px,
            qty=qty,
            warnings=warnings,
        )

    async def cmd_positions(self) -> list[PositionSummary]:
        """Return all open positions with current market prices and PnL."""
        rows = await db.open_positions()
        results = []
        for r in rows:
            current = await self.moex.last_price(r["ticker"])
            entry = r["entry_px"]
            side = r["side"]
            qty = r["qty"]
            pnl_pct = None
            pnl_rub = None
            if current:
                if side == "long":
                    pnl_pct = round((current - entry) / entry * 100, 2)
                else:
                    pnl_pct = round((entry - current) / entry * 100, 2)
                pnl_rub = round((current - entry) * qty, 2) if side == "long" else round((entry - current) * qty, 2)
            results.append(PositionSummary(
                id=r["id"],
                ts=r["ts"],
                ticker=r["ticker"],
                side=side,
                entry_px=entry,
                stop_px=r.get("stop_px"),
                target_px=r.get("target_px"),
                qty=qty,
                reason=r.get("reason"),
                current_px=current,
                pnl_pct=pnl_pct,
                pnl_rub=pnl_rub,
            ))
        return results

    async def cmd_close(self, trade_id: int, exit_px: float) -> CloseResult:
        """Close a trade by ID."""
        await db.close_trade(trade_id, exit_px)
        positions = await db.open_positions()
        return CloseResult(trade_id=trade_id, exit_px=exit_px, open_count=len(positions))

    async def cmd_close_market(self, trade_id: int) -> CloseResult:
        """Close a trade by ID at current market price."""
        rows = await db.open_positions()
        trade = next((r for r in rows if r["id"] == trade_id), None)
        if not trade:
            raise ValueError(f"Позиция #{trade_id} не найдена")
        current_px = await self.moex.last_price(trade["ticker"])
        if not current_px:
            raise ValueError(f"Не удалось получить текущую цену {trade['ticker']}")
        await db.close_trade(trade_id, current_px)
        positions = await db.open_positions()
        return CloseResult(trade_id=trade_id, exit_px=current_px, open_count=len(positions))

    async def cmd_backtest(self, ticker: str, days: int = 365) -> str:
        """Run signal-based backtest on historical candles and return summary."""
        candles = await self.moex.candles(ticker, interval="1d", limit=days + 50)
        if not candles or len(candles) < 60:
            return f"Недостаточно данных для бэктеста {ticker}"
        from strategies.indicators import df_from_candles
        df = df_from_candles(candles)
        bt = Backtester(lookback=50)
        result = bt.run(df, ticker=ticker, initial=PAPER_STARTING_CAPITAL, commission=0.0005)
        return result.summary()

    # ── Formatting helpers (plain text) ──────────────────────────

    @staticmethod
    def format_screener(results: list[ScreenerResult]) -> str:
        if not results:
            return "Скринер не нашёл акций с сигналами."
        lines = ["📊 ТОП акций по сигналам:", ""]
        for i, r in enumerate(results[:10], 1):
            d = r.details
            sigs = ", ".join(r.signals) or "—"
            lines.append(
                f"{i}. {r.ticker} {d.get('close', '?')}₽ | "
                f"RSI {d.get('rsi', '?')} | Vol {d.get('vol_ratio', '?')}x\n"
                f"   Сигналы: {sigs} | Очков: {r.score}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_ticker(a: TickerAnalysis) -> str:
        # Direction badge at the top
        if a.direction_advice:
            badge = format_direction_emoji(a.direction_advice.direction, a.direction_advice.strength)
            text = f"{badge}\n📈 {a.ticker}\n"
        else:
            text = f"📈 {a.ticker}\n"

        text += f"Цена: {a.close}₽\n"
        text += f"RSI(14): {a.rsi}\n"
        text += f"MACD hist: {a.macd_hist}\n"
        text += f"ATR: {a.atr}\n"
        text += f"Объём/средний: {a.vol_ratio}x\n"
        text += f"BB %B: {a.bb_pct}\n"
        text += f"SMA 20: {a.sma_20} | SMA 50: {a.sma_50}\n"
        text += f"\nСигналы: {', '.join(a.signals) if a.signals else 'нет'}\n"
        text += f"Очков скринера: {a.score}"

        if a.direction_advice:
            adv = a.direction_advice
            text += f"\n\n📋 Обоснование:\n{adv.reason}"
            if adv.risk_reward:
                text += f"\nРиск/прибыль: 1:{adv.risk_reward}"
            if adv.warnings:
                text += "\n\n⚠️ Важно:\n" + "\n".join(f"• {w}" for w in adv.warnings)

        if a.trade_plan:
            plan = a.trade_plan
            text += f"\n\n💡 Предлагаемый план ({plan.side}):"
            text += f"\n  Вход: {plan.entry_px}₽ | Стоп: {plan.stop_px}₽ | Цель: {plan.target_px}₽"
            text += f"\n  Кол-во: {plan.qty} | Риск: {plan.risk_rub}₽ ({plan.risk_pct}%)"
        return text

    @staticmethod
    def format_trade(t: TradeRecord) -> str:
        text = f"✅ Сделка #{t.trade_id} записана:\n"
        text += f"{t.side.upper()} {t.ticker} @ {t.entry_px}₽\n"
        if t.stop_px:
            text += f"Стоп: {t.stop_px}₽\n"
        if t.target_px:
            text += f"Цель: {t.target_px}₽\n"
        text += f"Кол-во: {t.qty}"
        if t.warnings:
            text += "\n\n⚠️ Предупреждения:\n" + "\n".join(f"• {w}" for w in t.warnings)
        return text

    @staticmethod
    def format_positions(positions: list[PositionSummary]) -> str:
        if not positions:
            return "Нет открытых позиций."
        lines = ["📋 Открытые позиции:", ""]
        total_pnl = 0.0
        for p in positions:
            if p.current_px:
                emoji = "🟢" if (p.pnl_pct or 0) >= 0 else "🔴"
                pnl_line = f"{emoji} {p.pnl_pct:+.2f}% ({p.pnl_rub:+.2f}₽)"
                price_line = f"Текущая: {p.current_px}₽"
            else:
                pnl_line = "⏳ Цена не загружена"
                price_line = ""
            lines.append(
                f"#{p.id} {p.side.upper()} {p.ticker} @ {p.entry_px}₽\n"
                f"  {price_line}\n"
                f"  {pnl_line} | Стоп: {p.stop_px or '—'} | Цель: {p.target_px or '—'} | Кол-во: {p.qty}"
            )
            if p.pnl_rub:
                total_pnl += p.pnl_rub
        if len(positions) > 1:
            emoji = "🟢" if total_pnl >= 0 else "🔴"
            lines.append(f"\n{'='*20}\n{emoji} Общий PnL: {total_pnl:+.2f}₽")
        return "\n".join(lines)

    @staticmethod
    def format_close(r: CloseResult) -> str:
        return f"✅ Сделка #{r.trade_id} закрыта по {r.exit_px}₽\n\nТеперь открытых позиций: {r.open_count}"

    # ── Proactive reports ──────────────────────────────────────

    async def morning_report(self) -> str:
        """Generate morning watchlist text."""
        async def fetch(ticker: str):
            return await self.moex.candles(ticker, interval="1d", limit=100)

        results = await run_screener(fetch)
        await db.save_screener(results)

        lines = ["🌅 Утренняя сводка", ""]
        for i, r in enumerate(results[:10], 1):
            d = r.get("details", {})
            sigs = ", ".join(r.get("signals", [])) or "—"
            lines.append(f"{i}. {r['ticker']} {d.get('close', '?')}₽ | RSI {d.get('rsi', '?')} | {sigs}")

        positions = await db.open_positions()
        if positions:
            lines.append("\n📋 Позиции:")
            for p in positions:
                lines.append(f"  {p['side'].upper()} {p['ticker']} @ {p['entry_px']}₽")

        return "\n".join(lines)

    async def evening_report(self) -> str:
        """Generate evening summary text."""
        positions = await db.open_positions()
        closed_today = []  # could filter by exit_ts date
        lines = ["🌆 Вечерняя сводка", ""]
        if closed_today:
            lines.append("Сделки сегодня:")
            for c in closed_today:
                lines.append(f"  #{c['id']} {c['ticker']} PnL {c.get('pnl', '?')}₽")
        else:
            lines.append("Сделок сегодня не было.")
        lines.append(f"\nОткрытых позиций: {len(positions)}")
        if positions:
            for p in positions:
                lines.append(f"  {p['side'].upper()} {p['ticker']} @ {p['entry_px']}₽")
        return "\n".join(lines)

    async def alert_text(self, ticker: str, message: str) -> str:
        return f"🚨 {ticker}: {message}"
