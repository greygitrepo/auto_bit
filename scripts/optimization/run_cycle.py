#!/usr/bin/env python3
"""Optimization Tournament Cycle Runner.

Applies a parameter profile from profiles.yaml to the system config,
runs paper trading for a specified duration, then collects results.

Usage:
    python3 scripts/optimization/run_cycle.py --team alpha --duration 30
    python3 scripts/optimization/run_cycle.py --team beta --duration 30
    python3 scripts/optimization/run_cycle.py --team gamma --duration 30

Duration is in minutes.
"""

import argparse
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
PROFILES_PATH = Path(__file__).resolve().parent / "profiles.yaml"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"


def load_profiles() -> dict:
    with open(PROFILES_PATH) as f:
        return yaml.safe_load(f)


def backup_configs() -> dict:
    """Backup current config files. Returns backup paths."""
    backups = {}
    for name in ["grid.yaml", "scanner.yaml", "asset.yaml"]:
        src = CONFIG_DIR / "strategy" / name
        dst = CONFIG_DIR / "strategy" / f"{name}.bak"
        if src.exists():
            shutil.copy2(src, dst)
            backups[name] = str(dst)
    return backups


def restore_configs():
    """Restore config files from backup."""
    for name in ["grid.yaml", "scanner.yaml", "asset.yaml"]:
        bak = CONFIG_DIR / "strategy" / f"{name}.bak"
        dst = CONFIG_DIR / "strategy" / name
        if bak.exists():
            shutil.copy2(bak, dst)
            bak.unlink()


def apply_profile(team_name: str, profile: dict):
    """Apply a team's profile to the config files."""
    # --- Grid config ---
    grid_path = CONFIG_DIR / "strategy" / "grid.yaml"
    with open(grid_path) as f:
        grid_cfg = yaml.safe_load(f)

    grid_profile = profile.get("grid", {})
    gb = grid_cfg["strategies"]["grid_bias"]

    # Apply top-level grid params
    for key in ["range_atr_multiplier", "min_range_pct", "max_range_pct",
                "recenter_interval_minutes", "recenter_threshold_pct",
                "leverage", "qty_per_level_pct", "max_open_levels",
                "max_symbols", "min_spacing_pct", "max_drawdown_pct"]:
        if key in grid_profile:
            gb[key] = grid_profile[key]

    # Apply nested sections
    for section in ["adaptive_levels", "dynamic_spacing", "bias", "mtf"]:
        if section in grid_profile:
            if section in gb:
                gb[section].update(grid_profile[section])
            else:
                gb[section] = grid_profile[section]

    # Apply exit config
    exit_profile = profile.get("exit", {})
    if exit_profile:
        grid_cfg["exit"].update(exit_profile)

    with open(grid_path, "w") as f:
        yaml.dump(grid_cfg, f, default_flow_style=False, allow_unicode=True)

    # --- Scanner config ---
    scanner_profile = profile.get("scanner", {})
    if scanner_profile:
        scanner_path = CONFIG_DIR / "strategy" / "scanner.yaml"
        with open(scanner_path) as f:
            scanner_cfg = yaml.safe_load(f)

        nl = scanner_cfg["strategies"]["new_listing"]
        if "max_days_since_listed" in scanner_profile:
            nl["listing"]["max_days_since_listed"] = scanner_profile["max_days_since_listed"]
        if "min_24h_turnover_usdt" in scanner_profile:
            nl["liquidity"]["min_24h_turnover_usdt"] = scanner_profile["min_24h_turnover_usdt"]
        if "max_candidates" in scanner_profile:
            nl["pool"]["max_candidates"] = scanner_profile["max_candidates"]
        if "min_score" in scanner_profile:
            nl["scoring"]["min_score"] = scanner_profile["min_score"]

        with open(scanner_path, "w") as f:
            yaml.dump(scanner_cfg, f, default_flow_style=False, allow_unicode=True)

    # --- Asset config ---
    asset_profile = profile.get("asset", {})
    if asset_profile:
        asset_path = CONFIG_DIR / "strategy" / "asset.yaml"
        with open(asset_path) as f:
            asset_cfg = yaml.safe_load(f)

        if "max_daily_loss_pct" in asset_profile:
            asset_cfg["daily_limits"]["max_daily_loss_pct"] = asset_profile["max_daily_loss_pct"]
        if "max_daily_trades" in asset_profile:
            asset_cfg["daily_limits"]["max_daily_trades"] = asset_profile["max_daily_trades"]
        if "consecutive_loss_cooldown_after" in asset_profile:
            asset_cfg["consecutive_loss"]["cooldown_after"] = asset_profile["consecutive_loss_cooldown_after"]
        if "consecutive_loss_stop_after" in asset_profile:
            asset_cfg["consecutive_loss"]["stop_after"] = asset_profile["consecutive_loss_stop_after"]

        with open(asset_path, "w") as f:
            yaml.dump(asset_cfg, f, default_flow_style=False, allow_unicode=True)

    print(f"[*] Applied profile: {team_name}")


