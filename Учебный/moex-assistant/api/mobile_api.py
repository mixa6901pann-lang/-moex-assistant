"""Mobile REST API — serves screener, ticker, and position data."""
from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

import pandas as pd
from loguru import logger
from fastapi import FastAPI, Depends, HTTPException, Request, Query, BackgroundTasks, Header
from fastapi.responses import FileResponse

from core import db
from core.moex import MoexClient
import core.config as app_config
from core.config import (
    WATCHLIST, LLM_PROVIDER, OLLAMA_URL, OLLAMA_MODEL, API_KEY, MAX_OPEN_POSITIONS,
    PAPER_STARTING_CAPITAL, MAX_POSITION_SIZE_PCT, MIN_POSITION_SIZE_PCT,
    TINKOFF_SANDBOX, PAPER_TRADING, SEMI_AUTO_TRADING,
    STOP_LOSS_ATR_MULT, TRAILING_STOP_ATR_MULT, should_auto_trade,
)
from core.analyzer import analyze_ticker
from core.fundamentals import fetch_fundamentals
from core.sentiment_agent import agent as sentiment_agent
from core.intraday_agent import agent as intraday_agent
from core.macro import get_macro_snapshot, compute_macro_bullish
from core.llm import get_last_used_provider
from strategies.indicators import df_from_candles, add_indicators, detect_signals, score_stock, run_screener, compute_levels, compute_volume_profile, predict_bounce, resample_1m_to_5m
from strategies.signals import recommend_direction, format_direction_emoji
from strategies.fees import estimate_trade_costs
import time

app = FastAPI(title="MOEX Assistant Mobile API")

# Simple TTL cache for LLM analysis: ticker -> (timestamp, primary_text, gemma_critique)
_LLM_CACHE: dict[str, tuple[float, str, str | None]] = {}
_LLM_CACHE_TTL = 300  # 5 minutes




def _to_native(value):
    """Recursively convert numpy scalars to plain Python types for JSON serialization."""
    import numpy as np
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {k: _to_native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_native(v) for v in value]
    return value


def _persist_tinkoff_account_id(new_account_id: str) -> None:
    """Write TINKOFF_ACCOUNT_ID into /root/moex/.env so next service restart
    picks it up. Best-effort: silently no-op if the file is not writable."""
    import os
    import re
    from pathlib import Path
    candidates = [
        Path("/root/moex/.env"),
        Path("/root/moex/.env.server"),
        Path(os.getcwd()) / ".env.server",
    ]
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        new_text, count = re.subn(
            r"^(TINKOFF_ACCOUNT_ID=).*$",
            lambda m: f"{m.group(1)}{new_account_id}",
            text,
            flags=re.MULTILINE,
        )
        if count == 0:
            if not text.endswith("\n"):
                text = text + "\n"
            new_text = text + f"TINKOFF_ACCOUNT_ID={new_account_id}\n"
        try:
            path.write_text(new_text, encoding="utf-8")
            logger.info(f"Persisted new TINKOFF_ACCOUNT_ID to {path}")
        except OSError as exc:
            logger.warning(f"Could not write {path}: {exc}")
        return


