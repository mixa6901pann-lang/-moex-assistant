#!/bin/bash
# Install Gemma 3:12b on server with 8+ GB RAM
# Run as root: bash install-gemma12b.sh

set -e

echo "=== Gemma 3:12b Installer ==="

# Check RAM
RAM_GB=$(free -g | awk '/^Mem:/{print $2}')
echo "Detected RAM: ${RAM_GB}GB"

if [ "$RAM_GB" -lt 8 ]; then
    echo "ERROR: Gemma 3:12b needs at least 8 GB RAM. You have ${RAM_GB}GB."
    echo "Please upgrade your server first:"
    echo "  1. Go to Timeweb Cloud panel"
    echo "  2. Find your server → 'Change configuration'"
    echo "  3. Select a plan with 16 GB RAM or more"
    echo "  4. Reboot server after upgrade"
    exit 1
fi

if [ "$RAM_GB" -lt 16 ]; then
    echo "WARNING: Gemma 3:12b uses ~7.5 GB RAM. On ${RAM_GB} GB it will run very tight"
    echo "when MOEX Assistant and the OS are also active. Expect swap, slow responses, and possible OOM."
    echo "Recommended: 16 GB RAM for production use."
    echo "Continue anyway? (y/n)"
    read -r CONFIRM
    if [ "$CONFIRM" != "y" ]; then
        exit 1
    fi
fi

echo "[1/4] Checking Ollama..."
if ! command -v ollama &> /dev/null; then
    echo "Ollama not found. Installing..."
    curl -fsSL https://ollama.com/install.sh | sh
fi

echo "[2/4] Enabling Ollama service..."
systemctl enable ollama || true
systemctl start ollama || true

echo "[3/4] Stopping current Ollama models to free RAM..."
ollama stop gemma3:4b 2>/dev/null || true
sleep 2

echo "[4/4] Pulling Gemma 3:12b (~7.5 GB, may take 10-20 min)..."
ollama pull gemma3:12b

echo "=== Installation complete ==="
echo "Model size: ~7.5 GB in Q4_K_M quantization"
echo "Required RAM: 16+ GB for production (you have ${RAM_GB}GB)"
echo ""
echo "Test: ollama run gemma3:12b 'Привет'"
echo "Set in .env: OLLAMA_MODEL=gemma3:12b"
echo "Restart MOEX: supervisorctl restart moex"
