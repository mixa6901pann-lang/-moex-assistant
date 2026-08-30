"""IntradayAgent — краткосрочный внутридневной анализ по 5-минутным свечам.

Технический скоринг без LLM + каскадная LLM-подтверждение:
1. Сначала быстрая локальная Gemma/Ollama.
2. При неуверенности или ошибке — fallback на YandexGPT.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from core.config import INTRADAY_MIN_FACTORS, INTRADAY_MIN_VOLUME_RATIO, INTRADAY_COOLDOWN_MINUTES
from core.llm import call_llm
from strategies.indicators import add_indicators, compute_levels, df_from_candles, predict_bounce
import time

logger = logging.getLogger(__name__)


# In-memory cooldown tracker: ticker -> last unix timestamp of a confirmed signal
_intraday_signal_cooldown: dict[str, float] = {}


@dataclass(frozen=True)
class IntradayResult:
    """Результат внутридневного анализа."""

    signal: str  # bounce_up | bounce_down | continuation | no_signal
    direction: str  # long | short | neutral
    confidence: int  # 0–100
    entry: float | None
    stop: float | None
    take: float | None
    atr: float | None
    reason: str
    llm_summary: str | None
    provider_used: str | None  # ollama | yandex | none
    signals_used: list[str]
    metrics: dict[str, Any]


SYSTEM_PROMPT = """Ты — внутридневной трейдер Мосбиржи. Ты анализируешь 5-минутные свечи и текущее состояние рынка.

Твоя задача — дать краткий внутридневной сигнал по тикеру. Используй только предоставленные данные.

Правила:
- bounce_up: цена у дневного минимума, есть признаки отскока (RSI перепродан, объём растёт, цена возвращается к VWAP).
- bounce_down: цена у дневного максимума, есть признаки отката (RSI перекуплен, объём растёт, цена падает к VWAP).
- continuation: цена чётко выше/ниже VWAP, ADX сильный, нет близости к дневным экстремумам.
- no_signal: нет чёткого направления.

Ответ строго JSON без markdown:
{
  "signal": "bounce_up|bounce_down|continuation|no_signal",
  "direction": "long|short|neutral",
  "confidence": 0-100,
  "entry": число,
  "stop": число,
  "take": число,
  "reason": "1-2 предложения",
  "summary": "1 предложение общего вывода"
}

