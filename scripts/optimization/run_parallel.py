#!/usr/bin/env python3
"""Parallel Optimization Tournament Runner.

Runs all 5 teams SIMULTANEOUSLY, each in a fully isolated environment:
- Separate SQLite database per team
- Separate config directory per team
- Separate GUI port per team (disabled by default)
- Shared Bybit API key (read-only, paper mode)

Usage:
    python3 scripts/optimization/run_parallel.py --duration 30
    python3 scripts/optimization/run_parallel.py --duration 30 --cycle 2 --no-reset

Duration is in minutes per cycle. All teams run at the same time.
"""

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROFILES_PATH = Path(__file__).resolve().parent / "profiles.yaml"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
TEAMS = ["alpha", "beta", "gamma", "delta", "epsilon",
         "zeta", "eta", "theta", "iota", "kappa"]
BASE_PORT = 8090  # alpha=8090, beta=8091, ...


def load_profiles() -> dict:
    with open(PROFILES_PATH) as f:
        return yaml.safe_load(f)


def setup_team_env(team: str, profile: dict, port: int) -> Path:
    """Create isolated environment for a team. Returns team data directory."""
    team_dir = PROJECT_ROOT / "data" / f"team_{team}"
    team_config_dir = team_dir / "config" / "strategy"
    team_config_dir.mkdir(parents=True, exist_ok=True)

    # Copy base config files
    for name in ["app.yaml", "symbols.yaml", "credentials.yaml"]:
        src = PROJECT_ROOT / "config" / name
        dst = team_dir / "config" / name
        if src.exists():
            shutil.copy2(src, dst)

    for name in ["grid.yaml", "scanner.yaml", "asset.yaml", "position.yaml"]:
        src = PROJECT_ROOT / "config" / "strategy" / name
        dst = team_config_dir / name
        if src.exists():
            shutil.copy2(src, dst)

    # Modify app.yaml: unique DB path, unique port, headless
    app_cfg_path = team_dir / "config" / "app.yaml"
    with open(app_cfg_path) as f:
        app_cfg = yaml.safe_load(f)

    app_cfg["database"]["path"] = str(team_dir / "paper.db")
    app_cfg["gui"]["port"] = port
    app_cfg["gui"]["enabled"] = False  # headless for speed
    app_cfg["mode"] = "paper"

    with open(app_cfg_path, "w") as f:
        yaml.dump(app_cfg, f, default_flow_style=False, allow_unicode=True)

    # Apply team profile to grid.yaml
    grid_path = team_config_dir / "grid.yaml"
    with open(grid_path) as f:
        grid_cfg = yaml.safe_load(f)

    grid_profile = profile.get("grid", {})
    gb = grid_cfg["strategies"]["grid_bias"]
    for key in ["range_atr_multiplier", "min_range_pct", "max_range_pct",
                "recenter_interval_minutes", "recenter_threshold_pct",
                "leverage", "qty_per_level_pct", "max_open_levels",
                "max_symbols", "min_spacing_pct", "max_drawdown_pct"]:
        if key in grid_profile:
            gb[key] = grid_profile[key]
    for section in ["adaptive_levels", "dynamic_spacing", "bias", "mtf"]:
        if section in grid_profile:
            if section in gb:
                gb[section].update(grid_profile[section])
            else:
                gb[section] = grid_profile[section]
    exit_profile = profile.get("exit", {})
    if exit_profile:
        grid_cfg["exit"].update(exit_profile)
    with open(grid_path, "w") as f:
        yaml.dump(grid_cfg, f, default_flow_style=False, allow_unicode=True)

    # Apply scanner profile
    scanner_profile = profile.get("scanner", {})
    if scanner_profile:
        scanner_path = team_config_dir / "scanner.yaml"
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

    # Apply asset profile
    asset_profile = profile.get("asset", {})
    if asset_profile:
        asset_path = team_config_dir / "asset.yaml"
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

    return team_dir


def reset_team_db(team_dir: Path):
    """Reset paper trading data in team's DB."""
    db_path = team_dir / "paper.db"
    if db_path.exists():
        db_path.unlink()
    # Fresh DB will be created on startup


