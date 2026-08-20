#!/bin/bash
# Установка всего необходимого на чистый сервер Ubuntu 22.04

set -e

PROJECT_DIR="/root/moex-assistant"

echo "=== Обновление системы ==="
apt update && apt upgrade -y

echo "=== Установка Python 3.11, pip, git ==="
apt install -y python3.11 python3.11-venv python3.11-pip git curl

echo "=== Создание виртуального окружения ==="
cd "$PROJECT_DIR"
python3.11 -m venv venv

echo "=== Установка зависимостей ==="
. venv/bin/activate
pip install --upgrade pip
pip install -e .

echo "=== Установка supervisor ==="
apt install -y supervisor
systemctl enable supervisor
systemctl start supervisor

echo "=== Копирование конфига supervisor ==="
cp "$PROJECT_DIR/scripts/supervisor-moex.conf" /etc/supervisor/conf.d/moex.conf

echo "=== Создание папок для данных и логов ==="
mkdir -p "$PROJECT_DIR/logs"
mkdir -p "$PROJECT_DIR/data"

echo "=== Открытие порта health endpoint ==="
ufw allow 8080/tcp || true

echo "=== Готово! ==="
echo ""
echo "Дальше:"
echo "  1. Создай .env:  nano $PROJECT_DIR/.env"
echo "  2. Запусти бота:  supervisorctl reread && supervisorctl update && supervisorctl start moex"
echo "  3. Проверь:       supervisorctl status moex"
echo "  4. Логи:          tail -f $PROJECT_DIR/logs/moex_\$(date +%Y-%m-%d).log"
echo "  5. Health:        curl http://localhost:8080/health"
