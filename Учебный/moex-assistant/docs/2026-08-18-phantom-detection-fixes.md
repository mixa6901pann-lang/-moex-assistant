# 18 августа 2026 — фиксы фантомного детекта и ручных закрытий

## Проблемы

1. **Фантомный плюс NVTK +72.82 ₽ (broker_take_phantom @ 875.69)** — реальная SHORT @912.10 была закрыта брокером по стопу @923.88, но reconcile всегда брал `take_px=875.69` если он есть. На UI отображался фейковый плюс.

2. **Любая закрытая позиция автоматом помечалась `_phantom`** — phantom-detection использовал `real_keys = portfolio positions`, а закрытая позиция по определению вне портфеля.

3. **Ручное закрытие через приложение** → брокер больше не имеет позицию → reconcile писал `broker_manual` с `exit_px = avg_entry_px` → P&L скрывался (=0).

4. **Импорт `from datetime import datetime, timedelta, timezone as _dt, _td, _tz` внутри try-блока** затенял глобальный `datetime` и ломал `intraday_broker_reconcile` каждый тик с 20:54.

## Фиксы

### main.py

- **`intraday_broker_reconcile: open→close block`**
  - При пропадании open-позиции из портфеля брокера — загрузить `broker.operations` за 7 дней
  - Найти последнюю противоположную операцию (для SHORT ищем BUY, для LONG — SELL) с тем же `qty` и `date >= open_ts`
  - Если найдена → `exit_px = цена операции`, `reason = broker_manual` (или `broker_stop`/`broker_take` если цена совпадает ±0.05 с защитным ордером)
  - Если не найдена → fallback на старую логику (prefer_stop если SHORT+stop>entry или LONG+stop<entry)

- **`intraday_broker_reconcile: phantom-detection`**
  - Вместо `real_keys` для проверки фантомов — `broker.get_operations` за 7 дней → `fill_keys`
  - `_phantom` суффикс ставится ТОЛЬКО если нет ни в `real_keys`, ни в `fill_keys`
  - Также `broker_take_phantom` записи в `journal` не пишутся (был баг с дублированием)

- **module-level импорт `from datetime import date, datetime, timedelta, timezone`**
  - Удалён локальный импорт-алиасы в try-блоке phantom-detection
  - Удалён `from datetime import timezone` внутри `run_sentiment_scan`

### DB правки (18.08 вручную)

- `broker_positions.id=7 NVTK SHORT @912.10`: exit_px 923.90, close_reason `broker_stop` (убрали `_phantom`)
- `broker_positions.id=9 VKCO LONG 19 @137.30`: exit_px 133.35, close_reason `broker_manual` (стоп/тейк null)
- `journal.id=22 NVTK`: exit_px 923.90, reason `broker_stop`, pnl −23.60 (убрали broker_take_phantom)
- `journal.id=23`: добавлен VKCO LONG 19 @137.30 → 133.35, pnl −75.05, broker_manual
- `broker_positions.id=1,2,4,5,6` (MTSS/GAZP/SBER/MGNT/VKCO 17.08) удалены

## Итоговый P&L по journal

| Дата | Тикер | Сторона | P&L ₽ |
|------|-------|---------|-------|
| 17.08 | SBER | L | −10.89 |
| 17.08 | GAZP | L | −19.80 |
| 17.08 | MTSS | L | +42.30 |
| 17.08 | VKCO | S | −147.44 |
| 17.08 | MGNT | S | −94.76 |
| 18.08 | NVTK | S | −23.60 |
| 18.08 | VKCO | L | −75.05 |
| | | **Всего** | **−329.24** |

## Брокерские операции 18.08 (получено через GetOperations)

| Время | Тикер | Операция | Цена | P&L |
|-------|-------|----------|------|-----|
| 04:01 | NVTK | SHORT @ | 912.10 | |
| 08:42 | NVTK | BUY back (stop) | 923.90 | −23.60 |
| 08:42 | VKCO | BUY 19 | 137.30 | |
| 09:16 | NVTK | SHORT @ | 930.00 | |
| 14:36 | VKCO | SELL 19 (manual) | 133.35 | −75.05 |
| 15:38 | NVTK | BUY back | 937.00 | −14.00 |