def reset_paper_data():
    """Reset paper trading data for a clean test."""
    db_path = DATA_DIR / "auto_bit.db"
    if db_path.exists():
        # Keep the DB but clear paper data
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DELETE FROM positions WHERE mode = 'paper'")
            conn.execute("DELETE FROM trades WHERE mode = 'paper'")
            conn.execute("DELETE FROM daily_performance WHERE mode = 'paper'")
            conn.execute("DELETE FROM grid_states WHERE mode = 'paper'")
            conn.execute("DELETE FROM grid_levels WHERE grid_state_id IN (SELECT id FROM grid_states WHERE mode = 'paper')")
            conn.execute("DELETE FROM system_state WHERE key LIKE '%paper%'")
            conn.commit()
            print("[*] Paper trading data reset")
        except Exception as e:
            print(f"[!] DB reset warning: {e}")
        finally:
            conn.close()


def start_system() -> int:
    """Start the trading system. Returns PID."""
    proc = subprocess.Popen(
        ["python3", "-m", "src.main", "--mode", "paper", "--headless"],
        cwd=str(PROJECT_ROOT),
        stdout=open(str(LOGS_DIR / "optimization.log"), "a"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(5)  # Wait for startup
    if proc.poll() is not None:
        print("[ERROR] System failed to start")
        sys.exit(1)
    print(f"[OK] System started (PID={proc.pid})")
    return proc.pid


def stop_system(pid: int):
    """Stop the trading system gracefully."""
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(15):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except OSError:
                print(f"[OK] System stopped (PID={pid})")
                return
        # Force kill
        os.kill(pid, signal.SIGKILL)
        print(f"[!] System force-killed (PID={pid})")
    except OSError:
        print(f"[OK] System already stopped")


def collect_results(team_name: str, cycle_num: int, duration_min: int) -> dict:
    """Collect trading results from the database."""
    import sqlite3

    db_path = DATA_DIR / "auto_bit.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        # Trade stats
        cur = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(pnl) as total_pnl, "
            "AVG(pnl) as avg_pnl, "
            "MIN(pnl) as worst_trade, "
            "MAX(pnl) as best_trade, "
            "SUM(fee) as total_fees "
            "FROM trades WHERE mode = 'paper'"
        )
        row = dict(cur.fetchone())

        total = row["cnt"] or 0
        wins = row["wins"] or 0
        total_pnl = row["total_pnl"] or 0.0
        avg_pnl = row["avg_pnl"] or 0.0
        total_fees = row["total_fees"] or 0.0
        best = row["best_trade"] or 0.0
        worst = row["worst_trade"] or 0.0

        win_rate = (wins / total * 100) if total > 0 else 0.0
        profit_factor = 0.0
        if total > 0:
            gross_profit = sum(
                r["pnl"] for r in conn.execute(
                    "SELECT pnl FROM trades WHERE mode='paper' AND pnl > 0"
                ).fetchall()
            )
            gross_loss = abs(sum(
                r["pnl"] for r in conn.execute(
                    "SELECT pnl FROM trades WHERE mode='paper' AND pnl <= 0"
                ).fetchall()
            ))
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Open positions
        open_pos = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions WHERE mode = 'paper'"
        ).fetchone()["cnt"]

        # Unique symbols traded
        symbols = conn.execute(
            "SELECT DISTINCT symbol FROM trades WHERE mode = 'paper'"
        ).fetchall()
        symbols_traded = [r["symbol"] for r in symbols]

        result = {
            "team": team_name,
            "cycle": cycle_num,
            "duration_minutes": duration_min,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_trades": total,
            "win_count": wins,
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 6),
            "avg_pnl": round(avg_pnl, 6),
            "best_trade": round(best, 6),
            "worst_trade": round(worst, 6),
            "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else "inf",
            "total_fees": round(total_fees, 6),
            "net_pnl": round(total_pnl, 6),
            "open_positions": open_pos,
            "symbols_traded": symbols_traded,
            "symbols_count": len(symbols_traded),
        }

        return result
    finally:
        conn.close()


