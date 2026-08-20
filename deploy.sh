#!/bin/bash
# Deploy script for MOEX Assistant
# Run on server: bash deploy.sh

set -e

echo "=== MOEX Assistant Deploy ==="

PROJECT_DIR="/root/moex-assistant"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_NAME="moex"

cd "$PROJECT_DIR"

echo "[1/5] Pulling latest code..."
git pull origin master

echo "[2/5] Updating Python dependencies..."
source "$VENV_DIR/bin/activate"
pip install -r server-requirements.txt

echo "[3/5] Running database migrations (if any)..."
# Add migration commands here if needed

echo "[4/5] Restarting service..."
supervisorctl restart "$SERVICE_NAME"

echo "[5/5] Checking health..."
sleep 3
curl -s http://localhost:8080/health || echo "Health check failed"

echo "=== Deploy complete ==="
echo "Check logs: supervisorctl tail -f $SERVICE_NAME"
