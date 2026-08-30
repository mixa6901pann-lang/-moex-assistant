"""Demo: feed a synthetic bullish 5m candle series to IntradayAgent and
print the semi-auto proposal that would appear in the terminal.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta

from core.intraday_agent import agent as intraday_agent
from strategies.fees import estimate_trade_costs


def _build_bounce_candles(count: int = 100, base_price: float = 250.0) -> list[dict]:
    """Build synthetic 5m candles ending near the daily low with bounce_up signal."""
    candles: list[dict] = []
    price = base_price
    now = datetime(2026, 7, 20, 10, 0, 0)
    for i in range(count):
        if i < count * 0.78:
            drift = -0.10  # trend down to session low
        elif i < count * 0.85:
            drift = 0.0  # first bottom near low
        elif i < count * 0.90:
            drift = 0.05  # tiny pullback still near low
        else:
            drift = 0.18  # small bounce, stays near daily low
        noise = random.uniform(-0.06, 0.06)
        open_px = round(price, 2)
        close_px = round(open_px + drift + noise, 2)
        high_px = round(max(open_px, close_px) + random.uniform(0.03, 0.12), 2)
        low_px = round(min(open_px, close_px) - random.uniform(0.03, 0.12), 2)
        # Volume: low baseline, huge spike near double bottom / bounce
        if i < count * 0.78:
            volume = int(random.uniform(4000, 7000))
        elif i >= count * 0.90:
            volume = int(random.uniform(50000, 80000))
        else:
            volume = int(random.uniform(20000, 35000))
        candles.append({
            "begin": (now + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S"),
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": volume,
        })
        price = close_px
    return candles


async def main():
    candles = _build_bounce_candles(count=100, base_price=255.0)
    result = await intraday_agent.analyze(
        ticker="SBER",
        candles_5m=candles,
        order_book=None,
        use_llm=False,
        calibrate=False,
    )

    print(f"Signal: {result.signal}")
    print(f"Direction: {result.direction}")
    print(f"Confidence: {result.confidence}%")
    print(f"Entry: {result.entry}, Stop: {result.stop}, Take: {result.take}")
    print(f"Reason: {result.reason}")

    if result.signal == "no_signal" or result.direction == "neutral":
        print("\nNo directional signal generated. Try again or tune candles.")
        return
    if result.confidence < 60:
        print("\nSignal confidence too low for semi-auto proposal.")
        return

    fee_est = estimate_trade_costs(result.entry or 0, result.take or 0, 1)
    proposal = (
        f"[ROBOT] Intraday предлагаю сделку\n"
        f"{result.direction.upper()} SBER\n"
        f"Сигнал: {result.signal}\n"
        f"Цена: {result.entry or '-'} RUB | Уверенность: {result.confidence}%\n"
        f"Стоп: {result.stop or '-'} RUB | Цель: {result.take or '-'} RUB\n"
        f"Комиссия (~): {fee_est.total_commission_rub if (result.entry and result.take) else '-'} RUB\n"
        f"Причина: {result.reason[:120]}\n\n"
        f"Для подтверждения напиши: /confirm_trade SBER {result.direction} 1"
    )
    print("\n" + "=" * 50)
    print(proposal)
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