Уверенность должна быть высокой только при сильном техническом подтверждении. Не гадай."""


# Пороги для технического скоринга
_BOUNCE_ZONE_PCT_UP = 1.0    # зона отскока вверх (у дневного low)
_BOUNCE_ZONE_PCT_DOWN = 1.5  # зона отката вниз (у дневного high)
_RSI_OVERSOLD = 35
_RSI_OVERBOUGHT = 60
_VOL_SPIKE_UP = 1.5          # объёмный импульс для отскока вверх
_VOL_SPIKE_DOWN = 1.0        # объёмный импульс для отката вниз
_VWAP_PROXIMITY = 0.003      # 0.3%
_SPREAD_ILLIQUID_PCT = 0.3   # 0.3%
_MIN_BOUNCE_FACTORS_UP = 2   # near_low + 1 дополнительный фактор
_MIN_BOUNCE_FACTORS_DOWN = 2 # near_high + 1 дополнительный фактор


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
        if np.isnan(v) or np.isinf(v):
            return None
        return v
    except Exception:
        return None


class IntradayAgent:
    """Агент внутридневного анализа по 5-минутным свечам."""

    async def analyze(
        self,
        ticker: str,
        candles_5m: list[dict],
        order_book: dict | None = None,
        daily_ohlc: dict | None = None,
        use_llm: bool = True,
        calibrate: bool = True,
    ) -> IntradayResult:
        """Проанализировать интрадей-ситуацию по тикеру.

        Args:
            ticker: тикер Мосбиржи.
            candles_5m: список 5-минутных свечей за текущую сессию.
            order_book: данные стакана (spread, imbalance и т.д.).
            daily_ohlc: дневной open/high/low/close (можно вычислить из candles_5m).
            use_llm: включить LLM-подтверждение.
            calibrate: применять калибровку confidence (количество свечей, стакан, спред).
        """
        if not candles_5m or len(candles_5m) < 3:
            return self._empty_result("Недостаточно 5-минутных данных", candles_5m)

        df = df_from_candles(candles_5m)
        if df.empty or "close" not in df.columns:
            return self._empty_result("Нет корректных свечных данных", candles_5m)

        df = add_indicators(df)
        last = df.iloc[-1]
        price = _safe_float(last.get("close"))
        if price is None:
            return self._empty_result("Не удалось получить текущую цену", candles_10m)

        # Дневные уровни
        if daily_ohlc:
            daily_open = _safe_float(daily_ohlc.get("open"))
            daily_high = _safe_float(daily_ohlc.get("high"))
            daily_low = _safe_float(daily_ohlc.get("low"))
        else:
            daily_open = _safe_float(df["open"].iloc[0])
            daily_high = _safe_float(df["high"].max())
            daily_low = _safe_float(df["low"].min())

        # Данные предыдущей свечи для фильтров разворота
        prev_close = None
        prev_high = None
        prev_low = None
        current_low = None
        current_high = None
        if len(df) >= 2:
            prev = df.iloc[-2]
            prev_close = _safe_float(prev.get("close"))
            prev_high = _safe_float(prev.get("high"))
            prev_low = _safe_float(prev.get("low"))
            current_low = _safe_float(last.get("low"))
            current_high = _safe_float(last.get("high"))
        current_low = _safe_float(last.get("low"))
        current_high = _safe_float(last.get("high"))

        vwap = _safe_float(last.get("vwap"))
        rsi = _safe_float(last.get("rsi"))
        atr = _safe_float(last.get("atr"))
        vol_ratio = _safe_float(last.get("vol_ratio"))
        adx = _safe_float(last.get("adx"))

        metrics: dict[str, Any] = {
            "price": price,
            "daily_open": daily_open,
            "daily_high": daily_high,
            "daily_low": daily_low,
            "vwap": vwap,
            "rsi": rsi,
            "atr": atr,
            "vol_ratio": vol_ratio,
            "adx": adx,
            "spread_pct": None,
            "imbalance": None,
        }

        if order_book and not order_book.get("error"):
            metrics["spread_pct"] = _safe_float(order_book.get("spread_pct"))
            metrics["imbalance"] = _safe_float(order_book.get("imbalance"))

        # bounce/reversal prediction from daily context (extra signal source)
        bounce = predict_bounce(df) if len(df) >= 30 else {"probability": 0, "direction": "none", "confidence_label": "low", "factors": [], "reasons": []}
        metrics["bounce_prediction"] = bounce

        # Технический скоринг
        tech = self._technical_signal(
            df=df,
            price=price,
            daily_high=daily_high,
            daily_low=daily_low,
            vwap=vwap,
            rsi=rsi,
            vol_ratio=vol_ratio,
            adx=adx,
            prev_close=prev_close,
            prev_high=prev_high,
            prev_low=prev_low,
            current_high=current_high,
            current_low=current_low,
            metrics=metrics,
        )

        signal = tech["signal"]
        direction = tech["direction"]
        confidence = tech["confidence"]
        reason = tech["reason"]
        signals_used = tech["signals_used"]

        # Filter: require enough confirming factors and volume confirmation
        if signal != "no_signal" and direction in ("long", "short"):
            # Count volume confirmation as one extra factor if present
            has_volume = vol_ratio is not None and vol_ratio >= INTRADAY_MIN_VOLUME_RATIO
            factor_count = len(signals_used)
            # For continuation signals volume is not automatically counted, add it if present
            if signal == "continuation" and has_volume:
                signals_used.append("volume_confirmation")
                factor_count += 1

            # Reject signals that lack enough confirming factors
            if factor_count < INTRADAY_MIN_FACTORS:
                logger.debug(
                    f"Intraday {ticker}: rejected, factor_count {factor_count} < {INTRADAY_MIN_FACTORS}"
                )
                signal = "no_signal"
                direction = "neutral"
                confidence = 0
                reason = f"Недостаточно подтверждающих факторов ({factor_count} из {INTRADAY_MIN_FACTORS})."
                signals_used = []
            elif not has_volume:
                logger.debug(
                    f"Intraday {ticker}: rejected, vol_ratio {vol_ratio:.2f} < {INTRADAY_MIN_VOLUME_RATIO}"
                )
                signal = "no_signal"
                direction = "neutral"
                confidence = 0
                reason = f"Нет объёмного подтверждения (vol_ratio {vol_ratio:.2f} < {INTRADAY_MIN_VOLUME_RATIO})."
                signals_used = []
            else:
                # Cooldown filter: do not repeat confirmed directional signals too often
                now = time.time()
                last = _intraday_signal_cooldown.get(ticker.upper())
                if last and (now - last) < INTRADAY_COOLDOWN_MINUTES * 60:
                    logger.debug(
                        f"Intraday {ticker}: rejected, cooldown active ({(now - last) / 60:.1f} min)")
                    signal = "no_signal"
                    direction = "neutral"
                    confidence = 0
                    reason = f"Кулдаун {INTRADAY_COOLDOWN_MINUTES} мин между сигналами по {ticker}."
                    signals_used = []
                else:
                    _intraday_signal_cooldown[ticker.upper()] = now

        # Boost confidence when intraday signal aligns with daily bounce prediction
        if (
            bounce["probability"] >= 45
            and bounce["direction"] == direction
            and "trend_weakening_confirmed" in bounce["factors"]
        ):
            confidence = min(100, confidence + int(bounce["probability"] * 0.25))
            signals_used.append("daily_bounce_aligned")
            reason += f" Предиктор отскока: {bounce['probability']}% (тренд ослабевает)."
        elif bounce["probability"] >= 45 and bounce["direction"] != direction and bounce["direction"] != "none":
            confidence = max(0, confidence - 15)
            signals_used.append("daily_bounce_contrarian")
            reason += " Предиктор отскока противоречит внутридневному сигналу."

        levels = _compute_intraday_levels(direction, price, atr) if direction in ("long", "short") else None

        entry = levels["entry"] if levels else None
        stop = levels["stop"] if levels else None
        take = levels["take"] if levels else None

        llm_summary: str | None = None
        provider_used: str | None = None

        if use_llm:
            llm_res = await self._llm_cascade(ticker, metrics, signal, direction, confidence)
            if llm_res:
                llm_summary = llm_res.get("summary") or llm_res.get("reason")
                provider_used = llm_res.get("provider_used")
                # Если LLM уверен и согласен с направлением — можно усилить confidence
                llm_conf = llm_res.get("confidence")
                llm_signal = llm_res.get("signal")
                if (
                    isinstance(llm_conf, int)
                    and llm_conf >= 60
                    and llm_res.get("direction") == direction
                    and llm_signal == signal
                ):
                    confidence = min(100, int((confidence + llm_conf) / 2) + 5)
                elif isinstance(llm_conf, int) and llm_conf < 40:
                    confidence = max(0, confidence - 15)

        # Калибровка
        if calibrate:
            confidence = self._calibrate_confidence(confidence, len(df), order_book, metrics)

        return IntradayResult(
            signal=signal,
            direction=direction,
            confidence=confidence,
            entry=entry,
            stop=stop,
            take=take,
            atr=atr,
            reason=reason,
            llm_summary=llm_summary,
            provider_used=provider_used,
            signals_used=signals_used,
            metrics=metrics,
        )

    @staticmethod
    def _technical_signal(
        df: pd.DataFrame,
        price: float,
        daily_high: float | None,
        daily_low: float | None,
        vwap: float | None,
        rsi: float | None,
        vol_ratio: float | None,
        adx: float | None,
        prev_close: float | None,
        prev_high: float | None,
        prev_low: float | None,
        current_high: float | None,
        current_low: float | None,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        """Чисто технический скоринг с подтверждением разворота."""
        signals_used: list[str] = []

        dist_to_high = None
        dist_to_low = None
        if daily_high and daily_high > 0:
            dist_to_high = (price / daily_high - 1) * 100
        if daily_low and daily_low > 0:
            dist_to_low = (price / daily_low - 1) * 100

        metrics["dist_to_high_pct"] = dist_to_high
        metrics["dist_to_low_pct"] = dist_to_low

        near_high = dist_to_high is not None and abs(dist_to_high) <= _BOUNCE_ZONE_PCT_DOWN
        near_low = dist_to_low is not None and abs(dist_to_low) <= _BOUNCE_ZONE_PCT_UP

        rsi_oversold = rsi is not None and rsi < _RSI_OVERSOLD
        rsi_overbought = rsi is not None and rsi > _RSI_OVERBOUGHT
        vol_spike_up = vol_ratio is not None and vol_ratio > _VOL_SPIKE_UP
        vol_spike_down = vol_ratio is not None and vol_ratio > _VOL_SPIKE_DOWN

        around_vwap = False
        above_vwap_band = False
        below_vwap_band = False
        if vwap and vwap > 0:
            around_vwap = abs(price / vwap - 1) <= _VWAP_PROXIMITY

        # double bottom / double top — low/high не обновляет экстремум
        double_bottom = False
        double_top = False
        if current_low is not None and prev_low is not None and prev_low > 0:
            double_bottom = abs(current_low / prev_low - 1) <= 0.001
        if current_high is not None and prev_high is not None and prev_high > 0:
            double_top = abs(current_high / prev_high - 1) <= 0.001

        # Факторы для bounce_up
        bounce_up_factors = 0
        if near_low:
            bounce_up_factors += 1
            signals_used.append("near_daily_low")
        if rsi_oversold:
            bounce_up_factors += 1
            signals_used.append("rsi_oversold")
        if vol_spike_up:
            bounce_up_factors += 1
            signals_used.append("volume_spike")
        if around_vwap:
            bounce_up_factors += 1
            signals_used.append("price_around_vwap")
        if double_bottom:
            bounce_up_factors += 1
            signals_used.append("double_bottom")

        # Факторы для bounce_down
        bounce_down_factors = 0
        if near_high:
            bounce_down_factors += 1
            signals_used.append("near_daily_high")
        if rsi_overbought:
            bounce_down_factors += 1
            signals_used.append("rsi_overbought")
        if vol_spike_down:
            bounce_down_factors += 1
            signals_used.append("volume_spike")
        if around_vwap:
            bounce_down_factors += 1
            signals_used.append("price_around_vwap")
        if double_top:
            bounce_down_factors += 1
            signals_used.append("double_top")

        # Определение сигнала
        signal = "no_signal"
        direction = "neutral"
        confidence = 0
        reason = "Нет чёткого внутридневного сигнала."

        if near_low and bounce_up_factors >= _MIN_BOUNCE_FACTORS_UP:
            signal = "bounce_up"
            direction = "long"
            confidence = 40 + bounce_up_factors * 12
            if double_bottom:
                confidence += 8
            parts = ["цена у дневного лоу"]
            if rsi_oversold:
                parts.append("RSI перепродан")
            if vol_spike_up:
                parts.append("объём выше среднего")
            if around_vwap:
                parts.append("цена у VWAP")
            if double_bottom:
                parts.append("double bottom")
            reason = "Возможен отскок вверх: " + ", ".join(parts) + "."
        elif near_high and bounce_down_factors >= _MIN_BOUNCE_FACTORS_DOWN:
            signal = "bounce_down"
            direction = "short"
            confidence = 40 + bounce_down_factors * 12
            if double_top:
                confidence += 8
            parts = ["цена у дневного хая"]
            if rsi_overbought:
                parts.append("RSI перекуплен")
            if vol_spike_down:
                parts.append("объём выше среднего")
            if around_vwap:
                parts.append("цена у VWAP")
            if double_top:
                parts.append("double top")
            reason = "Возможен откат вниз: " + ", ".join(parts) + "."
        elif vwap:
            above_vwap = price > vwap * (1 + _VWAP_PROXIMITY)
            below_vwap = price < vwap * (1 - _VWAP_PROXIMITY)
            trend_factors = 0
            if above_vwap:
                direction = "long"
                signals_used.append("above_vwap")
                trend_factors += 1
            elif below_vwap:
                direction = "short"
                signals_used.append("below_vwap")
                trend_factors += 1
            if adx is not None and adx > 25:
                trend_factors += 1
                signals_used.append("adx_strong")
            if vol_spike_up or vol_spike_down:
                trend_factors += 1
                signals_used.append("volume_spike")

            if trend_factors >= 2 and not near_high and not near_low:
                signal = "continuation"
                confidence = 40 + trend_factors * 12
                reason = (
                    f"Продолжение {'восходящего' if direction == 'long' else 'медвежьего'} тренда: "
                    f"цена {'выше' if direction == 'long' else 'ниже'} VWAP"
                )
                if adx is not None and adx > 25:
                    reason += f", ADX {adx:.1f}"
                reason += "."

        return {
            "signal": signal,
            "direction": direction,
            "confidence": min(confidence, 95),
            "reason": reason,
            "signals_used": list(dict.fromkeys(signals_used)),
        }

    async def _llm_cascade(
        self,
        ticker: str,
        metrics: dict[str, Any],
        signal: str,
        direction: str,
        confidence: int,
    ) -> dict[str, Any] | None:
        """Сначала Gemma/Ollama, при сомнении — Yandex."""
        user = self._build_llm_prompt(ticker, metrics, signal, direction, confidence)

        # Попытка 1: Ollama/Gemma
        raw_ollama = await call_llm(SYSTEM_PROMPT, user, max_tokens=256, provider="ollama")
        parsed = self._parse_llm_response(raw_ollama)

        if parsed and not self._is_uncertain_response(parsed, raw_ollama):
            parsed["provider_used"] = "ollama"
            return parsed

        # Попытка 2: Yandex
        raw_yandex = await call_llm(SYSTEM_PROMPT, user, max_tokens=256, provider="yandex")
        parsed_yandex = self._parse_llm_response(raw_yandex)
        if parsed_yandex:
            parsed_yandex["provider_used"] = "yandex"
            return parsed_yandex

        return None

    @staticmethod
    def _build_llm_prompt(
        ticker: str,
        metrics: dict[str, Any],
        signal: str,
        direction: str,
        confidence: int,
    ) -> str:
        return f"""Тикер: {ticker.upper()}

