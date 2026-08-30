> Для общего обзора проекта смотри [`README.md`](README.md).  
> Для руководства пользователя смотри [`GUIDE.md`](GUIDE.md).

# Деплой MOEX Assistant на сервер

## Важное ограничение сервера

- **1 ГБ RAM** — локальная Ollama не подходит. Используй облачный провайдер (`yandex`, `anthropic`, `gemini`).
- **4 ГБ RAM** — можно поднять Ollama с `gemma3:4b` (~3 GB RAM) для домашнего ПК/сервера.
- **8 ГБ RAM** (4 CPU / 80 GB NVMe) — `gemma3:4b` работает уверенно как fallback/critic. `gemma3:12b` возможен, но рискует уйти в swap/OOM; не рекомендуется для продакшена.

---

## 1. Требования

- Ubuntu 22.04 LTS
- Python 3.11+
- 1 GB RAM минимум (без Ollama)
- 4 GB RAM для Ollama `gemma3:4b`
- 8 GB RAM для Ollama `gemma3:12b` (не рекомендуется одновременно с активной торговлей)
- 15 GB диска (без моделей); 30–40 GB с локальными моделями

## 2. Подключиться к серверу

```bash
ssh root@IP_АДРЕС_СЕРВЕРА
```

Рекомендуется настроить вход по SSH-ключу и отключить парольный вход.

## 3. Загрузить проект

```bash
cd /root
git clone https://github.com/ТВОЙ_НИК/moex-assistant.git
cd moex-assistant
```

Из-за структуры репозитория исходники проекта окажутся в `/root/moex-assistant/Учебный/moex-assistant/`.
Создай симлинк `/root/moex-app`, чтобы supervisor и скрипты работали с единым путём:

```bash
ln -sfn /root/moex-assistant/Учебный/moex-assistant /root/moex-app
cd /root/moex-app
```

## 4. Установить зависимости

```bash
cd /root/moex-app
bash scripts/setup-server.sh
```

Скрипт установит Python 3.11, venv, зависимости из `pyproject.toml`, supervisor.

## 5. Настроить `.env`

```bash
cp /root/moex-app/.env.server /root/moex-app/.env
chmod 600 /root/moex-app/.env
nano /root/moex-app/.env
```

Обязательные переменные:

```env
# LLM — на сервере без Ollama
LLM_PROVIDER=yandex
YANDEX_API_KEY=...
YANDEX_FOLDER_ID=...

# Или anthropic / gemini
# LLM_PROVIDER=anthropic
# ANTHROPIC_API_KEY=...

# API key для защиты write-эндпоинтов
API_KEY=...

# CodeAct на сервере всегда false
CODEACT_ENABLED=false

# Web UI по умолчанию отключена
WEB_UI_ENABLED=false

# Торговля — песочница и/или paper
TINKOFF_SANDBOX=true
PAPER_TRADING=true
SEMI_AUTO_TRADING=true
AUTO_TRADING_ENABLED=false
```

## 6. (Опционально) Установить локальную Ollama на сервере 8 GB RAM

Если хочешь, чтобы `gemma3:4b` работала как fallback/critic (например, когда YandexGPT временно недоступен):

```bash
cd /root/moex-app
bash install-ollama.sh
```

Скрипт:
1. Установит Ollama и systemd-сервис.
2. Скачает `gemma3:4b` (~3 GB).
3. Запустит сервис.

Проверь:

```bash
systemctl status ollama
curl http://localhost:11434/api/tags
ollama run gemma3:4b 'Привет'
```

В `.env` убедись, что заданы:

```env
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=gemma3:4b
# Для ускорения на CPU можно уменьшить до 2048.
OLLAMA_NUM_CTX=4096
OLLAMA_TIMEOUT=180.0
```

> **Не ставь `gemma3:12b` на 8 GB сервер, если бот активно торгует.** Модель занимает ~7.5 GB RAM, и ОС + приложение могут упереться в лимит. Апгрейд до 16 GB RAM обязателен для 12b в продакшене.

## 7. Запустить через supervisor

```bash
cp /root/moex-app/scripts/supervisor-moex.conf /etc/supervisor/conf.d/moex.conf
supervisorctl reread
supervisorctl update
supervisorctl start moex
```

Проверка:

```bash
supervisorctl status moex
curl http://localhost:8080/health
```

Логи:

```bash
tail -f /root/moex-app/logs/moex_$(date +%Y-%m-%d).log
```

## 8. Удалённый доступ

**Никогда не открывай порт 8080 в интернет.** Приложение слушает `127.0.0.1:8080`.

Рекомендуемые варианты доступа:

### A. Cloudflare Tunnel (проще всего)

```bash
cd /root/moex-app
bash scripts/setup-cloudflare-tunnel.sh
```

### B. nginx + HTTPS + Basic Auth

Инструкция по смене пароля: [`docs/2026-07-20—nginx-basic-auth-password-change.md`](docs/2026-07-20—nginx-basic-auth-password-change.md).

---

## Перезапуск и обновление

```bash
supervisorctl restart moex
supervisorctl stop moex
supervisorctl start moex

# Обновление кода
cd /root/moex-app
git pull
. venv/bin/activate
pip install -e .
supervisorctl restart moex
```

---

## Проверка API

```bash
# Health
curl http://localhost:8080/health

# Sandbox orders (требуется API key)
curl -H "x-api-key: $API_KEY" http://localhost:8080/api/sandbox_orders

# Source stats
curl -H "x-api-key: $API_KEY" http://localhost:8080/api/proposals/source_stats
```

---

## Безопасность

- `.env` не должен попадать в git.
- CodeAct отключён на сервере.
- Web UI отключён по умолчанию.
- Порт 8080 закрыт наружу.

Подробный чеклист: [`docs/2026-06-21—security-hardening-checklist.md`](docs/2026-06-21—security-hardening-checklist.md).