def save_result(result: dict):
    """Save cycle result to results directory."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    team = result["team"]
    cycle = result["cycle"]
    path = RESULTS_DIR / f"{team}_cycle{cycle}.json"
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"[*] Results saved: {path}")


def print_result(result: dict):
    """Pretty-print cycle result."""
    print(f"\n{'='*50}")
    print(f"  Team {result['team'].upper()} - Cycle {result['cycle']}")
    print(f"{'='*50}")
    print(f"  Duration:     {result['duration_minutes']} min")
    print(f"  Trades:       {result['total_trades']}")
    print(f"  Win Rate:     {result['win_rate']}%")
    print(f"  Total P&L:    {result['total_pnl']:.6f} USDT")
    print(f"  Avg P&L:      {result['avg_pnl']:.6f} USDT")
    print(f"  Best Trade:   {result['best_trade']:.6f} USDT")
    print(f"  Worst Trade:  {result['worst_trade']:.6f} USDT")
    print(f"  Profit Factor:{result['profit_factor']}")
    print(f"  Fees:         {result['total_fees']:.6f} USDT")
    print(f"  Open Pos:     {result['open_positions']}")
    print(f"  Symbols:      {result['symbols_count']} ({', '.join(result['symbols_traded'][:5])}...)")
    print(f"{'='*50}\n")


def main():
    parser = argparse.ArgumentParser(description="Run optimization cycle")
    parser.add_argument("--team", required=True, choices=["alpha", "beta", "gamma", "delta", "epsilon"],
                       help="Team profile to use")
    parser.add_argument("--duration", type=int, default=30,
                       help="Duration in minutes (default: 30)")
    parser.add_argument("--cycle", type=int, default=1,
                       help="Cycle number (for tracking)")
    parser.add_argument("--no-reset", action="store_true",
                       help="Don't reset paper data (continue from previous)")
    args = parser.parse_args()

    profiles = load_profiles()
    if args.team not in profiles:
        print(f"[ERROR] Unknown team: {args.team}")
        sys.exit(1)

    profile = profiles[args.team]

    print(f"\n[*] Optimization Cycle: Team {args.team.upper()}, Cycle {args.cycle}, Duration {args.duration}min")
    print(f"[*] Started at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    # 1. Backup configs
    backup_configs()

    try:
        # 2. Apply profile
        apply_profile(args.team, profile)

        # 3. Reset paper data
        if not args.no_reset:
            reset_paper_data()

        # 4. Start system
        pid = start_system()

        # 5. Wait for duration
        print(f"[*] Running for {args.duration} minutes...")
        try:
            time.sleep(args.duration * 60)
        except KeyboardInterrupt:
            print("\n[!] Interrupted by user")

        # 6. Stop system
        stop_system(pid)

        # 7. Collect and save results
        time.sleep(2)  # Wait for DB flush
        result = collect_results(args.team, args.cycle, args.duration)
        save_result(result)
        print_result(result)

    finally:
        # 8. Restore original configs
        restore_configs()
        print("[*] Original configs restored")


if __name__ == "__main__":
    main()
