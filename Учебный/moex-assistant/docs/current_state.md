# MOEX Assistant — текущее состояние системы

**Дата:** 26 июля 2026
**Домен:** https://invest-assistant.ru
**Сервер:** Timeweb Cloud VPS, IP 201.51.1.244, порт SSH 2222
**Управление:** `supervisorctl` (сервис `moex`)

> Этот документ — operational snapshot. Для общего обзора смотри [`README.md`](../README.md), для пользователя — [`GUIDE.md`](../GUIDE.md), для деплоя — [`DEPLOY.md`](../DEPLOY.md).

---

## 1. Общее назначение

MOEX Assistant — торговый ассистент для российского рынка акций (Московская биржа):
- собирает рыночные данные (свечи, индексы);
- сканирует RSS-новости по тикерам;
- анализирует сентимент через LLM (Yandex / Ollama / Anthropic / Gemini);
- формирует технические и LLM-рекомендации;
- ведёт дневник прогнозов и проверяет точность через 1/3/7 дней;
- ведёт бумажную торговлю и работает с Тинькофф (sandbox/semi-auto/live).

---

## 2. Архитектура развёртывания

```
Пользователь
    ↓ HTTPS
invest-assistant.ru (nginx + HTTP Basic Auth)
    ↓ proxy_pass
127.0.0.1:8080 (MOEX Assistant FastAPI)
    ↓
SQLite data/moex.db
```

Компоненты:
- **nginx** — HTTPS, HTTP Basic Auth, проксирование на `127.0.0.1:8080`.
- **MOEX Assistant (`main.py`)** — FastAPI + фоновый планировщик задач.
- **supervisor** (`moex`) — автозапуск и перезапуск.
- **SQLite (`data/moex.db`)** — прогнозы, сделки, сентимент, цены, предложения робота.

---

## 3. Домен и HTTPS

- Домен: `invest-assistant.ru`
- A-запись: `@` → `201.51.1.244`
- SSL: Let’s Encrypt (certbot)
- HTTP → HTTPS redirect

---

## 4. Правила доступа

| Запрос | Требования |
|---|---|
| `GET /` и веб-UI | HTTP Basic Auth (nginx) |
| `GET /api/*` | без ключа (чтение) |
| `POST/PUT/PATCH/DELETE /api/*` | `x-api-key` |

---

## 5. Управление сервисом

```bash
supervisorctl status moex
supervisorctl restart moex
supervisorctl stop moex
supervisorctl tail moex stderr
```

Локальные логи: `/root/moex-assistant/Учебный/moex-assistant/logs/moex_YYYY-MM-DD.log`

---

## 6. LLM / Анализ новостей

- Провайдер на сервере: `yandex` (Ollama не используется на 1 GB RAM).
- Fallback: `yandex,ollama,anthropic,gemini`.
- Источники новостей: RBC, Finam, Interfax, Ведомости, Коммерсант, Investing.com, TradingView.
- Окно сбора: 4 часа.
- Вес новости с возрастом: 100% / 75% / 50% / 25%.

---

## 7. Фоновый планировщик (MSK)

| Время | Задача |
|---|---|
| 10:00 | Morning screener |
| */15 10–18:45 | Intraday monitor |
| */15 07–23:00 | RSS sentiment scan |
| */30 07–23:00 | Geo-risk scan |
| 18:00 | Evening trading decision |
| 19:00 | Evening report |
| 19:05 | Prediction accuracy check |
| 19:06 | Paper trading check |
| Суб 12:00 | Dividend update |

---

## 8. Торговля

- **Бумажная торговля:** виртуальный счёт `PAPER_STARTING_CAPITAL`.
- **Semi-auto:** робот создаёт предложения, исполнение по подтверждению в UI.
- **Live:** автоматические ордера через Тинькофф (требуется полный доступ).
- **Sandbox:** тестирование на виртуальном брокерском счёте Тинькофф.

---

## 9. Безопасность

- Приложение слушает `127.0.0.1:8080`.
- `WEB_UI_ENABLED=false` по умолчанию.
- `CODEACT_ENABLED=false`.
- Write-эндпоинты защищены `API_KEY`.
- nginx + HTTPS + Basic Auth перед UI.
- SSH порт 2222, рекомендуется только ключ.

Чеклист: [`2026-06-21—security-hardening-checklist.md`](2026-06-21—security-hardening-checklist.md).

---

## 10. Полезные ссылки

- UI: https://invest-assistant.ru
- Health: https://invest-assistant.ru/api/health