def _resolve_proposal_mode() -> str:
    """Mirror of execution.pipeline.resolve_proposal_mode for API-created proposals.

    Sandbox is never allowed to emit fully-automatic "live" proposals, because
    "live" means real-money broker orders. When connected to the Tinkoff sandbox,
    the most aggressive mode is semi-auto so the user confirms before the sandbox
    broker adapter places an order.
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


# Serve frontend
MOBILE_DIR = Path(__file__).resolve().parent.parent / "mobile"


async def require_api_key(x_api_key: str = Header(None, alias="x-api-key")) -> None:
    """Validate the master API key on write endpoints.

    Returns 503 if no key is configured on the server, 401 if the caller
    provides a missing or wrong key.
    """
    if not API_KEY:
        raise HTTPException(status_code=503, detail="API_KEY not configured")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key header")


@app.get("/api/health")
async def api_health():
    return {"status": "ok"}


@app.get("/health")
async def health(request: Request):
    assistant = getattr(request.app.state, "assistant", None)
    scheduler_running = (
        assistant.scheduler.running
        if assistant and hasattr(assistant, "scheduler")
        else False
    )
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return {
        "status": "ok",
        "timestamp": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(),
        "llm_provider": LLM_PROVIDER,
        "ollama_url": OLLAMA_URL if LLM_PROVIDER == "ollama" else None,
        "ollama_model": OLLAMA_MODEL if LLM_PROVIDER == "ollama" else None,
        "scheduler_running": scheduler_running,
    }


async def api_run_screener(_=Depends(require_api_key)):
    """Run live screener via MOEX API, save to DB, and return results."""
    client = MoexClient()
    try:
        index_candles = await client.index_candles("IMOEX", interval="1d", count=30)

        async def fetch(ticker: str):
            candles = await client.candles_recent(ticker, count=100)
            return candles

        results = await run_screener(fetch, tickers=WATCHLIST, top_n=33, index_candles=index_candles)
        results = [_to_native(r) for r in results]
        await db.save_screener(results)
        run_ts = await db.latest_screener_ts()
        return {"count": len(results), "results": results, "run_ts": run_ts}
    finally:
        await client.close()


@app.post("/api/run_screener")
async def api_run_screener_endpoint(_=Depends(require_api_key)):
    """POST wrapper protected by API key."""
    return await api_run_screener()


@app.get("/api/screener")
async def api_screener(limit: int = 50, refresh: bool = False):
    """Latest screener results. Pass ?refresh=1 to force live run.

    If cached data is older than 15 minutes we try to refresh it from MOEX.
    If the live run fails we fall back to the cached results so the UI never
    shows a blank screener because of a transient MOEX API error.
    """
    from datetime import datetime

    run_ts = await db.latest_screener_ts()
    needs_refresh = refresh
    if not needs_refresh and run_ts:
        try:
            ts_dt = datetime.fromisoformat(run_ts.replace(" ", "T"))
            age_minutes = (datetime.now() - ts_dt).total_seconds() / 60
            if age_minutes > 15:
                needs_refresh = True
        except Exception:
            needs_refresh = True
    else:
        needs_refresh = True

    if needs_refresh:
        try:
            return await api_run_screener()
        except Exception as exc:
            logger.warning(f"Live screener refresh failed: {exc}; returning cached data")

    results = await db.latest_screener(limit=limit)
    return {"count": len(results), "results": results, "run_ts": run_ts}


@app.get("/api/watchlist")
async def api_watchlist():
    """Current prices for all watchlist tickers (from daily candles to match screener)."""
    client = MoexClient()
    items = []
    for ticker in WATCHLIST:
        try:
            candles = await client.candles_recent(ticker, interval="1d", count=2)
            if candles:
                last = candles[-1]
                prev = candles[-2] if len(candles) > 1 else None
                close = last.get("close")
                change = None
                if prev and prev.get("close") and close:
                    change = ((close - prev.get("close")) / prev.get("close")) * 100
                items.append({
                    "ticker": ticker,
                    "price": close,
                    "change": round(change, 2) if change is not None else None,
                })
            else:
                items.append({"ticker": ticker, "price": None, "change": None})
        except Exception:
            items.append({"ticker": ticker, "price": None, "change": None})
    await client.close()
    return {"count": len(items), "items": items}


@app.get("/api/ticker/{ticker}")
async def api_ticker(ticker: str, news: str = ""):
    """Detailed analysis for a single ticker with higher-TF trend, fundamentals, and volume profile.

    Query param `news` — optional news text to include in LLM analysis.
    """
    client = MoexClient()
    # Fetch daily, weekly, and hourly candles, order book and macro snapshot in parallel
    candles_task = client.candles_recent(ticker.upper(), interval="1d", count=100)
    weekly_task = client.candles_recent(ticker.upper(), interval="1w", count=50)
    hourly_task = client.candles_recent(ticker.upper(), interval="1h", count=50)
    ob_task = client.order_book_summary(ticker.upper(), depth=10)
    macro_task = get_macro_snapshot()
    candles, weekly_candles, hourly_candles, order_book, macro_snapshot = await asyncio.gather(
        candles_task, weekly_task, hourly_task, ob_task, macro_task
    )
    await client.close()

    # Fetch fundamentals
    fund = await fetch_fundamentals(ticker.upper())

    if not candles:
        return {"error": "Нет данных"}

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
    score_data = _to_native(score_stock(df, higher_tf_trend=higher_tf_trend, div_yield=fund.div_yield))
    bounce_pred = _to_native(predict_bounce(df))
    last = df.iloc[-1]

    # Build chart data with indicators
    chart_rows = []
    for i in range(max(-30, -len(df)), 0):
        row = df.iloc[i]
        chart_rows.append({
            "date": str(df.index[i]),
            "close": float(row.get("close")) if not pd.isna(row.get("close")) else None,
            "rsi": float(row.get("rsi")) if not pd.isna(row.get("rsi")) else None,
            "sma_20": float(row.get("sma_20")) if not pd.isna(row.get("sma_20")) else None,
            "sma_50": float(row.get("sma_50")) if not pd.isna(row.get("sma_50")) else None,
        })

    score_val = score_data.get("score", 0) if isinstance(score_data, dict) else score_data
    price_val = float(last.get("close", 0)) if not pd.isna(last.get("close")) else None
    atr_val = float(last.get("atr", 0)) if not pd.isna(last.get("atr")) else None
    adx_val = float(last.get("adx")) if not pd.isna(last.get("adx")) else None
    di_plus_val = float(last.get("di_plus")) if not pd.isna(last.get("di_plus")) else None
    di_minus_val = float(last.get("di_minus")) if not pd.isna(last.get("di_minus")) else None

    # Fetch latest auto-sentiment from RSS background scan (if any)
    latest_sentiment = await db.get_latest_sentiment(ticker.upper(), max_age_hours=24)

    advice = recommend_direction(
        ticker=ticker.upper(),
        signals=signals,
        score=score_val,
        rsi=float(last.get("rsi", 50)) if not pd.isna(last.get("rsi")) else 50,
        macd_hist=float(last.get("macd_hist", 0)) if not pd.isna(last.get("macd_hist")) else 0,
        bb_pct=float(last.get("bb_pct", 0.5)) if not pd.isna(last.get("bb_pct")) else 0.5,
        sma_20=float(last.get("sma_20", 0)) if not pd.isna(last.get("sma_20")) else 0,
        sma_50=float(last.get("sma_50", 0)) if not pd.isna(last.get("sma_50")) else 0,
        close=price_val,
        vol_ratio=float(last.get("vol_ratio", 1)) if not pd.isna(last.get("vol_ratio")) else 1,
        macro_bullish=compute_macro_bullish(macro_snapshot),
        adx=adx_val,
        di_plus=di_plus_val,
        di_minus=di_minus_val,
        higher_tf_trend=higher_tf_trend,
        div_yield=fund.div_yield,
        sentiment=latest_sentiment.get("sentiment") if latest_sentiment else None,
        sentiment_confidence=latest_sentiment.get("confidence", 0) if latest_sentiment else 0,
    )

    # Propagate liquidity warning from score_stock details to the user-facing advice.
    liquidity_warnings = score_data.get("details", {}).get("warnings", [])
    if liquidity_warnings:
        advice.warnings = liquidity_warnings + advice.warnings

    # Detect low-liquidity state so the UI/API can style the recommendation distinctly.
    low_liquidity = any("Низкая ликвидность" in w for w in liquidity_warnings)

    # Queue a robot proposal whenever we have a directional signal.
    # Mode is chosen from the global trading configuration (paper / semi_auto / live).
    if advice.direction in ("long", "short"):
        # Avoid creating another pending proposal if one already exists for this
        # ticker. Prevents duplicate cards when the user refreshes the analysis
        # page or when the screener re-evaluates the same ticker repeatedly.
        skip_proposal = False
        try:
            pending_same_ticker = await db.get_robot_proposals(
                status="pending", ticker=ticker.upper(), limit=1, since_days=1
            )
            if pending_same_ticker:
                advice.warnings.append(
                    f"Предложение по {ticker.upper()} уже есть в очереди робота; "
                    f"новое не создано, чтобы избежать дублей."
                )
                skip_proposal = True
        except Exception:
            skip_proposal = False

        if not skip_proposal:
            proposal_mode = _resolve_proposal_mode()
            if proposal_mode == "semi_auto" and should_auto_trade(50):
                proposal_mode = "auto_trade"
            levels = compute_levels(advice.direction, price_val, atr_val) if price_val else None
            stop_px = levels.get("stop") if levels else None
            take_px = levels.get("take") if levels else None

            # Fee check before queuing: skip if expected profit after commission is too small.
            if take_px and price_val:
                from strategies.risk import calculate_position
                # For paper proposals use virtual capital; for real/semi_auto proposals use
                # the broker portfolio value so position sizing matches actual buying power.
                if proposal_mode == "paper":
                    equity = (await db.get_paper_stats()).get("current_capital", PAPER_STARTING_CAPITAL)
                else:
                    try:
                        from brokers.tinkoff_client import TinkoffClient
                        _tinkoff = TinkoffClient()
                        _portfolio = await _tinkoff.get_portfolio()
                        equity = _portfolio.total_value_rub or PAPER_STARTING_CAPITAL
                        await _tinkoff.close()
                    except Exception:
                        equity = PAPER_STARTING_CAPITAL
                plan = calculate_position(ticker.upper(), advice.direction, price_val, atr_val or 0, equity)

                # Cap notional exposure at MAX_POSITION_SIZE_PCT and enforce minimum.
                max_value = equity * (MAX_POSITION_SIZE_PCT / 100)
                if price_val > 0 and plan.qty * price_val > max_value:
                    plan.qty = max(int(max_value / price_val), 1)
                min_value = equity * (MIN_POSITION_SIZE_PCT / 100)
                if price_val > 0 and plan.qty * price_val < min_value:
                    plan.qty = max(int(min_value / price_val), 1)

                fee_est = estimate_trade_costs(price_val, take_px, max(plan.qty, 1), side=advice.direction, hold_days=1)
                if not fee_est.worth_it:
                    advice.warnings.append(
                        f"Пропуск сигнала: комиссия и плата за перенос съедают прибыль (чистая {fee_est.net_profit_pct}% "
                        f"при комиссии {fee_est.total_commission_rub} RUB и переносе {fee_est.carry_fee_rub} RUB)"
                    )
                    take_px = None

            if take_px:
                open_count = len(await db.get_open_paper_positions())
                if open_count < MAX_OPEN_POSITIONS:
                    await db.save_robot_proposal(
                        ticker=ticker.upper(),
                        side=advice.direction,
                        source="api_ticker",
                        signal=advice.direction,
                        entry_px=price_val,
                        qty=max(plan.qty, 1),
                        stop_px=stop_px,
                        take_px=take_px,
                        confidence=50,
                        reason=",".join(advice.signals_used or []),
                        fee_rub=fee_est.total_commission_rub,
                        net_profit_pct=fee_est.net_profit_pct,
                        horizon="1d",
                        proposal_mode=proposal_mode,
                    )
                    if proposal_mode == "paper":
                        advice.warnings.append(
                            f"Сделка {ticker.upper()} {advice.direction.upper()} поставлена в очередь "
                            f"на исполнение по открытию следующего дня"
                        )
                    elif proposal_mode == "semi_auto":
                        advice.warnings.append(
                            f"Сделка {ticker.upper()} {advice.direction.upper()} добавлена в Предложения робота. "
                            f"Подтвердите вручную для отправки заявки брокеру"
                        )
                    else:
                        advice.warnings.append(
                            f"Сделка {ticker.upper()} {advice.direction.upper()} поставлена в очередь "
                            f"на автоматическое исполнение"
                        )

    # Auto-include RSS sentiment headline if no manual news provided
    news_for_llm = news
    if not news_for_llm and latest_sentiment:
        news_for_llm = latest_sentiment.get("headline", "") or latest_sentiment.get("summary", "")

    llm_text = None
    gemma_critique_text = None
    if LLM_PROVIDER != "none":
        # Cache key includes news flag so manual-news requests bypass the plain cache
        cache_key = ticker.upper() + (":news" if news_for_llm else "")
        cached = _LLM_CACHE.get(cache_key)
        if cached and (time.time() - cached[0]) < _LLM_CACHE_TTL:
            llm_text = cached[1]
            gemma_critique_text = cached[2]
        else:
            try:
                price_data = {
                    "close": float(last.get("close")) if not pd.isna(last.get("close")) else None,
                    "rsi": float(last.get("rsi", 50)) if not pd.isna(last.get("rsi")) else None,
                    "macd_hist": float(last.get("macd_hist", 0)) if not pd.isna(last.get("macd_hist")) else None,
                    "atr": float(last.get("atr", 0)) if not pd.isna(last.get("atr")) else None,
                    "vol_ratio": float(last.get("vol_ratio", 1)) if not pd.isna(last.get("vol_ratio")) else None,
                    "bb_pct": float(last.get("bb_pct", 0.5)) if not pd.isna(last.get("bb_pct")) else 0.5,
                    "sma_20": float(last.get("sma_20", 0)) if not pd.isna(last.get("sma_20")) else 0,
                    "sma_50": float(last.get("sma_50", 0)) if not pd.isna(last.get("sma_50")) else 0,
                    "adx": float(last.get("adx")) if not pd.isna(last.get("adx")) else None,
                    "di_plus": float(last.get("di_plus")) if not pd.isna(last.get("di_plus")) else None,
                    "di_minus": float(last.get("di_minus")) if not pd.isna(last.get("di_minus")) else None,
                    "higher_tf_trend": higher_tf_trend,
                    "div_yield": fund.div_yield,
                    "score": score_data.get("score", 0) if isinstance(score_data, dict) else score_data,
                    "direction": advice.direction,
                    "strength": advice.strength,
                    "recommendation": format_direction_emoji(advice.direction, advice.strength, low_liquidity=low_liquidity),
                    "reason": advice.reason,
                    "warnings": advice.warnings,
                    "order_book": order_book,
                }
                analysis_result = await analyze_ticker(
                    ticker.upper(), price_data, signals, news=news_for_llm, macro=macro_snapshot,
                    bounce_prediction=bounce_pred, latest_sentiment=latest_sentiment,
                )
                # analyze_ticker now returns (primary_analysis, gemma_critique)
                if isinstance(analysis_result, tuple) and len(analysis_result) == 2:
                    llm_text, gemma_critique_text = analysis_result
                else:
                    llm_text = analysis_result
                    gemma_critique_text = None

                if news_for_llm and llm_text:
                    llm_text = f"📰 Новость учтена: {news_for_llm[:60]}{'...' if len(news_for_llm) > 60 else ''}\n\n{llm_text}"
                _LLM_CACHE[cache_key] = (time.time(), llm_text, gemma_critique_text)
            except Exception as e:
                from loguru import logger
                logger.warning(f"LLM analysis failed for {ticker}: {e}")
                gemma_critique_text = None

    # Save the prediction after LLM analysis so we can record the actual provider used.
    # API analysis is paper-only; sandbox predictions come from the evening pipeline.
    await db.save_prediction(
        ticker=ticker.upper(),
        predicted_direction=advice.direction,
        predicted_price=price_val or 0,
        predicted_strength=advice.strength,
        higher_tf_trend=higher_tf_trend,
        signals_used=advice.signals_used,
        reasoning=advice.reason,
        source="analysis",
        llm_provider=get_last_used_provider() or LLM_PROVIDER,
        environment="paper",
    )

    levels = compute_levels(advice.direction, price_val, atr_val) if price_val else None

    # Volume profile from hourly candles
    vol_profile = None
    if hourly_candles:
        try:
            hdf = df_from_candles(hourly_candles)
            if len(hdf) >= 5:
                vol_profile = compute_volume_profile(hdf, bins=8)
        except Exception:
            pass

    return {
        "ticker": ticker.upper(),
        "score": score_val,
        "signals": signals,
        "price": price_val,
        "rsi": float(last.get("rsi", 50)) if not pd.isna(last.get("rsi")) else None,
        "macd_hist": float(last.get("macd_hist", 0)) if not pd.isna(last.get("macd_hist")) else None,
        "atr": atr_val,
        "adx": adx_val,
        "di_plus": di_plus_val,
        "di_minus": di_minus_val,
        "higher_tf_trend": higher_tf_trend,
        "div_yield": fund.div_yield,
        "market_cap": fund.market_cap,
        "last_dividend": fund.last_dividend,
        "candles": candles[-30:],
        "chart_data": chart_rows,
        "levels": levels,
        "volume_profile": vol_profile,
        "order_book": order_book,
        "llm_analysis": llm_text,
        "gemma_critique": gemma_critique_text,
        "macro": macro_snapshot,
        "direction": advice.direction,
        "strength": advice.strength,
        "reason": advice.reason,
        "risk_reward": advice.risk_reward,
        "stop_pct": advice.stop_pct,
        "warnings": advice.warnings,
        "recommendation": format_direction_emoji(advice.direction, advice.strength, low_liquidity=low_liquidity),
        "low_liquidity": low_liquidity,
        "liquidity_status": "низкая ликвидность" if low_liquidity else "ликвидность в норме",
        "latest_sentiment": latest_sentiment,
        "bounce_prediction": bounce_pred,
    }


@app.get("/api/intraday/{ticker}")
async def api_intraday(ticker: str, llm: bool = True):
    """Краткосрочный внутридневной анализ по 5-минутным свечам."""
    client = MoexClient()
    t = ticker.upper()
    try:
        candles_1m_task = client.candles_recent(t, interval="1m", count=500)
        daily_task = client.candles_recent(t, interval="1d", count=1)
        ob_task = client.order_book_summary(t, depth=10)
        candles_1m, daily_candles, order_book = await asyncio.gather(
            candles_1m_task, daily_task, ob_task
        )
    finally:
        await client.close()

    candles_5m = resample_1m_to_5m(candles_1m or [])

    daily_ohlc = None
    if daily_candles and len(daily_candles) > 0:
        d = daily_candles[-1]
        daily_ohlc = {
            "open": d.get("open"),
            "high": d.get("high"),
            "low": d.get("low"),
            "close": d.get("close"),
        }

    result = await intraday_agent.analyze(
        ticker=t,
        candles_5m=candles_5m,
        order_book=order_book,
        daily_ohlc=daily_ohlc,
        use_llm=llm,
    )
    data = intraday_agent.to_dict(result)
    data["candles_5m"] = candles_5m[-120:]
    return data


@app.get("/api/positions")
async def api_positions():
    """Open trades."""
    positions = await db.open_positions()
    return {"count": len(positions), "positions": positions}


from pydantic import BaseModel


class AddPositionRequest(BaseModel):
    ticker: str
    side: str
    entry_px: float
    stop_px: float | None = None
    target_px: float | None = None
    qty: int = 1
    reason: str | None = None


async def api_add_position(req: AddPositionRequest):
    """Add a new trade to journal."""
    trade_id = await db.add_trade(
        ticker=req.ticker.upper(),
        side=req.side,
        entry_px=req.entry_px,
        stop_px=req.stop_px,
        target_px=req.target_px,
        qty=req.qty,
        reason=req.reason,
    )
    return {"status": "ok", "trade_id": trade_id}


app.post("/api/positions/add")(api_add_position)


class SentimentRequest(BaseModel):
    news: list[str]


class AutoTradingSetting(BaseModel):
    auto_trading_enabled: bool | None = None
    auto_trade: bool | None = None


async def api_sentiment(ticker: str, req: SentimentRequest):
    """Анализ сентимента новостей для тикера (NewsSentimentAgent)."""
    result = await sentiment_agent.analyze(ticker.upper(), req.news)
    return sentiment_agent.to_dict(result)


app.post("/api/ticker/{ticker}/sentiment")(api_sentiment)


@app.get("/api/indices")
async def api_indices():
    """IMOEX and RTSI current values."""
    client = MoexClient()
    indices = {}
    for idx in ("IMOEX", "RTSI"):
        val = await client.index_value(idx)
        indices[idx] = val.get("value") if isinstance(val, dict) and val else None
    await client.close()
    return indices


@app.get("/api/ticker/{ticker}/sentiment_status")
async def api_sentiment_status(ticker: str):
    """Latest cached sentiment for a ticker (from RSS auto-scan or manual)."""
    result = await db.get_latest_sentiment(ticker.upper(), max_age_hours=24)
    if result:
        return {"ticker": ticker.upper(), **result}
    return {"ticker": ticker.upper(), "sentiment": "unknown", "confidence": 0, "summary": "Нет данных"}


@app.get("/api/watchlist/sentiment")
async def api_watchlist_sentiment():
    """Sentiment indicators for all watchlist tickers."""
    results = await db.get_watchlist_sentiment(WATCHLIST, max_age_hours=24)
    return {"count": len(results), "items": results}


@app.get("/api/alerts")
async def api_alerts():
    """Tickers with strong signals for browser push notifications."""
    alerts = []

    # Screener-based alerts
    screener_results = await db.latest_screener(limit=33)
    for r in screener_results:
        score = r.get("score", 50)
        if isinstance(score, dict):
            score = score.get("score", 50)
        ticker = r.get("ticker", "?")
        if score >= 70:
            alerts.append({
                "ticker": ticker,
                "type": "screener_bullish",
                "message": f"{ticker}: сильный бычий сигнал (очков {score:.1f})",
                "priority": "high" if score >= 80 else "normal",
            })
        elif score <= 30:
            alerts.append({
                "ticker": ticker,
                "type": "screener_bearish",
                "message": f"{ticker}: сильный медвежий сигнал (очков {score:.1f})",
                "priority": "high" if score <= 20 else "normal",
            })

    # Sentiment-based alerts (skip if same ticker already alerted by screener)
    tickers_alerted = {a["ticker"] for a in alerts}
    sentiments = await db.get_watchlist_sentiment(WATCHLIST, max_age_hours=24)
    for s in sentiments.values():
        conf = s.get("confidence", 0)
        sent = s.get("sentiment", "")
        ticker = s.get("ticker", "?")
        if conf >= 80 and sent in ("bullish", "bearish") and ticker not in tickers_alerted:
            alerts.append({
                "ticker": ticker,
                "type": f"sentiment_{sent}",
                "message": f"{ticker}: сильный {sent} сентимент ({conf}%) — {s.get('summary', '')[:60]}",
                "priority": "high" if conf >= 90 else "normal",
            })

    return {"count": len(alerts), "alerts": alerts}


@app.get("/api/predictions")
async def api_predictions(tickers: str = "", days: int = 30, limit: int = 50, source: str = "", environment: str = ""):
    """List recent predictions with optional ticker, source and environment filter."""
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()] or None
    rows = await db.get_predictions(
        tickers=ticker_list,
        since_days=days,
        limit=limit,
        source=source.strip() or None,
        environment=environment.strip() or None,
    )
    return {"count": len(rows), "predictions": rows}


@app.get("/api/predictions/stats")
async def api_prediction_stats(tickers: str = "", days: int = 30, source: str = "", environment: str = ""):
    """Aggregated accuracy stats for predictions.

    Query params:
    - tickers: comma-separated ticker list (optional)
    - days: lookback window (default 30)
    - source: filter predictions by source, e.g. 'analysis', 'trading_agent' (optional)
    - environment: 'paper', 'sandbox' or 'live' (optional)
    """
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    src = source.strip() or None
    env = environment.strip() or None
    if ticker_list:
        # Per-ticker stats
        by_ticker: dict[str, dict] = {}
        for t in ticker_list:
            by_ticker[t] = await db.get_prediction_stats(
                ticker=t, since_days=days, group_by_provider=True, source=src, environment=env
            )
        total = sum(s["total"] for s in by_ticker.values())
        overall = (
            await db.get_prediction_stats(ticker=None, since_days=days, group_by_provider=True, source=src, environment=env)
            if len(ticker_list) > 1
            else list(by_ticker.values())[0]
        )
    else:
        overall = await db.get_prediction_stats(
            ticker=None, since_days=days, group_by_provider=True, source=src, environment=env
        )
        by_ticker = {}
        total = overall["total"]
    return {
        "total": total,
        "days": days,
        "correct_1d_pct": overall.get("correct_1d_pct", 0),
        "correct_3d_pct": overall.get("correct_3d_pct", 0),
        "correct_7d_pct": overall.get("correct_7d_pct", 0),
        "by_ticker": by_ticker,
        "by_provider": overall.get("by_provider", {}),
    }


@app.delete("/api/predictions")
async def api_reset_predictions(environment: str = Query(..., description="Environment segment to clear: paper, sandbox, or live")):
    """Clear predictions for a single environment segment.

    The environment parameter is required so a UI click cannot accidentally
    wipe predictions for every mode at once.
    """
    env = environment.strip().lower()
    if env not in ("paper", "sandbox", "live"):
        raise HTTPException(status_code=400, detail="environment must be paper, sandbox, or live")
    count = await db.clear_predictions_environment(env)
    logger.info(f"Predictions for {env} cleared by API: {count} rows")
    return {"ok": True, "message": f"Predictions for {env} cleared", "count": count}


@app.get("/api/paper/positions")
async def api_paper_positions(status: str = "", limit: int = 50):
    """List paper trading positions."""
    st = status if status in ("open", "closed") else None
    rows = await db.get_paper_positions(status=st, limit=limit)
    return {"count": len(rows), "positions": rows}


@app.post("/api/paper/positions/{position_id}/close")
async def api_close_paper_position(position_id: int):
    """Close an open paper position at the current market price."""
    pos = await db.get_paper_position_by_id(position_id)
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")
    if pos.get("status") != "open":
        raise HTTPException(status_code=400, detail="Position is already closed")
    client = MoexClient()
    try:
        candles = await client.candles_recent(pos["ticker"], count=1)
        if not candles:
            raise HTTPException(status_code=500, detail="Could not fetch market price")
        exit_px = float(candles[-1]["close"])
    finally:
        await client.close()
    await db.close_paper_position(position_id, exit_px, reason="manual")
    closed = await db.get_paper_position_by_id(position_id)
    return {
        "ok": True,
        "position_id": position_id,
        "ticker": pos["ticker"],
        "exit_px": exit_px,
        "pnl_pct": closed.get("pnl_pct") if closed else None,
        "pnl_rub": closed.get("pnl_rub") if closed else None,
    }


@app.get("/api/paper/stats")
async def api_paper_stats(days: int = 30):
    """Virtual portfolio statistics."""
    stats = await db.get_paper_stats(since_days=days)
    return stats


@app.get("/api/georisk")
async def api_georisk():
    """Latest geopolitical risk score with sector impacts and overall direction."""
    geo = await db.get_latest_georisk()
    if not geo:
        return {
            "score": 0,
            "severity": "low",
            "summary": "Нет данных",
            "overall_direction": 0,
            "affected_sectors": [],
            "trigger_keywords": [],
        }
    return geo


def _html_response(path: Path) -> FileResponse:
    """Return an HTML file with cache-busting headers."""
    resp = FileResponse(path)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/")
async def serve_root():
    """Serve the desktop web interface as the only UI."""
    return _html_response(MOBILE_DIR / "desktop.html")


@app.get("/desktop")
async def serve_desktop():
    """Legacy path kept for backward compatibility."""
    return _html_response(MOBILE_DIR / "desktop.html")


@app.get("/api/proposals")
async def api_proposals(status: str = "", limit: int = 50, since_days: int = 7):
    """List robot trade proposals. Defaults to recent proposals of all statuses."""
    st = status if status in ("pending", "confirmed", "rejected", "executed", "superseded") else None
    rows = await db.get_robot_proposals(status=st, limit=limit, since_days=since_days)
    return {"count": len(rows), "proposals": rows}


@app.get("/api/proposals/pending")
async def api_pending_proposals(limit: int = 50):
    """Return pending proposals that need a user decision."""
    rows = await db.get_unique_pending_robot_proposals(limit=limit)
    return {"count": len(rows), "proposals": rows}


class ProposalDecision(BaseModel):
    decided_by: str = "user"
    reject_reason: str | None = None
    proposal_mode: str | None = None


async def _execute_paper_proposal_now(proposal: dict) -> dict:
    """Immediately open/flip a paper position from a confirmed proposal.

    Returns {"ok": bool, "position_id": int|None, "message": str}.
    Mirrors the sizing and reversal logic in execution.paper_execution.
    """
    ticker = proposal["ticker"]
    side = proposal.get("side", "long")
    entry_px = float(proposal.get("entry_px") or 0)
    qty = int(proposal.get("qty") or 1)
    if entry_px <= 0:
        return {"ok": False, "position_id": None, "message": f"Нет цены входа для {ticker}"}

    open_positions = await db.get_open_paper_positions()
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return {"ok": False, "position_id": None, "message": f"Лимит открытых позиций ({MAX_OPEN_POSITIONS}) достигнут"}

    existing = await db.get_open_paper_position(ticker)
    if existing:
        if existing["side"] == side:
            return {"ok": False, "position_id": None, "message": f"Позиция {ticker} {side} уже открыта"}
        await db.close_paper_position(existing["id"], entry_px, f"reverse_to_{side}")

    # Derive ATR from proposal stop if available, otherwise use a 3% fallback.
    stop_px = proposal.get("stop_px")
    if stop_px:
        initial_atr = abs(entry_px - float(stop_px)) / STOP_LOSS_ATR_MULT
    else:
        initial_atr = entry_px * 0.03

    position_id = await db.open_paper_position(
        ticker=ticker,
        side=side,
        entry_px=entry_px,
        signals_used=(proposal.get("reason") or "").split(",") + ["confirmed"],
        stop_px=proposal.get("stop_px"),
        take_px=proposal.get("take_px"),
        qty=qty,
        initial_atr=initial_atr,
        atr_mult=TRAILING_STOP_ATR_MULT,
    )
    return {"ok": True, "position_id": position_id, "message": f"Открыта бумажная позиция {ticker} {side}"}


async def api_confirm_proposal(proposal_id: int, req: ProposalDecision):
    """Confirm a robot proposal (user wants to execute it).

    The user can override the execution mode: paper, semi_auto or live.
    Sandbox mode never allows live orders, so live is silently downgraded
    to semi_auto when TINKOFF_SANDBOX is enabled.

    In paper/sandbox mode the position is opened immediately on the virtual
    account; in live/semi-auto mode the proposal is confirmed and the broker
    order executor will place the market order on its next tick.

    A prediction diary row is also written so confirmed ideas show up in the
    paper/sandbox/live diary with the correct environment.
    """
    mode = req.proposal_mode
    if mode == "live" and TINKOFF_SANDBOX:
        mode = "semi_auto"

    proposal = await db.get_robot_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    # Auto-trade: manual confirm в авторежиме лишь ставит статус 'confirmed'
    # для дневника. executor уже подхватит pending с confidence >= порога.
    if app_config.AUTO_TRADE and proposal.get("status") == "pending":
        logger.info(
            f"Manual confirm of proposal {proposal_id} in AUTO_TRADE mode — "
            f"executor will pick it up on next tick if confidence >= "
            f"{app_config.AUTO_TRADE_MIN_CONFIDENCE}"
        )

    # Paper: open virtual position right away. Semi-auto / live: leave the
    # proposal as 'confirmed' so the broker_order_executor places the real
    # order on Tinkoff (sandbox or live) on its next tick.
    should_open_paper_now = mode == "paper" or (
        mode == "semi_auto" and PAPER_TRADING
    )

    if should_open_paper_now:
        result = await _execute_paper_proposal_now(proposal)
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result["message"])
        await db.mark_proposal_executed(proposal_id, decided_by=req.decided_by)
        status = "executed"
    else:
        ok = await db.confirm_robot_proposal(proposal_id, decided_by=req.decided_by, proposal_mode=mode)
        if not ok:
            raise HTTPException(status_code=404, detail="Proposal not found")
        status = "confirmed"

    # Write a diary prediction row for the confirmed proposal so it appears
    # in the diary/statistics for the selected environment.
    try:
        env = "paper"
        if mode != "paper":
            env = "sandbox" if TINKOFF_SANDBOX else "live"
        side = proposal.get("side", "long")
        direction = side if side in ("long", "short") else "long"
        confidence = proposal.get("confidence") or 50
        strength = "strong" if confidence >= 70 else "moderate" if confidence >= 50 else "weak"
        await db.save_prediction(
            ticker=proposal["ticker"],
            predicted_direction=direction,
            predicted_price=float(proposal.get("entry_px") or 0),
            predicted_strength=strength,
            higher_tf_trend="unknown",
            signals_used=[],
            reasoning=proposal.get("reason", "") or f"Подтверждённое предложение робота ({proposal.get('source', '?')})",
            source=proposal.get("source", "robot_proposal"),
            llm_provider=get_last_used_provider() or LLM_PROVIDER,
            environment=env,
        )
    except Exception as exc:
        logger.warning(f"Could not write diary prediction for confirmed proposal {proposal_id}: {exc}")

    return {"ok": True, "proposal_id": proposal_id, "status": status, "proposal_mode": mode}


async def api_reject_proposal(proposal_id: int, req: ProposalDecision):
    """Reject a robot proposal."""
    ok = await db.reject_robot_proposal(
        proposal_id,
        decided_by=req.decided_by,
        reject_reason=req.reject_reason,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return {"ok": True, "proposal_id": proposal_id, "status": "rejected"}


async def api_execute_proposal(proposal_id: int, req: ProposalDecision):
    """Mark a confirmed proposal as executed (broker order placed)."""
    ok = await db.mark_proposal_executed(proposal_id, decided_by=req.decided_by)
    if not ok:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return {"ok": True, "proposal_id": proposal_id, "status": "executed"}


async def api_cleanup_old_proposals(clear_all_pending: bool = Query(False, description="Remove all pending proposals regardless of age")):
    """Remove stale api_ticker proposals and pending proposals."""
    deleted = await db.delete_stale_robot_proposals(clear_all_pending=clear_all_pending)
    return {"ok": True, "deleted": deleted}


@app.get("/api/proposals/source_stats")
async def api_proposal_source_stats(since_days: int = 30):
    """Return robot proposal stats grouped by source."""
    rows = await db.get_proposal_source_stats(since_days=since_days)
    return {"count": len(rows), "sources": rows}


app.post("/api/proposals/{proposal_id}/confirm")(api_confirm_proposal)
app.post("/api/proposals/{proposal_id}/reject")(api_reject_proposal)
app.post("/api/proposals/{proposal_id}/execute")(api_execute_proposal)
app.post("/api/proposals/cleanup")(api_cleanup_old_proposals)


@app.get("/api/broker_orders")
async def api_broker_orders(status: str = "", limit: int = 50, environment: str = ""):
    """Return real broker orders submitted to Tinkoff."""
    st = status if status in ("pending", "filled", "partial", "rejected", "cancelled") else None
    rows = await db.get_broker_orders(status=st, limit=limit)
    if environment:
        rows = [r for r in rows if r.get("environment") == environment]
    counts = await db.count_broker_orders_by_status(environment=environment or None)
    return {"count": len(rows), "orders": rows, "counts": counts}


@app.get("/api/sandbox_orders")
async def api_sandbox_orders(status: str = "", limit: int = 50):
    """Return sandbox orders only (convenience alias)."""
    if not app_config.TINKOFF_SANDBOX:
        return {
            "count": 0,
            "orders": [],
            "counts": {"pending": 0, "filled": 0, "partial": 0, "rejected": 0, "cancelled": 0},
            "note": "Not running in sandbox mode",
        }
    return await api_broker_orders(status=status, limit=limit, environment="sandbox")


@app.get("/api/sandbox/summary")
async def api_sandbox_summary(limit: int = 500):
    """Return realised-P&L summary of sandbox trades plus per-ticker stats.

    Useful for the "Сводка" panel on the Песочница tab — the user can see
    total P&L, win-rate, and best/worst ticker at a glance instead of
    having to scan 50 raw order rows.
    """
    if not app_config.TINKOFF_SANDBOX:
        return {"environment": "sandbox", "closed_count": 0, "open_count": 0,
                "total_pnl_rub": 0.0, "win_rate_pct": 0.0,
                "by_ticker": [], "open_positions": [], "trades": []}
    return await db.summarize_broker_trades(environment="sandbox", limit=limit)


@app.get("/api/sandbox/trades")
async def api_sandbox_trades(limit: int = 200):
    """Return trade-level rows for the Sandbox panel — one row per trade.

    Closed trades come from the `journal` table (with realized entry/exit/P&L).
    Open positions come from `broker_positions` where status='open' and are
    rendered with null exit fields. This is the source of truth for the
    "Сделки в песочнице" table — each row is a single idea, not a single order.
    """
    if not app_config.TINKOFF_SANDBOX:
        return {"count": 0, "trades": [], "note": "Not running in sandbox mode"}
    db = await core_db_module.get_db() if False else None  # placeholder, replaced below
    from core import db as core_db
    db = await core_db.get_db()
    # Closed: journal rows
    cur = await db.execute(
        """SELECT j.id, COALESCE(j.exit_ts, j.ts) AS ts, j.ticker, j.side,
                  j.entry_px, j.exit_px, j.qty, j.pnl, j.stop_px, j.target_px, j.reason
           FROM journal j
           ORDER BY COALESCE(j.exit_ts, j.ts) DESC
           LIMIT ?""",
        (limit,),
    )
    closed = await cur.fetchall()
    # Open: broker_positions
    cur = await db.execute(
        """SELECT id, ts, ticker, side, qty, avg_entry_px, stop_px, take_px
           FROM broker_positions
           WHERE status='open' AND broker='tinkoff'
           ORDER BY ts DESC
           LIMIT ?""",
        (limit,),
    )
    open_pos = await cur.fetchall()
    rows: list[dict] = []
    for r in closed:
        entry = float(r[4]) if r[4] is not None else None
        exit_px = float(r[5]) if r[5] is not None else None
        qty = int(r[6]) if r[6] is not None else 0
        pnl = float(r[7]) if r[7] is not None else None
        pnl_pct = None
        if pnl is not None and entry and qty:
            notional = abs(entry * qty)
            pnl_pct = round(pnl / notional * 100, 2) if notional else None
        rows.append({
            "kind": "closed",
            "id": int(r[0]),
            "ts": r[1],
            "ticker": r[2],
            "side": r[3] or "long",
            "entry_px": entry,
            "exit_px": exit_px,
            "qty": qty,
            "pnl_rub": pnl,
            "pnl_pct": pnl_pct,
            "stop_px": float(r[8]) if r[8] is not None else None,
            "take_px": float(r[9]) if r[9] is not None else None,
            "reason": r[10] or "",
        })
    for r in open_pos:
        # columns: id, ts, ticker, side, qty, avg_entry_px, stop_px, take_px
        entry = float(r[5]) if r[5] is not None else None
        qty = int(r[4]) if r[4] is not None else 0
        rows.append({
            "kind": "open",
            "id": int(r[0]),
            "ts": r[1],
            "ticker": r[2],
            "side": r[3] or "long",
            "entry_px": entry,
            "exit_px": None,
            "qty": qty,
            "pnl_rub": None,
            "pnl_pct": None,
            "stop_px": float(r[6]) if r[6] is not None else None,
            "take_px": float(r[7]) if r[7] is not None else None,
            "reason": "Открыта",
        })
    rows.sort(key=lambda x: x["ts"] or "", reverse=True)
    # Normalize side to long/short for display
    for r in rows:
        if r["side"] in ("buy",):
            r["side"] = "long"
        elif r["side"] in ("sell",):
            r["side"] = "short"
    return {"count": len(rows), "trades": rows}


@app.get("/api/sandbox/orders.csv")
async def api_sandbox_orders_csv(limit: int = 500):
    """Return sandbox orders as a downloadable CSV file."""
    from fastapi.responses import PlainTextResponse
    rows = await db.get_broker_orders(status=None, limit=limit)
    rows = [r for r in rows if r.get("environment") == "sandbox"]
    header = ["ts", "ticker", "side", "qty", "lots", "entry_px", "status",
              "proposal_id", "proposal_source", "proposal_confidence",
              "proposal_proposal_mode", "order_id"]
    lines = [";".join(header)]
    for r in rows:
        lines.append(";".join(str(r.get(h, "") or "") for h in header))
    body = "\n".join(lines) + "\n"
    return PlainTextResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=sandbox_orders.csv"},
    )


@app.get("/api/sandbox/portfolio")
async def api_sandbox_portfolio():
    """Return live sandbox portfolio from Tinkoff: cash, positions, total value.

    Positions are enriched with side, stop and take from the matching executed
    robot proposal so the UI can show a complete trading picture.
    """
    if not app_config.TINKOFF_SANDBOX:
        return {"error": "Not running in sandbox mode"}
    if not app_config.TINKOFF_TOKEN:
        return {"error": "Tinkoff token not configured"}
    try:
        from brokers.tinkoff_client import TinkoffClient
        client = TinkoffClient(sandbox=True)
        portfolio = await client.get_portfolio()
        await client.close()

        # Enrich positions with side/stop/take from the local broker_positions table.
        # We only trust positions the robot actually tracks - executed proposals are
        # historical and can outlive the position (e.g. OZON: closed short, opened
        # long manually, UI was still showing the old short's stop/take).
        tracked_lookup: dict[tuple, dict] = {}
        tracked_positions = await db.get_open_broker_positions(
            broker="tinkoff", account_id=portfolio.account_id
        )
        for tp in tracked_positions:
            tracked_lookup[(tp["ticker"], tp["side"])] = tp

        positions = []
        for p in portfolio.positions:
            if p.ticker == "RUB000UTSTOM":
                continue
            # Tinkoff returns signed quantities: positive for long, negative for short.
            # Use the broker sign as the source of truth for the side.
            signed_qty = int(p.quantity)
            side = "short" if signed_qty < 0 else "long"
            tracked = tracked_lookup.get((p.ticker, side), {})
            positions.append(
                {
                    "ticker": p.ticker,
                    "figi": p.figi,
                    "instrument_uid": p.instrument_uid,
                    "quantity": signed_qty,
                    # Prefer our avg_entry_px from broker_positions when
                    # available: Tinkoff sandbox can return a stale or
                    # nonsensical average_price (e.g. daily close) for
                    # freshly opened positions, which confuses the UI.
                    "average_price": tracked.get("avg_entry_px") or p.average_price,
                    "current_price": p.current_price,
                    "currency": p.currency,
                    "side": side,
                    "stop_px": tracked.get("stop_px"),
                    "take_px": tracked.get("take_px"),
                    "proposal_id": tracked.get("id"),
                }
            )

        # Cross-check local tracking vs broker reality. The broker is the
        # source of truth, so any local open rows whose (ticker, side) isn't
        # on the broker are flagged as phantom and can be reconciled. We also
        # include recently closed rows so the UI can show broker-side closes
        # that we reconciled automatically.
        #
        # Closed rows with qty=0 (the historical journal of every trade we ever
        # took) are not phantoms — they're the audit trail. Only closed rows
        # that still hold a non-zero qty are anomalies worth flagging here.
        #
        # Split tracked rows into two buckets:
        # - db_discrepancy: real phantoms — ticker does not appear on the
        #   broker at all (any side). Triggers the "Очистить фиктивные"
        #   banner. A reverse (e.g. closed OZON SHORT while broker holds
        #   OZON LONG) is NOT a phantom — the broker knows about OZON.
        # - db_legacy: rows that DO correspond to a live broker position
        #   (same ticker, any side). Audit-trail cruft from legacy
        #   migrations, NOT phantoms — the banner must NOT fire on these.
        real_keys: set[tuple[str, str]] = {(p["ticker"].upper(), p["side"]) for p in positions}
        real_tickers: set[str] = {p["ticker"].upper() for p in positions}
        tracked_all = await db.get_open_broker_positions(
            broker="tinkoff",
            account_id=portfolio.account_id,
            include_closed=True,
        )
        discrepancy: list[dict] = []
        legacy: list[dict] = []
        for tp in tracked_all:
            key = (tp["ticker"].upper(), (tp["side"] or "long").lower())
            ticker = tp["ticker"].upper()
            status = tp.get("status") or "open"
            qty = tp.get("qty") or 0
            if status == "open" and key in real_keys:
                continue
            entry = {
                "id": tp["id"],
                "ticker": tp["ticker"],
                "side": tp["side"],
                "qty": qty,
                "ts": tp["ts"],
                "status": status,
                "avg_entry_px": tp.get("avg_entry_px"),
                "stop_px": tp.get("stop_px"),
                "take_px": tp.get("take_px"),
                "close_reason": tp.get("close_reason"),
                "exit_px": tp.get("exit_px"),
            }
            if status == "closed" and qty == 0:
                continue
            if ticker in real_tickers:
                legacy.append(entry)
            else:
                discrepancy.append(entry)

        return {
            "account_id": portfolio.account_id,
            "total_value_rub": portfolio.total_value_rub,
            "cash_rub": portfolio.cash_rub,
            "positions": positions,
            "db_discrepancy": discrepancy,
            "db_legacy": legacy,
        }
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/api/sandbox/reconcile")
async def api_sandbox_reconcile(req: Request):
    """Close any open broker_positions rows that aren't present in the live
    Tinkoff sandbox portfolio. Broker is the source of truth."""
    if not app_config.TINKOFF_SANDBOX:
        raise HTTPException(status_code=400, detail="Not running in sandbox mode")
    try:
        from brokers.tinkoff_client import TinkoffClient
        client = TinkoffClient(sandbox=True)
        portfolio = await client.get_portfolio()
        await client.close()
        real_keys: set[tuple[str, str]] = set()
        for p in portfolio.positions:
            if p.ticker == "RUB000UTSTOM":
                continue
            side = "short" if int(p.quantity) < 0 else "long"
            real_keys.add((p.ticker.upper(), side))
        closed = await db.purge_phantom_broker_positions(
            broker="tinkoff",
            account_id=portfolio.account_id,
            real_keys=real_keys,
        )
        return {"ok": True, "closed_phantoms": closed, "broker_positions": len(real_keys)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reconcile failed: {exc}")


@app.post("/api/sandbox/close_position")
async def api_sandbox_close_position(req: Request):
    """Close an open sandbox position by sending an opposite market order.

    Body: {"ticker": "SBER", "qty": 18}
    qty is a signed number of shares from the broker portfolio
    (positive for long, negative for short). We close only whole lots and
    never round up, to avoid accidentally reversing the position.
    """
    if not app_config.TINKOFF_SANDBOX:
        raise HTTPException(status_code=400, detail="Not running in sandbox mode")
    if not app_config.TINKOFF_TOKEN:
        raise HTTPException(status_code=400, detail="Tinkoff token not configured")
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body")
    ticker = (body.get("ticker") or "").upper()
    try:
        signed_qty = int(body.get("qty") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="qty must be an integer")
    if not ticker or signed_qty == 0:
        raise HTTPException(status_code=422, detail="ticker and non-zero qty are required")

    from brokers.tinkoff_client import TinkoffClient
    client = TinkoffClient(sandbox=True)
    try:
        # Resolve instrument to get lot size.
        instr = await client.find_instrument(ticker)
        if not instr:
            raise HTTPException(status_code=404, detail=f"Instrument {ticker} not found")
        lot = int(instr.get("lot", 1) or 1)
        # Close only whole lots, floor towards zero. Rounding up could flip the position side.
        lots = abs(signed_qty) // lot
        if lots == 0:
            raise HTTPException(
                status_code=422,
                detail=f"Position size {signed_qty} shares is smaller than one lot ({lot} shares)",
            )
        # For a long position we sell; for a short position we buy.
        order_side = "sell" if signed_qty > 0 else "buy"
        result = await client.place_market_order(
            ticker=ticker, side=order_side, lots=lots
        )
        if result.status in ("EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_NEW", "EXECUTION_REPORT_STATUS_PARTIALLYFILL"):
            account_id = await client.resolve_account_id()
            filled_qty = lots * lot
            # Snapshot the last market price before submitting the close so
            # the trade shows a real exit price in /api/sandbox_orders
            # (otherwise every close row has a blank "entry_px" column).
            close_px = await client.get_ticker_price(ticker)
            await db.save_broker_order(
                proposal_id=None,
                ticker=ticker,
                side=order_side,
                broker="tinkoff",
                account_id=account_id,
                order_id=result.order_id,
                lots=lots,
                qty=filled_qty,
                entry_px=close_px,
                status="pending" if result.status in ("EXECUTION_REPORT_STATUS_NEW", "EXECUTION_REPORT_STATUS_PARTIALLYFILL") else "filled",
                broker_message=result.message,
                environment="sandbox",
            )
            # Mirror the close in our local position tracking so the UI and DB stay in sync.
            close_side = "sell" if signed_qty > 0 else "buy"
            await db.update_broker_position(
                ticker=ticker,
                side=close_side,
                qty=filled_qty,
                lots=lots,
                broker="tinkoff",
                account_id=account_id,
                reason="manual_close",
            )
            return {
                "ok": True,
                "order_id": result.order_id,
                "status": result.status,
                "lots": lots,
                "qty": filled_qty,
                "side": order_side,
                "requested_qty": signed_qty,
                "remaining_qty": signed_qty - (filled_qty if signed_qty > 0 else -filled_qty),
                "lot": lot,
            }
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Order rejected: {result.status} — {result.message}",
            )
    finally:
        await client.close()


@app.post("/api/sandbox/reset")
async def api_sandbox_reset(req: Request):
    """Reset sandbox portfolio: sell everything via market orders.

    Used when pre-filled sandbox positions (RTKM/CHMF etc) refuse to close
    with 30034 Not enough balance. Strategy:

    1. Try to sell every open position via ``place_market_order``.
    2. If any close hits 30034 (the sandbox reservation problem), fall back to
       closing the entire sandbox account and opening a fresh one with
       SANDBOX_STARTING_CAPITAL. The new account id is written to .env so the
       next restart picks it up.

    Also clears our local tracking tables so the UI starts fresh.
    """
    if not app_config.TINKOFF_SANDBOX:
        raise HTTPException(status_code=400, detail="Not running in sandbox mode")
    if not app_config.TINKOFF_TOKEN:
        raise HTTPException(status_code=400, detail="Tinkoff token not configured")

    body: dict = {}
    try:
        body = await req.json()
    except Exception:
        body = {}
    force_account_recreate = bool(body.get("force_account_recreate"))

    from brokers.tinkoff_client import TinkoffClient
    client = TinkoffClient(sandbox=True)
    closed: list[dict] = []
    sell_failures: list[dict] = []
    recreated = False
    new_account_id = ""
    try:
        account_id = await client.resolve_account_id()
        portfolio = await client.get_portfolio()
        positions = [p for p in portfolio.positions if p.ticker and p.ticker != "RUB000UTSTOM"]

        # Phase 1: try to sell each non-zero position via market order.
        for pos in positions:
            qty = int(pos.quantity)
            if qty == 0:
                continue
            instr = await client.find_instrument(pos.ticker)
            if not instr:
                sell_failures.append({"ticker": pos.ticker, "reason": "instrument not found"})
                continue
            lot = int(instr.get("lot", 1) or 1)
            lots = abs(qty) // lot
            if lots == 0:
                sell_failures.append({"ticker": pos.ticker, "reason": "qty smaller than one lot"})
                continue
            order_side = "sell" if qty > 0 else "buy"
            try:
                result = await client.place_market_order(
                    ticker=pos.ticker, side=order_side, lots=lots
                )
            except Exception as exc:
                sell_failures.append({"ticker": pos.ticker, "error": str(exc)})
                continue
            if result.status in (
                "EXECUTION_REPORT_STATUS_FILL",
                "EXECUTION_REPORT_STATUS_NEW",
                "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
            ):
                closed.append({
                    "ticker": pos.ticker,
                    "side": order_side,
                    "lots": lots,
                    "qty": lots * lot,
                    "order_id": result.order_id,
                    "status": result.status,
                })
            else:
                sell_failures.append({
                    "ticker": pos.ticker,
                    "status": result.status,
                    "message": result.message,
                })

        # Phase 2: if any close failed (or caller forced), recreate the account.
        if sell_failures or force_account_recreate:
            logger.warning(
                f"Sandbox reset: {len(sell_failures)} sells failed, "
                f"recreating sandbox account"
            )
            try:
                await client.close_sandbox_account(account_id)
            except Exception as exc:
                logger.warning(f"close_sandbox_account failed: {exc}")
            new_account_id = await client.open_sandbox_account()
            await client.sandbox_pay_in(app_config.SANDBOX_STARTING_CAPITAL, new_account_id)
            recreated = True
            # Persist new account id so next restart picks it up.
            try:
                _persist_tinkoff_account_id(new_account_id)
            except Exception as exc:
                logger.warning(f"Could not persist new account id: {exc}")

        # Phase 3: clear local tracking tables.
        try:
            await db.clear_broker_positions_for_account(account_id)
        except Exception as exc:
            logger.warning(f"clear broker_positions failed: {exc}")
        try:
            await db.clear_journal_for_environment("sandbox")
        except Exception as exc:
            logger.warning(f"clear journal failed: {exc}")
        try:
            await db.clear_broker_orders_for_environment("sandbox")
        except Exception as exc:
            logger.warning(f"clear broker_orders failed: {exc}")

        return {
            "ok": True,
            "closed_via_sell": closed,
            "sell_failures": sell_failures,
            "account_recreated": recreated,
            "new_account_id": new_account_id or account_id,
        }
    finally:
        await client.close()


@app.get("/api/guards")
async def api_guards():
    """Return upcoming dividend cutoffs and CBR meeting dates."""
    from datetime import date

    cbr_meeting, cbr_pre, next_cbr = db.cbr_soft_mode_state()
    upcoming_dividends = await db.tickers_with_upcoming_dividend_cutoff(look_ahead_days=14)
    return {
        "today": date.today().isoformat(),
        "cbr_soft_mode": {
            "enabled": app_config.CBR_SOFT_MODE_ENABLED,
            "is_meeting_day": cbr_meeting,
            "is_pre_meeting_day": cbr_pre,
            "next_meeting_date": next_cbr.isoformat() if next_cbr else None,
            "configured_dates": sorted(app_config.CBR_MEETING_DATES),
        },
        "upcoming_dividends": upcoming_dividends,
    }


@app.get("/api/settings/auto_trading")
async def api_get_auto_trading():
    """Return current auto-trading state and effective trading mode."""
    if not app_config.PAPER_TRADING and app_config.SEMI_AUTO_TRADING:
        mode = "semi_auto"
    elif not app_config.PAPER_TRADING:
        mode = "live"
    else:
        mode = "paper"
    return {
        "auto_trading_enabled": app_config.AUTO_TRADING_ENABLED,
        "auto_trade": app_config.AUTO_TRADE,
        "auto_trade_min_confidence": app_config.AUTO_TRADE_MIN_CONFIDENCE,
        "mode": mode,
        "paper_trading": app_config.PAPER_TRADING,
        "semi_auto_trading": app_config.SEMI_AUTO_TRADING,
        "circuit_breaker": app_config.CIRCUIT_BREAKER_ENABLED,
        "sandbox": app_config.TINKOFF_SANDBOX,
        "max_daily_loss_pct": app_config.MAX_DAILY_LOSS_PCT,
    }


async def api_set_auto_trading(req: AutoTradingSetting):
    """Toggle auto-trading AND auto-trade (no manual confirmation) flags."""
    log = __import__("loguru").logger
    if req.auto_trading_enabled is not None:
        await db.save_auto_trading_enabled(req.auto_trading_enabled)
        app_config.AUTO_TRADING_ENABLED = req.auto_trading_enabled
        log.info(f"Auto-trading toggled via API: {app_config.AUTO_TRADING_ENABLED}")
    if req.auto_trade is not None:
        await db.save_auto_trade_enabled(req.auto_trade)
        app_config.AUTO_TRADE = req.auto_trade
        log.info(f"Auto-trade toggled via API: {app_config.AUTO_TRADE}")
    return {
        "auto_trading_enabled": app_config.AUTO_TRADING_ENABLED,
        "auto_trade": app_config.AUTO_TRADE,
    }


app.post("/api/settings/auto_trading")(api_set_auto_trading)


class CircuitBreakerSetting(BaseModel):
    enabled: bool
    decided_by: str = "api"


async def api_set_circuit_breaker(req: CircuitBreakerSetting):
    """Emergency switch to block all new real broker orders."""
    app_config.CIRCUIT_BREAKER_ENABLED = req.enabled
    logger = __import__("loguru").logger
    logger.warning(f"Circuit breaker toggled via API: {req.enabled}")
    return {"circuit_breaker_enabled": app_config.CIRCUIT_BREAKER_ENABLED}


app.post("/api/settings/circuit_breaker")(api_set_circuit_breaker)


# Fallback: serve desktop.html for unknown paths
@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    file = MOBILE_DIR / full_path
    if file.exists() and file.is_file():
        return FileResponse(file)
    return _html_response(MOBILE_DIR / "desktop.html")