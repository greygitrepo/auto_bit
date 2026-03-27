#!/bin/bash
# auto_bit - Start trading system
# Usage: ./start.sh [paper|live] [--headless]

set -e
cd "$(dirname "$0")"

MODE="${1:-paper}"
HEADLESS=""
PID_FILE="data/auto_bit.pid"
LOG_FILE="logs/auto_bit.log"

# Parse args
for arg in "$@"; do
    case "$arg" in
        --headless) HEADLESS="--headless" ;;
        paper|live) MODE="$arg" ;;
    esac
done

# Check if already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[!] auto_bit is already running (PID=$OLD_PID)"
        echo "    Use ./stop.sh to stop it first."
        exit 1
    else
        echo "[*] Stale PID file found, cleaning up..."
        rm -f "$PID_FILE"
    fi
fi

# Ensure directories exist
mkdir -p logs data

# Safety check for live mode
if [ "$MODE" = "live" ]; then
    echo "============================================"
    echo "  WARNING: Starting in LIVE trading mode!"
    echo "  Real money will be used for trades."
    echo "============================================"
    read -p "Are you sure? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Aborted."
        exit 0
    fi
fi

echo "[*] Starting auto_bit (mode=$MODE)..."
nohup python3 -m src.main --mode "$MODE" $HEADLESS \
    >> "$LOG_FILE" 2>&1 &

PID=$!
echo "$PID" > "$PID_FILE"

# Wait briefly and verify it started
sleep 3
if kill -0 "$PID" 2>/dev/null; then
    echo "[OK] auto_bit started (PID=$PID, mode=$MODE)"
    if [ -z "$HEADLESS" ]; then
        PORT=$(python3 -c "import yaml; print(yaml.safe_load(open('config/app.yaml'))['gui']['port'])" 2>/dev/null || echo "8080")
        echo "[OK] GUI: http://localhost:$PORT"
    fi
    echo "[OK] Log: tail -f $LOG_FILE"
else
    echo "[ERROR] auto_bit failed to start. Check $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
