#!/bin/bash
# setup-cloudflare-tunnel.sh — Cloudflare Tunnel для MOEX Assistant
# Запускай на сервере: bash scripts/setup-cloudflare-tunnel.sh
#
# Этот скрипт:
#   1. Проверяет, что приложение слушает только 127.0.0.1 (порт 8080 закрыт из интернета)
#   2. Устанавливает cloudflared
#   3. Создаёт туннель и конфиг
#   4. Регистрирует адрес moex-assistant.pages.dev
#   5. Устанавливает туннель как службу
#
# После запуска скрипта:
#   - Выполни: cloudflared tunnel login
#   - Открой ссылку в браузере и залогинься в Cloudflare
#   - Затем снова запусти этот скрипт (или выполни шаги 2-5 вручную)
#   - Настрой пароль по почте в Cloudflare Zero Trust → Access → Applications

set -e

PROJECT_DIR="/root/moex-assistant"
APP_LINK="/root/moex-app"
CLOUDFLARED_BIN="/usr/local/bin/cloudflared"
CONFIG_DIR="/root/.cloudflared"
TUNNEL_NAME="moex-assistant"
HOSTNAME="moex-assistant.pages.dev"

echo "=============================================="
echo " Cloudflare Tunnel setup for MOEX Assistant"
echo "=============================================="
echo ""

# Если проект лежит по симлинку /root/moex-app, используем его
if [ -L "$APP_LINK" ] && [ -d "$APP_LINK" ]; then
    PROJECT_DIR="$APP_LINK"
fi

# ── 0. Проверяем .env ─────────────────────────────────────────
echo "[0/6] Checking $PROJECT_DIR/.env..."
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "ERROR: .env not found at $PROJECT_DIR/.env"
    echo "Copy .env.server to .env and fill API_KEY:"
    echo "  cp $PROJECT_DIR/.env.server $PROJECT_DIR/.env"
    echo "  nano $PROJECT_DIR/.env"
    exit 1
fi

if ! grep -qE '^HOST=127\.0\.0\.1' "$PROJECT_DIR/.env"; then
    echo "WARNING: HOST is not 127.0.0.1 in .env"
    echo "Fixing... adding HOST=127.0.0.1"
    echo "HOST=127.0.0.1" >> "$PROJECT_DIR/.env"
fi

if ! grep -qE '^API_KEY=' "$PROJECT_DIR/.env" || grep -qE '^API_KEY=your_random_api_key_here' "$PROJECT_DIR/.env"; then
    echo "WARNING: API_KEY is not set or is a placeholder"
    echo "Generate a key and add it to .env:"
    echo "  python -c \"import secrets; print(secrets.token_urlsafe(32))\""
    echo "Then add: API_KEY=<generated-key>"
    exit 1
fi

if ! grep -qE '^WEB_UI_ENABLED=true' "$PROJECT_DIR/.env"; then
    echo "WARNING: WEB_UI_ENABLED is not true in .env"
    echo "Add: WEB_UI_ENABLED=true"
    exit 1
fi

echo "OK: .env looks safe (HOST=127.0.0.1, API_KEY set, WEB_UI_ENABLED=true)"
echo ""

# ── 1. Установка cloudflared ──────────────────────────────────
echo "[1/6] Installing cloudflared..."
if command -v cloudflared &>/dev/null; then
    echo "cloudflared already installed: $(cloudflared --version | head -1)"
else
    echo "Downloading cloudflared..."
    wget -q "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" -O "$CLOUDFLARED_BIN"
    chmod +x "$CLOUDFLARED_BIN"
    echo "Installed: $(cloudflared --version | head -1)"
fi
echo ""

# ── 2. Авторизация ────────────────────────────────────────────
echo "[2/6] Checking Cloudflare authorization..."
if [ ! -f "$CONFIG_DIR/cert.pem" ]; then
    echo "You need to authorize cloudflared first."
    echo "Run this command and open the link in your browser:"
    echo ""
    echo "  cloudflared tunnel login"
    echo ""
    echo "After logging in, run this script again."
    exit 0
fi
echo "OK: cloudflared is authorized"
echo ""

# ── 3. Создание туннеля ───────────────────────────────────────
echo "[3/6] Creating tunnel '$TUNNEL_NAME'..."
TUNNEL_FILE=$(ls "$CONFIG_DIR"/*.json 2>/dev/null | head -1)
if [ -z "$TUNNEL_FILE" ]; then
    cloudflared tunnel create "$TUNNEL_NAME"
    TUNNEL_FILE=$(ls "$CONFIG_DIR"/*.json 2>/dev/null | head -1)
fi

if [ -z "$TUNNEL_FILE" ]; then
    echo "ERROR: Tunnel credentials file not found"
    exit 1
fi

TUNNEL_ID=$(basename "$TUNNEL_FILE" .json)
echo "Tunnel ID: $TUNNEL_ID"
echo ""

# ── 4. Конфигурация ───────────────────────────────────────────
echo "[4/6] Writing config to $CONFIG_DIR/config.yml..."
mkdir -p "$CONFIG_DIR"
cat > "$CONFIG_DIR/config.yml" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CONFIG_DIR/$TUNNEL_ID.json

ingress:
  - hostname: $HOSTNAME
    service: http://127.0.0.1:8080
  - service: http_status:404
EOF
echo "OK"
echo ""

# ── 5. Регистрация DNS ────────────────────────────────────────
echo "[5/6] Registering DNS route $HOSTNAME..."
if cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME" 2>/dev/null; then
    echo "OK: $HOSTNAME -> 127.0.0.1:8080"
else
    echo "WARNING: DNS route may already exist. Continuing..."
fi
echo ""

# ── 6. Установка службы ───────────────────────────────────────
echo "[6/6] Installing cloudflared as a system service..."
cloudflared service install
systemctl enable cloudflared
systemctl restart cloudflared

sleep 2
if systemctl is-active --quiet cloudflared; then
    echo "OK: cloudflared service is running"
else
    echo "ERROR: cloudflared service failed to start"
    echo "Check logs: journalctl -u cloudflared -n 50"
    exit 1
fi

echo ""
echo "=============================================="
echo " Cloudflare Tunnel is ready!"
echo "=============================================="
echo ""
echo "Your secure URL: https://$HOSTNAME"
echo ""
echo "Next steps (important!):"
echo "1. Open https://dash.cloudflare.com"
echo "2. Go to Zero Trust → Access → Applications"
echo "3. Click 'Add an application' → 'Self-hosted'"
echo "4. Domain: $HOSTNAME"
echo "5. Policy: Action=Allow, Include=Emails, enter your email"
echo "6. Save"
echo ""
echo "Now only your email (and emails you add) can open the app."
echo ""
echo "Direct access to http://201.51.1.244:8080 should be blocked."
echo "Test: curl http://201.51.1.244:8080/health"
echo ""