def start_team(team: str, team_dir: Path) -> subprocess.Popen:
    """Start a team's trading process."""
    env = os.environ.copy()
    env["AUTO_BIT_CONFIG_DIR"] = str(team_dir / "config")

    log_path = PROJECT_ROOT / "logs" / f"team_{team}.log"
    proc = subprocess.Popen(
        [
            "python3", "-m", "src.main",
            "--mode", "paper",
            "--headless",
            "--config-dir", str(team_dir / "config"),
        ],
        cwd=str(PROJECT_ROOT),
        stdout=open(str(log_path), "w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    return proc


def stop_team(proc: subprocess.Popen, team: str):
    """Stop a team's process gracefully."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception as e:
        print(f"  [{team}] Stop error: {e}")


def collect_team_results(team: str, team_dir: Path, cycle: int, duration: int) -> dict:
    """Collect results from a team's isolated DB."""
    db_path = team_dir / "paper.db"
    if not db_path.exists():
        return {
            "team": team, "cycle": cycle, "duration_minutes": duration,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_trades": 0, "win_count": 0, "win_rate": 0,
            "total_pnl": 0, "avg_pnl": 0, "best_trade": 0, "worst_trade": 0,
            "profit_factor": 0, "total_fees": 0, "net_pnl": 0,
            "open_positions": 0, "symbols_traded": [], "symbols_count": 0,
        }

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        # Check if trades table exists
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        if "trades" not in tables:
            return {
                "team": team, "cycle": cycle, "duration_minutes": duration,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_trades": 0, "win_count": 0, "win_rate": 0,
                "total_pnl": 0, "avg_pnl": 0, "best_trade": 0, "worst_trade": 0,
                "profit_factor": 0, "total_fees": 0, "net_pnl": 0,
                "open_positions": 0, "symbols_traded": [], "symbols_count": 0,
            }

        cur = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(pnl) as total_pnl, AVG(pnl) as avg_pnl, "
            "MIN(pnl) as worst, MAX(pnl) as best, SUM(fee) as fees "
            "FROM trades WHERE mode = 'paper'"
        )
        row = dict(cur.fetchone())

        total = row["cnt"] or 0
        wins = row["wins"] or 0
        total_pnl = row["total_pnl"] or 0.0
        avg_pnl = row["avg_pnl"] or 0.0
        fees = row["fees"] or 0.0
        best = row["best"] or 0.0
        worst = row["worst"] or 0.0

        win_rate = (wins / total * 100) if total > 0 else 0.0

        gross_profit = sum(
            r["pnl"] for r in conn.execute(
                "SELECT pnl FROM trades WHERE mode='paper' AND pnl > 0"
            ).fetchall()
        ) if total > 0 else 0
        gross_loss = abs(sum(
            r["pnl"] for r in conn.execute(
                "SELECT pnl FROM trades WHERE mode='paper' AND pnl <= 0"
            ).fetchall()
        )) if total > 0 else 0
        pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)

        open_pos = 0
        if "positions" in tables:
            open_pos = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE mode='paper'"
            ).fetchone()[0]

        symbols = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM trades WHERE mode='paper'"
        ).fetchall()] if total > 0 else []

        return {
            "team": team, "cycle": cycle, "duration_minutes": duration,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_trades": total, "win_count": wins,
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 6),
            "avg_pnl": round(avg_pnl, 6),
            "best_trade": round(best, 6),
            "worst_trade": round(worst, 6),
            "profit_factor": round(pf, 4) if pf != float("inf") else "inf",
            "total_fees": round(fees, 6),
            "net_pnl": round(total_pnl, 6),
            "open_positions": open_pos,
            "symbols_traded": symbols,
            "symbols_count": len(symbols),
        }
    finally:
        conn.close()


