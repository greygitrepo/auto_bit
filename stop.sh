#!/bin/bash
# auto_bit - Stop trading system gracefully
# Usage: ./stop.sh [--force]

set -e
cd "$(dirname "$0")"

PID_FILE="data/auto_bit.pid"
FORCE=false

if [ "$1" = "--force" ]; then
    FORCE=true
fi

# Find PID
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
else
    # Try to find by process name
    PID=$(pgrep -f "python3 -m src.main" 2>/dev/null | head -1 || true)
    if [ -z "$PID" ]; then
        echo "[*] auto_bit is not running."
        exit 0
    fi
fi

if ! kill -0 "$PID" 2>/dev/null; then
    echo "[*] auto_bit is not running (PID=$PID already exited)."
    rm -f "$PID_FILE"
    exit 0
fi

echo "[*] Stopping auto_bit (PID=$PID)..."

if [ "$FORCE" = true ]; then
    echo "[*] Force stopping..."
    kill -9 "$PID" 2>/dev/null || true
    # Also kill child processes
    pkill -9 -P "$PID" 2>/dev/null || true
    pkill -9 -f "src\.(collector|strategy|order|gui)" 2>/dev/null || true
else
    # Graceful: send SIGTERM, wait up to 20 seconds
    kill -TERM "$PID" 2>/dev/null || true
    echo -n "[*] Waiting for graceful shutdown"

    for i in $(seq 1 20); do
        if ! kill -0 "$PID" 2>/dev/null; then
            echo ""
            echo "[OK] auto_bit stopped gracefully."
            rm -f "$PID_FILE"
            exit 0
        fi
        echo -n "."
        sleep 1
    done

    echo ""
    echo "[!] Graceful shutdown timed out, force killing..."
    kill -9 "$PID" 2>/dev/null || true
    pkill -9 -P "$PID" 2>/dev/null || true
    pkill -9 -f "src\.(collector|strategy|order|gui)" 2>/dev/null || true
fi

# Cleanup
sleep 1
rm -f "$PID_FILE"

# Verify all stopped (including any orphan src.main processes)
REMAINING=$(pgrep -f "python3 -m src\." 2>/dev/null || true)
if [ -n "$REMAINING" ]; then
    echo "[!] Orphan processes found, killing..."
    echo "$REMAINING" | xargs kill -9 2>/dev/null || true
    sleep 1
fi
REMAINING2=$(pgrep -f "src\.(collector|strategy|order|gui)" 2>/dev/null || true)
if [ -n "$REMAINING2" ]; then
    echo "$REMAINING2" | xargs kill -9 2>/dev/null || true
fi

echo "[OK] auto_bit stopped."
