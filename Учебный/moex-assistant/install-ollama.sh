#!/bin/bash
# Install Ollama + Gemma 3:4b on Ubuntu server
# Run as root: bash install-ollama.sh

set -e

echo "=== Ollama + Gemma 3:4b Installer ==="

# Check RAM
RAM_GB=$(free -g | awk '/^Mem:/{print $2}')
echo "Detected RAM: ${RAM_GB}GB"

if [ "$RAM_GB" -lt 4 ]; then
    echo "ERROR: Gemma 3:4b needs at least 4 GB RAM. You have ${RAM_GB}GB."
    exit 1
fi

echo "[1/5] Installing Ollama..."
if ! command -v ollama &> /dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
else
    echo "Ollama already installed, skipping..."
fi

echo "[2/5] Enabling Ollama service..."
systemctl enable ollama || true
systemctl start ollama || true

# Wait for the API to become ready before pulling.
for i in {1..30}; do
    if curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
        break
    fi
    echo "Waiting for Ollama to start... ($i)"
    sleep 1
done

echo "[3/5] Pulling Gemma 3:4b model..."
ollama pull gemma3:4b

echo "[4/5] Verifying model..."
ollama list | grep gemma3:4b || { echo "Model not found!"; exit 1; }

echo "[5/5] Testing model..."
ollama run gemma3:4b "Say 'Ollama is ready' in Russian" || true

echo "=== Installation complete ==="
echo "Model: gemma3:4b (~3 GB RAM)"
echo "Service: systemctl status ollama"
echo "Test API: curl http://localhost:11434/api/tags"
echo "Set in .env: OLLAMA_MODEL=gemma3:4b"
echo "Restart MOEX: supervisorctl restart moex"