def print_live_status(results: dict):
    """Print a compact comparison table."""
    print(f"\n{'='*75}")
    print(f"  CYCLE RESULTS — {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print(f"{'='*75}")
    print(f"  {'Team':<10} {'Trades':>8} {'WinRate':>8} {'PnL':>12} {'AvgPnL':>10} {'Symbols':>8}")
    print(f"  {'─'*62}")
    for team in TEAMS:
        r = results.get(team, {})
        trades = r.get("total_trades", 0)
        wr = r.get("win_rate", 0)
        pnl = r.get("total_pnl", 0)
        avg = r.get("avg_pnl", 0)
        sym = r.get("symbols_count", 0)
        marker = " *" if pnl == max(r2.get("total_pnl", -999) for r2 in results.values()) else ""
        print(f"  {team:<10} {trades:>8} {wr:>7.1f}% {pnl:>11.6f} {avg:>10.6f} {sym:>8}{marker}")
    print(f"{'='*75}")


def main():
    parser = argparse.ArgumentParser(description="Run parallel optimization")
    parser.add_argument("--duration", type=int, default=30, help="Minutes per cycle")
    parser.add_argument("--cycle", type=int, default=1, help="Starting cycle number")
    parser.add_argument("--no-reset", action="store_true", help="Continue from previous data")
    args = parser.parse_args()

    profiles = load_profiles()

    print(f"\n{'='*75}")
    print(f"  PARALLEL OPTIMIZATION TOURNAMENT")
    print(f"  {len(TEAMS)} teams running SIMULTANEOUSLY")
    print(f"  Duration: {args.duration} min/cycle")
    print(f"  Cycle: {args.cycle}")
    print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}\n")

    # 1. Setup isolated environments
    team_dirs = {}
    for i, team in enumerate(TEAMS):
        profile = profiles[team]
        port = BASE_PORT + i
        team_dir = setup_team_env(team, profile, port)
        team_dirs[team] = team_dir
        if not args.no_reset:
            reset_team_db(team_dir)
        print(f"  [{team}] Env ready: db={team_dir/'paper.db'}")

    print()

    # 2. Start teams with staggered delay to avoid API rate limits
    procs = {}
    for i, team in enumerate(TEAMS):
        proc = start_team(team, team_dirs[team])
        procs[team] = proc
        print(f"  [{team}] Started (PID={proc.pid})")
        if i < len(TEAMS) - 1:
            time.sleep(5)  # 5-second stagger to spread API init calls

    print(f"\n  All {len(TEAMS)} teams running. Waiting {args.duration} minutes...\n")

    # 3. Wait, with periodic status checks
    start_time = time.time()
    end_time = start_time + args.duration * 60

    try:
        while time.time() < end_time:
            remaining = int(end_time - time.time())
            alive = sum(1 for p in procs.values() if p.poll() is None)
            mins = remaining // 60
            secs = remaining % 60
            print(f"\r  [{mins:02d}:{secs:02d} remaining] {alive}/{len(TEAMS)} teams alive", end="", flush=True)

            # Check if any team died (crash or unexpected exit)
            for team, proc in procs.items():
                if proc.poll() is not None:
                    print(f"\n  [!] {team} died (exit={proc.returncode}), restarting...")
                    time.sleep(3)  # Brief delay before restart
                    procs[team] = start_team(team, team_dirs[team])

            time.sleep(5)
    except KeyboardInterrupt:
        print("\n\n  [!] Interrupted by user")

    # 4. Stop all teams
    print(f"\n\n  Stopping all teams...")
    for team in TEAMS:
        stop_team(procs[team], team)
        print(f"  [{team}] Stopped")

    time.sleep(3)

    # 5. Collect results
    print(f"\n  Collecting results...")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    all_results = {}
    for team in TEAMS:
        result = collect_team_results(team, team_dirs[team], args.cycle, args.duration)
        all_results[team] = result
        path = RESULTS_DIR / f"{team}_cycle{args.cycle}.json"
        with open(path, "w") as f:
            json.dump(result, f, indent=2)

    # 6. Print comparison
    print_live_status(all_results)

    # 7. Print per-team details
    for team in TEAMS:
        r = all_results[team]
        syms = ", ".join(r.get("symbols_traded", [])[:5])
        if len(r.get("symbols_traded", [])) > 5:
            syms += "..."
        print(f"  {team}: trades={r['total_trades']} wr={r['win_rate']}% pnl={r['total_pnl']:.6f} "
              f"fees={r['total_fees']:.6f} open={r['open_positions']} symbols=[{syms}]")

    print(f"\n  Results saved to {RESULTS_DIR}/")
    print(f"  Run 'python3 scripts/optimization/report.py' for final report.\n")


if __name__ == "__main__":
    main()