Текущая цена: {metrics.get('price')}
Дневной диапазон: открытие {metrics.get('daily_open')} / high {metrics.get('daily_high')} / low {metrics.get('daily_low')}
VWAP: {metrics.get('vwap')}
RSI(14): {metrics.get('rsi')}
ATR: {metrics.get('atr')}
Объём отн. среднего: {metrics.get('vol_ratio')}
ADX: {metrics.get('adx')}
Расстояние до дневного high: {metrics.get('dist_to_high_pct'):.2f}%
Расстояние до дневного low: {metrics.get('dist_to_low_pct'):.2f}%

Предварительный технический сигнал: {signal}, направление {direction}, уверенность {confidence}%.

Подтверди или опровергни этот сигнал и верни JSON."""

    @staticmethod
    def _parse_llm_response(raw: str) -> dict[str, Any] | None:
        if not raw:
            return None
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None

        signal = data.get("signal", "no_signal")
        if signal not in ("bounce_up", "bounce_down", "continuation", "no_signal"):
            signal = "no_signal"

        direction = data.get("direction", "neutral")
        if direction not in ("long", "short", "neutral"):
            direction = "neutral"

        conf = data.get("confidence", 50)
        try:
            conf = int(conf)
        except Exception:
            conf = 50
        conf = max(0, min(100, conf))

        return {
            "signal": signal,
            "direction": direction,
            "confidence": conf,
            "entry": _safe_float(data.get("entry")),
            "stop": _safe_float(data.get("stop")),
            "take": _safe_float(data.get("take")),
            "reason": str(data.get("reason", "")),
            "summary": str(data.get("summary", "")),
        }

    @staticmethod
    def _is_uncertain_response(parsed: dict[str, Any], raw: str) -> bool:
        if parsed["confidence"] < 50:
            return True
        lowered = raw.lower()
        uncertain_words = (
            "не уверен",
            "противоречиво",
            "недостаточно данных",
            "сложно сказать",
            "возможно",
            "maybe",
            "uncertain",
        )
        if any(w in lowered for w in uncertain_words):
            return True
        if parsed["signal"] == "no_signal":
            return True
        return False

    @staticmethod
    def _calibrate_confidence(
        confidence: int,
        n_candles: int,
        order_book: dict | None,
        metrics: dict[str, Any],
    ) -> int:
        if n_candles < 20:
            confidence = min(confidence, 70)
        if not order_book or order_book.get("error"):
            confidence = min(confidence, 60)
        spread_pct = metrics.get("spread_pct")
        if spread_pct is not None and spread_pct > _SPREAD_ILLIQUID_PCT:
            confidence = max(0, confidence - 15)
        return max(0, min(100, confidence))

    @staticmethod
    def _empty_result(reason: str, candles_10m: list[dict]) -> IntradayResult:
        return IntradayResult(
            signal="no_signal",
            direction="neutral",
            confidence=0,
            entry=None,
            stop=None,
            take=None,
            atr=None,
            reason=reason,
            llm_summary=None,
            provider_used=None,
            signals_used=[],
            metrics={"candles_count": len(candles_5m) if candles_5m else 0},
        )

    def to_dict(self, result: IntradayResult) -> dict[str, Any]:
        """Сериализовать результат для JSON-ответа API."""
        return {
            "signal": result.signal,
            "direction": result.direction,
            "confidence": result.confidence,
            "entry": result.entry,
            "stop": result.stop,
            "take": result.take,
            "reason": result.reason,
            "llm_summary": result.llm_summary,
            "provider_used": result.provider_used,
            "signals_used": result.signals_used,
            "metrics": result.metrics,
        }


def _compute_intraday_levels(direction: str, close: float, atr: float | None) -> dict[str, float] | None:
    """5-минутные уровни: широкий стоп (2.5 ATR) и быстрый тейк (1.5 ATR)."""
    if close <= 0 or not close:
        return None
    if atr is None or pd.isna(atr) or atr <= 0:
        atr = close * 0.01

    # Calibrated for 5m noise: wider stop, faster take
    stop_mult = 2.5
    take_mult = 1.5

    if direction == "long":
        entry = close - min(atr * 0.3, close * 0.005)
        stop = close - atr * stop_mult
        take = close + atr * take_mult
    elif direction == "short":
        entry = close + min(atr * 0.3, close * 0.005)
        stop = close + atr * stop_mult
        take = close - atr * take_mult
    else:
        return None

    return {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "take": round(take, 2),
    }


# Глобальный экземпляр для удобного импорта
agent = IntradayAgent()
