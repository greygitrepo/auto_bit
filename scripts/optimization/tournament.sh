#!/bin/bash
# ============================================================
# Parallel Optimization Tournament Runner — 10 Teams
# All 10 teams run SIMULTANEOUSLY in isolated environments
# Teams with 3+ warnings get strategy overhaul
#
# Usage: ./scripts/optimization/tournament.sh [cycle_minutes]
# Default cycle: 30 minutes (5분봉 최소 6개 수집 가능)
#
# Deadline: 2026-04-09 09:00 KST
# ============================================================

set -e
cd "$(dirname "$0")/../.."

CYCLE_MINUTES="${1:-30}"
# System timezone is KST
DEADLINE_KST="2026-04-09 18:00:00"
DEADLINE_TS=$(date -d "$DEADLINE_KST" +%s 2>/dev/null || date -j -f "%Y-%m-%d %H:%M:%S" "$DEADLINE_KST" +%s 2>/dev/null)
REPORT_RESERVE_MINUTES=20
RESULTS_DIR="scripts/optimization/results"

mkdir -p "$RESULTS_DIR" logs

echo "============================================"
echo "  PARALLEL OPTIMIZATION TOURNAMENT v2"
echo "============================================"
echo "  10 teams running SIMULTANEOUSLY"
echo "  Cycle Duration: ${CYCLE_MINUTES} min"
echo "  Deadline: $DEADLINE_KST KST"
echo "  Started: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  Warning system: 3 strikes → strategy overhaul"
echo "============================================"
echo ""

# Stop any running instance first
./stop.sh --force 2>/dev/null || true
pkill -f "src.main" 2>/dev/null || true
sleep 2

CYCLE=1
while true; do
    NOW_TS=$(date +%s)
    CUTOFF_TS=$((DEADLINE_TS - REPORT_RESERVE_MINUTES * 60))

    if [ "$NOW_TS" -ge "$CUTOFF_TS" ]; then
        echo ""
        echo "[*] Approaching deadline. Stopping tournament."
        break
    fi

    REMAINING_SEC=$((CUTOFF_TS - NOW_TS))

    if [ "$REMAINING_SEC" -lt "$((CYCLE_MINUTES * 60 + 120))" ]; then
        echo "[*] Not enough time for another cycle. Stopping."
        break
    fi

    echo ""
    echo "========== ROUND $CYCLE =========="
    echo "Time remaining: $((REMAINING_SEC / 60)) minutes"
    echo "All 10 teams start NOW (parallel, ${CYCLE_MINUTES}min)"
    echo ""

    RESET_FLAG=""
    if [ "$CYCLE" -eq 1 ]; then
        RESET_FLAG=""  # First cycle: fresh start
    else
        RESET_FLAG="--no-reset"  # Subsequent: accumulate data
    fi

    python3 scripts/optimization/run_parallel.py \
        --duration "$CYCLE_MINUTES" \
        --cycle "$CYCLE" \
        $RESET_FLAG \
        2>&1 | tee -a "logs/tournament_parallel.log"

    CYCLE=$((CYCLE + 1))
    echo ""
    echo "[*] Cooling down 10 seconds before next round..."
    sleep 10
done

echo ""
echo "============================================"
echo "  TOURNAMENT COMPLETE"
echo "  Generating final report..."
echo "============================================"

python3 scripts/optimization/report.py 2>&1

echo ""
echo "  Done. Report: $RESULTS_DIR/final_report.txt"
echo "============================================"
