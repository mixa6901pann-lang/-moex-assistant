# Чеклист защиты MOEX Assistant (21 июня 2026)

Напоминание: 20 июня аудит нашёл реальные ключи VK/OpenRouter в `.env`/`.env.server` и открытые API/веб-панель. Этот чеклист — план доведения защиты до конца.

## Сделано сегодня в коде

- [x] API-key защита write-эндпоинтов (`/api/run_screener`, `/api/positions/add`, `/api/ticker/{ticker}/sentiment`) — `api/mobile_api.py`.
- [x] CodeAct теперь выключен по умолчанию (`CODEACT_ENABLED=false`). На сервере оставлять `false`.
- [x] `.env.server` приведён к безопасному шаблону: без Ollama, без реальных ключей, с плейсхолдерами `API_KEY` и `CODEACT_ENABLED`.
- [x] Приложение по умолчанию биндится на `127.0.0.1` (`HOST` в `core/config.py`, `main.py`), порт 8080 не светится наружу.
- [x] `setup-security.sh` и `DEPLOY.md` больше не предлагают открывать 8080 в интернет.
- [x] Добавлен тест `tests/test_api_security.py`.

## Остаётся сделать вручную (нельзя сделать из Claude Code)

### 1. Отозвать скомпрометированные ключи
- **VK:** <https://vk.com/dev> → настройки приложения → удалить/пересоздать токен.
- **OpenRouter:** <https://openrouter.ai/keys> → удалить старый ключ и создать новый.
- Если использовался **Anthropic / Gemini** ключ на сервере — тоже пересоздать, если есть подозрение, что он попал в `.env`.

### 2. Пересоздать `.env` на сервере
```bash
cd /root/moex-assistant
cp .env.server .env
chmod 600 .env
nano .env
```
Заполни:
```ini
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your_new_key
API_KEY=your_random_32+_chars
CODEACT_ENABLED=false
WEB_UI_ENABLED=false   # или true, но только вместе с API_KEY
```

### 3. Убедиться, что `.env` не попадёт в git
В `.gitignore` уже есть:
```gitignore
.env
.env.*
!.env.example
!.env.server
```
Это значит, что `.env` игнорируется, а `.env.server` — шаблон без секретов. Проверь перед коммитом:
```bash
git status --short
```
`.env` не должен появляться.

### 4. Закрыть порт 8080 от интернета
На сервере приложение слушает `127.0.0.1:8080` по умолчанию. Не добавляй `ufw allow 8080`. Для удалённого доступа используй один из вариантов:

#### Вариант A: Cloudflare Tunnel (проще, рекомендуем)
Запусти готовый скрипт:
```bash
cd /root/moex-assistant
bash scripts/setup-cloudflare-tunnel.sh
```
Скрипт сам установит `cloudflared`, создаст туннель и запустит службу.
После авторизации (`cloudflared tunnel login`) и настройки Access по почте — приложение доступно по `https://moex-assistant.pages.dev`.

Детали вручную:
1. Установи `cloudflared`:
   ```bash
   wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /usr/local/bin/cloudflared
   chmod +x /usr/local/bin/cloudflared
   ```
2. Авторизуй:
   ```bash
   cloudflared tunnel login
   ```
3. Создай туннель:
   ```bash
   cloudflared tunnel create moex-assistant
   cloudflared tunnel route dns moex-assistant moex-assistant.pages.dev
   cloudflared service install
   systemctl enable cloudflared
   systemctl start cloudflared
   ```

#### Вариант B: nginx + HTTPS + HTTP Basic Auth
```bash
apt-get install nginx certbot python3-certbot-nginx
```
Конфиг `/etc/nginx/sites-available/moex`:
```nginx
server {
    listen 443 ssl;
    server_name moex.example.com;

    ssl_certificate /etc/letsencrypt/live/moex.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/moex.example.com/privkey.pem;

    auth_basic "MOEX Assistant";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```
Создай пользователя:
```bash
apt-get install apache2-utils
htpasswd -c /etc/nginx/.htpasswd moex
systemctl restart nginx
```

### 5. Закрыть SSH паролем (если ещё не)
```bash
ssh-copy-id root@201.51.1.244   # с локального ПК
# на сервере:
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl restart sshd
```

### 6. Проверить защиту API
С сервера:
```bash
curl http://127.0.0.1:8080/api/health
```
Без ключа write-эндпоинт должен отказать:
```bash
curl -X POST http://127.0.0.1:8080/api/run_screener
# -> 401 или 503
```
С ключом:
```bash
curl -X POST -H "x-api-key: YOUR_API_KEY" http://127.0.0.1:8080/api/run_screener
```

### 7. Перезаписать историю git (опционально, но рекомендуется)
Если раньше в `.env` или `.env.server` действительно были реальные ключи, лучше вычистить их из всей истории. Это **деструктивная операция** — делай только если уверен.

С помощью `git-filter-repo`:
```bash
pip install git-filter-repo
git filter-repo --path Учебный/moex-assistant/.env --path Учебный/moex-assistant/.env.server --invert-paths
```
После этого придётся force-push. **Все клонированные копии репозитория станут несовместимы.**

## Повторять регулярно

- Раз в месяц проверять `git grep -E '(sk-or-|sk-ant-|vk1\.|AIza)'` на случай случайных утечек.
- Раз в квартал ротировать API-ключи.
- Следить за `logs/moex_YYYY-MM-DD.log` на необычные запросы к `/api/*`.
