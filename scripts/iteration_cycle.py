#!/usr/bin/env python3
"""Automated strategy iteration cycle.

Runs as a standalone script to:
1. Collect current performance data
2. Analyze trends and issues
3. Generate optimization recommendations
4. Write reports for team review

Usage: python3 scripts/iteration_cycle.py [--once | --loop MINUTES]
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "auto_bit.db"
REPORTS_DIR = PROJECT_ROOT / "docs" / "iterations"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

KST = timezone(timedelta(hours=9))


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def collect_metrics():
    """Collect all current trading metrics."""
    conn = get_db()

    trades = conn.execute("SELECT * FROM trades WHERE mode='paper' ORDER BY id").fetchall()
    positions = conn.execute("SELECT * FROM positions WHERE mode='paper'").fetchall()
    grids = conn.execute("SELECT * FROM grid_state WHERE status='active'").fetchall()
    levels = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM grid_levels GROUP BY status"
    ).fetchall()

    val = conn.execute(
        "SELECT value FROM system_state WHERE key='current_balance_paper'"
    ).fetchone()
    balance = float(val["value"]) if val else 0

    pos_margin = sum(float(p["margin"] or 0) for p in positions)
    total_pnl = sum(float(t["pnl"] or 0) for t in trades)
    total_fee = sum(float(t["fee"] or 0) for t in trades)
    equity = balance + pos_margin

    wins = sum(1 for t in trades if float(t["pnl"] or 0) > 0)
    losses = sum(1 for t in trades if float(t["pnl"] or 0) <= 0)

    # Per-symbol breakdown
    symbol_stats = {}
    for t in trades:
        sym = t["symbol"]
        if sym not in symbol_stats:
            symbol_stats[sym] = {"trades": 0, "pnl": 0, "wins": 0, "fees": 0}
        symbol_stats[sym]["trades"] += 1
        symbol_stats[sym]["pnl"] += float(t["pnl"] or 0)
        symbol_stats[sym]["fees"] += float(t["fee"] or 0)
        if float(t["pnl"] or 0) > 0:
            symbol_stats[sym]["wins"] += 1

    # Grid info
    grid_info = []
    for g in grids:
        sp_pct = float(g["grid_spacing"]) / float(g["center_price"]) * 100 if float(g["center_price"]) > 0 else 0
        grid_info.append({
            "symbol": g["symbol"],
            "center": float(g["center_price"]),
            "spacing_pct": round(sp_pct, 3),
            "buy_levels": g["num_buy_levels"],
            "sell_levels": g["num_sell_levels"],
            "bias": g["bias"],
        })

    level_status = {r["status"]: r["cnt"] for r in levels}

    conn.close()

    return {
        "timestamp": datetime.now(KST).isoformat(),
        "equity": round(equity, 4),
        "balance": round(balance, 4),
        "margin_locked": round(pos_margin, 4),
        "total_pnl": round(total_pnl, 6),
        "total_fee": round(total_fee, 6),
        "net_pnl": round(total_pnl, 6),
        "trade_count": len(trades),
        "open_positions": len(positions),
        "win_count": wins,
        "loss_count": losses,
        "win_rate": round(wins / max(len(trades), 1) * 100, 1),
        "profit_factor": round(
            sum(float(t["pnl"]) for t in trades if float(t["pnl"]) > 0) /
            max(abs(sum(float(t["pnl"]) for t in trades if float(t["pnl"]) <= 0)), 0.0001),
            2
        ),
        "active_grids": len(grids),
        "grid_info": grid_info,
        "level_status": level_status,
        "symbol_stats": symbol_stats,
        "margin_match": abs(equity - (20 + total_pnl)) < 0.5,
    }


def analyze_and_recommend(metrics):
    """Generate actionable recommendations based on metrics."""
    recs = []

    # 1. Check profitability
    if metrics["trade_count"] > 0:
        avg_pnl = metrics["total_pnl"] / metrics["trade_count"]
        avg_fee = metrics["total_fee"] / metrics["trade_count"]
        if avg_pnl < avg_fee * 1.5:
            recs.append({
                "priority": "HIGH",
                "area": "spacing",
                "issue": f"Avg PnL ({avg_pnl:.6f}) barely covers avg fee ({avg_fee:.6f})",
                "action": "Consider increasing min_spacing_pct",
            })

    # 2. Check win rate
    if metrics["trade_count"] >= 5 and metrics["win_rate"] < 60:
        recs.append({
            "priority": "HIGH",
            "area": "entry",
            "issue": f"Win rate {metrics['win_rate']}% is below 60% target",
            "action": "Review grid placement and bias effectiveness",
        })

    # 3. Check margin health
    if not metrics["margin_match"]:
        recs.append({
            "priority": "CRITICAL",
            "area": "margin",
            "issue": "Margin accounting mismatch detected",
            "action": "Investigate paper executor balance tracking",
        })

    # 4. Check grid utilization
    pending = metrics["level_status"].get("PENDING", 0)
    filled = metrics["level_status"].get("FILLED", 0) + metrics["level_status"].get("TP_SET", 0)
    total_levels = pending + filled
    if total_levels > 0 and filled / total_levels < 0.05:
        recs.append({
            "priority": "MEDIUM",
            "area": "grid_range",
            "issue": f"Only {filled}/{total_levels} levels active ({filled/total_levels*100:.1f}%)",
            "action": "Grid range may be too wide or spacing too narrow",
        })

    # 5. Per-symbol performance
    for sym, stats in metrics["symbol_stats"].items():
        if stats["trades"] >= 3 and stats["pnl"] < 0:
            recs.append({
                "priority": "MEDIUM",
                "area": "symbol_selection",
                "issue": f"{sym} is unprofitable ({stats['pnl']:+.4f} over {stats['trades']} trades)",
                "action": f"Consider removing {sym} from grid pool",
            })

    return recs


def write_report(metrics, recommendations, cycle_num):
    """Write iteration report."""
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M")
    report_path = REPORTS_DIR / f"cycle_{cycle_num:03d}_{ts}.md"

    lines = [
        f"# Iteration Cycle {cycle_num}",
        f"**Time:** {metrics['timestamp']}",
        "",
        "## Metrics",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Equity | {metrics['equity']:.4f} USDT |",
        f"| PnL | {metrics['net_pnl']:+.6f} |",
        f"| Trades | {metrics['trade_count']} |",
        f"| Win Rate | {metrics['win_rate']}% |",
        f"| Profit Factor | {metrics['profit_factor']} |",
        f"| Open Positions | {metrics['open_positions']} |",
        f"| Active Grids | {metrics['active_grids']} |",
        f"| Margin Match | {'OK' if metrics['margin_match'] else 'MISMATCH'} |",
        "",
        "## Active Grids",
    ]

    for g in metrics["grid_info"]:
        lines.append(
            f"- {g['symbol']}: spacing={g['spacing_pct']}% "
            f"levels=B{g['buy_levels']}:S{g['sell_levels']} bias={g['bias']}"
        )

    if metrics["symbol_stats"]:
        lines.extend(["", "## Symbol Performance"])
        for sym, s in sorted(metrics["symbol_stats"].items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr = s["wins"] / max(s["trades"], 1) * 100
            lines.append(f"- {sym}: {s['trades']} trades, pnl={s['pnl']:+.4f}, WR={wr:.0f}%")

    if recommendations:
        lines.extend(["", "## Recommendations"])
        for r in recommendations:
            lines.append(f"- **[{r['priority']}]** {r['area']}: {r['issue']}")
            lines.append(f"  - Action: {r['action']}")

    lines.append("")
    report_path.write_text("\n".join(lines))
    print(f"Report written: {report_path}")
    return str(report_path)


def get_cycle_num():
    """Get next cycle number from existing reports."""
    existing = list(REPORTS_DIR.glob("cycle_*.md"))
    if not existing:
        return 1
    nums = []
    for f in existing:
        try:
            nums.append(int(f.stem.split("_")[1]))
        except (IndexError, ValueError):
            pass
    return max(nums, default=0) + 1


def run_once():
    """Run a single iteration cycle."""
    cycle_num = get_cycle_num()
    print(f"\n{'='*50}")
    print(f"Iteration Cycle {cycle_num} — {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}")
    print(f"{'='*50}")

    metrics = collect_metrics()
    print(f"Equity: {metrics['equity']:.4f} | PnL: {metrics['net_pnl']:+.6f} | "
          f"Trades: {metrics['trade_count']} | WR: {metrics['win_rate']}% | "
          f"Pos: {metrics['open_positions']} | Grids: {metrics['active_grids']}")

    recs = analyze_and_recommend(metrics)
    if recs:
        print(f"Recommendations ({len(recs)}):")
        for r in recs:
            print(f"  [{r['priority']}] {r['area']}: {r['issue']}")

    report = write_report(metrics, recs, cycle_num)

    # Save metrics as JSON for programmatic access
    metrics_path = REPORTS_DIR / f"metrics_{cycle_num:03d}.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2, default=str)

    return metrics, recs


def main():
    if "--once" in sys.argv:
        run_once()
    elif "--loop" in sys.argv:
        idx = sys.argv.index("--loop")
        interval = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 30
        print(f"Starting iteration loop (every {interval} minutes)")
        print(f"Deadline: 2026-03-29 16:00 KST")

        deadline = datetime(2026, 3, 29, 16, 0, tzinfo=KST)
        while datetime.now(KST) < deadline:
            try:
                run_once()
            except Exception as e:
                print(f"ERROR in cycle: {e}")

            remaining = (deadline - datetime.now(KST)).total_seconds() / 3600
            print(f"\nNext cycle in {interval} min. Deadline in {remaining:.1f} hours.")
            time.sleep(interval * 60)

        print("Deadline reached. Final cycle:")
        run_once()
    else:
        print("Usage: python3 scripts/iteration_cycle.py [--once | --loop MINUTES]")


if __name__ == "__main__":
    main()
