#!/bin/bash
# auto_bit - Check system status
# Usage: ./status.sh

cd "$(dirname "$0")"

PID_FILE="data/auto_bit.pid"
PORT=$(python3 -c "import yaml; print(yaml.safe_load(open('config/app.yaml'))['gui']['port'])" 2>/dev/null || echo "8080")

echo "============================================"
echo "  auto_bit System Status"
echo "============================================"

# Check main process
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        UPTIME=$(ps -o etime= -p "$PID" 2>/dev/null | xargs)
        echo "  Main Process: RUNNING (PID=$PID, uptime=$UPTIME)"
    else
        echo "  Main Process: DEAD (stale PID=$PID)"
    fi
else
    PID=$(pgrep -f "python3 -m src.main" 2>/dev/null | head -1 || true)
    if [ -n "$PID" ]; then
        echo "  Main Process: RUNNING (PID=$PID, no PID file)"
    else
        echo "  Main Process: STOPPED"
        echo "============================================"
        exit 0
    fi
fi

# Check child processes
echo ""
echo "  Child Processes:"
for name in collector strategy order gui; do
    PIDS=$(pgrep -f "src\.$name" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        COUNT=$(echo "$PIDS" | wc -l)
        echo "    $name: $COUNT process(es)"
    else
        echo "    $name: not running"
    fi
done

# Check GUI
echo ""
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    echo "  GUI: http://localhost:$PORT (OK)"
else
    echo "  GUI: http://localhost:$PORT (not responding)"
fi

# Check API status
echo ""
API=$(curl -s "http://localhost:$PORT/api/status" 2>/dev/null)
if [ -n "$API" ]; then
    python3 -c "
import json, sys
d = json.loads('''$API''')
mode = d.get('mode', '?')
active = d.get('trading_active', False)
procs = d.get('processes', {})
print(f'  Mode: {mode.upper()}')
print(f'  Trading: {\"ACTIVE\" if active else \"INACTIVE\"}')" 2>/dev/null

    # Show summary
    SUMMARY=$(curl -s "http://localhost:$PORT/api/summary" 2>/dev/null)
    if [ -n "$SUMMARY" ]; then
        python3 -c "
import json
d = json.loads('''$SUMMARY''')
print(f'  Balance: {d.get(\"current_balance\", 0):.2f} USDT')
print(f'  P&L: {d.get(\"total_pnl\", 0):.4f} USDT ({d.get(\"total_pnl_pct\", 0):.2f}%)')
print(f'  Today: {d.get(\"today_trades\", 0)} trades, P&L={d.get(\"today_pnl\", 0):.4f}')" 2>/dev/null
    fi

    # Show positions
    POSITIONS=$(curl -s "http://localhost:$PORT/api/positions" 2>/dev/null)
    if [ -n "$POSITIONS" ]; then
        python3 -c "
import json
data = json.loads('''$POSITIONS''')
if data:
    print(f'  Positions: {len(data)} open')
    for p in data:
        side = 'LONG' if p['side'] == 'Buy' else 'SHORT'
        print(f'    {p[\"symbol\"]} {side} pnl={p.get(\"unrealized_pnl\",0):.4f} ({p.get(\"unrealized_pnl_pct\",0):.1f}%)')
else:
    print('  Positions: none')" 2>/dev/null
    fi

    # Tuner status
    TUNER=$(curl -s "http://localhost:$PORT/api/tuner" 2>/dev/null)
    if [ -n "$TUNER" ]; then
        python3 -c "
import json
d = json.loads('''$TUNER''')
print(f'  Tuner: L{d.get(\"level\",0)} rate={d.get(\"signal_rate\",0)*100:.1f}% streak={d.get(\"stable_streak\",0)}')" 2>/dev/null
    fi
fi

echo ""
echo "============================================"
echo "  Logs: tail -f logs/auto_bit.log"
echo "============================================"
