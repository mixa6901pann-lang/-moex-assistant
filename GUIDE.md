# MOEX Assistant — Руководство пользователя

> Для общего обзора проекта смотри [`README.md`](README.md).  
> Для деплоя на сервер смотри [`DEPLOY.md`](DEPLOY.md).

---

## Что это

MOEX Assistant — автоматизированный ассистент для анализа и торговли акциями Московской биржи. Он собирает рыночные данные, новости, считает индикаторы и с помощью LLM формирует торговые идеи. Решение о реальных сделках принимаешь ты (полуавтоматический режим) или робот (live-режим, только при явном включении).

---

## Веб-интерфейс

Проект имеет две версии фронтенда:

- **Desktop** (`/desktop`) — левая панель, графики, полный анализ.
- **Mobile** (`/`) — компактная версия для телефона.

### Локальный запуск

```bash
cd moex-assistant
uvicorn api.mobile_api:app --host 127.0.0.1 --port 8080
```

- Desktop: `http://localhost:8080/desktop`
- Mobile: `http://localhost:8080`
- Health: `http://localhost:8080/health`

На сервере веб-UI отключён по умолчанию (`WEB_UI_ENABLED=false`). Чтобы включить, установи `WEB_UI_ENABLED=true` и обязательно задай сильный `API_KEY`.

---

## Установка и настройка

### 1. Зависимости

```bash
cd moex-assistant
pip install -e ".[dev]"
```

Требуется Python 3.11+.

### 2. Переменные окружения

```bash
cp .env.example .env
# отредактируй .env
```

Основные переменные:

| Переменная | Описание |
|---|---|
| `LLM_PROVIDER` | `anthropic`, `gemini`, `yandex`, `ollama`, `none` |
| `LLM_FALLBACK_ORDER` | Порядок fallback, например `yandex,ollama,anthropic,gemini` |
| `TINKOFF_TOKEN` | Токен Тинькофф Invest API |
| `PAPER_TRADING` | `true` — только виртуальные сделки |
| `SEMI_AUTO_TRADING` | `true` — робот предлагает, ты подтверждаешь |
| `AUTO_TRADING_ENABLED` | `true` — открывать автоматически |
| `TINKOFF_SANDBOX` | `true` — песочница Тинькофф |
| `API_KEY` | Ключ для защиты write-эндпоинтов |
| `WEB_UI_ENABLED` | `false` по умолчанию |

### 3. LLM-модель

**Вариант A — локальная Ollama (для ПК с RAM ≥ 4 ГБ):**

```bash
ollama pull gemma3:4b
ollama serve
```

В `.env`:
```env
LLM_PROVIDER=ollama
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=gemma3:4b
```

**Вариант B — облачный провайдер (для сервера или если Ollama не влезает):**

```env
LLM_PROVIDER=yandex
YANDEX_API_KEY=...
YANDEX_FOLDER_ID=...
```

или

```env
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Полный запуск

```bash
python main.py
```

Запускает:
- Планировщик задач (скринер, интрадей, вечернее решение и т.д.).
- FastAPI health endpoint на порту `HEALTH_PORT`.
- VK/Telegram ботов, если включены.

---

## Расписание задач

| Время | Задача |
|---|---|
| 10:00 МСК | Утренний скринер |
| */15 10–18:45 | Внутридневной мониторинг |
| */15 07–23:00 | RSS и сентимент |
| */30 07–23:00 | Геополитический риск |
| 18:00 | Вечернее торговое решение |
| 19:00 | Вечерний отчёт |
| 19:05 | Проверка точности прогнозов |
| 19:06 | Проверка бумажных позиций |
| Суб 12:00 | Обновление дивидендов |

---

## Торговые режимы

| Режим | Описание |
|---|---|
| **paper** | Виртуальный портфель, реальные деньги не используются. |
| **semi_auto** | Робот создаёт предложения; ты подтверждаешь в веб-UI. |
| **live** | Робот выставляет ордера сам. Требуется полный доступ Тинькофф. |

Безопасный старт: `PAPER_TRADING=true`, `TINKOFF_SANDBOX=true`.

---

## Риск-менеджмент

- Максимум 5 позиций одновременно.
- Риск до 2% капитала на сделку.
- Размер позиции от 0.5% до 5% капитала.
- Стоп-лосс 2×ATR, trailing stop 2.5×ATR.
- Фильтр ликвидности по среднему объёму.
- Учёт комиссии и платы за перенос перед входом.
- GeoRisk ≥ 7 — аварийное закрытие всех позиций.

---

## Безопасность

- `.env` и `.env.tinkoff` не попадают в git.
- Web UI отключён по умолчанию.
- Write-эндпоинты требуют `API_KEY`.
- CodeAct отключён по умолчанию.
- Приложение слушает `127.0.0.1`.

Подробнее: [`docs/2026-06-21—security-hardening-checklist.md`](docs/2026-06-21—security-hardening-checklist.md).

---

## Разработка

```bash
# Тесты
pytest

# Линтер
ruff check .

# Логи
tail -f logs/moex_$(date +%Y-%m-%d).log

# Перезапуск сервиса
supervisorctl restart moex
```

---

## Структура проекта

```
moex-assistant/
├── api/            # FastAPI + веб-интерфейс
├── bot/            # Telegram/VK боты
├── brokers/        # Тинькофф Invest API адаптер
├── core/           # MOEX API, SQLite, LLM, конфиг
├── mobile/         # HTML фронтенд
├── scripts/        # Вспомогательные скрипты
├── strategies/     # Индикаторы, риск, комиссии
├── tests/          # Тесты
├── main.py         # Точка входа
├── README.md       # Общий обзор
├── GUIDE.md        # Этот файл
├── DEPLOY.md       # Деплой на сервер
└── docs/           # Архитектура, анализ, чеклисты
```

---

## Частые проблемы

| Проблема | Решение |
|---|---|
| `ModuleNotFoundError: fastapi` | `pip install -e ".[dev]"` |
| LLM не отвечает | Проверь `LLM_PROVIDER` и ключи; для Ollama убедись, что `ollama serve` запущен. |
| Порт 8080 занят | Укажи другой `HEALTH_PORT` в `.env`. |
| Нет real-time данных | MOEX ISS API может отставать вечером/ночью. |

---

## Полезные ссылки

- [`README.md`](README.md) — общий обзор
- [`DEPLOY.md`](DEPLOY.md) — деплой на сервер
- [`docs/project_assessment_2026-07-26.md`](docs/project_assessment_2026-07-26.md) — внешняя оценка и план
