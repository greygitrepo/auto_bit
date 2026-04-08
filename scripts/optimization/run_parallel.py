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

    # Determine strategy type: "grid_bias" (default) or position-based
    strategy_type = profile.get("strategy_type", "grid_bias")

    # Apply team profile to grid.yaml
    grid_path = team_config_dir / "grid.yaml"
    with open(grid_path) as f:
        grid_cfg = yaml.safe_load(f)

    if strategy_type == "grid_bias":
        # Grid strategy: apply grid params
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
    else:
        # Position strategy: disable grid, set position.active
        grid_cfg["active"] = ""  # Empty = no grid strategy

    with open(grid_path, "w") as f:
        yaml.dump(grid_cfg, f, default_flow_style=False, allow_unicode=True)

    # Apply position strategy config if not grid
    if strategy_type != "grid_bias":
        position_path = team_config_dir / "position.yaml"
        with open(position_path) as f:
            position_cfg = yaml.safe_load(f)
        position_cfg["active"] = strategy_type
        # Apply strategy-specific params
        strategy_params = profile.get("strategy_params", {})
        if strategy_params:
            if "strategies" not in position_cfg:
                position_cfg["strategies"] = {}
            position_cfg["strategies"][strategy_type] = strategy_params
        # Apply exit params
        exit_profile = profile.get("exit_params", {})
        if exit_profile:
            position_cfg["exit"] = {**position_cfg.get("exit", {}), **exit_profile}
        with open(position_path, "w") as f:
            yaml.dump(position_cfg, f, default_flow_style=False, allow_unicode=True)

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


import random

# Available strategy types for overhaul rotation
ALL_STRATEGY_TYPES = [
    "grid_bias", "momentum_scalper", "breakout_scalper",
    "rsi_reversal", "ema_crossover", "volatility_breakout",
]


def _overhaul_team(team: str, profiles: dict, results: dict):
    """Overhaul a stagnant team's strategy entirely.

    1. Pick the best-performing team's strategy type as base
    2. Mutate parameters randomly for diversity
    3. Or assign a completely different strategy type
    4. Save to profiles.yaml so next cycle picks it up
    5. Reset the team's DB for a fresh start
    """
    # Find the best performing team
    best_team = max(results, key=lambda t: results[t].get("total_pnl", -999))
    best_profile = profiles.get(best_team, {})
    current_type = profiles.get(team, {}).get("strategy_type", "grid_bias")

    # Decide: 50% chance clone+mutate best team, 50% chance try a new strategy type
    if random.random() < 0.5 and best_team != team:
        # Clone best team's profile with mutations
        import copy
        new_profile = copy.deepcopy(best_profile)
        new_profile = _mutate_profile(new_profile)
        print(f"    → {team}: cloning {best_team} (type={new_profile.get('strategy_type', 'grid_bias')}) with mutations")
    else:
        # Pick a strategy type that the team hasn't used
        available = [s for s in ALL_STRATEGY_TYPES if s != current_type]
        new_type = random.choice(available)
        new_profile = _generate_profile(new_type)
        print(f"    → {team}: switching to new strategy: {new_type}")

    # Update profiles dict (in memory) and save to YAML
    profiles[team] = new_profile
    with open(PROFILES_PATH, "w") as f:
        yaml.dump(profiles, f, default_flow_style=False, allow_unicode=True)

    # Reset team DB for fresh start
    team_dir = PROJECT_ROOT / "data" / f"team_{team}"
    reset_team_db(team_dir)
    print(f"    → {team}: DB reset for fresh start")


def _mutate_profile(profile: dict) -> dict:
    """Apply random mutations to a profile's parameters."""
    import copy
    p = copy.deepcopy(profile)

    strategy_type = p.get("strategy_type", "grid_bias")

    if strategy_type == "grid_bias":
        grid = p.get("grid", {})
        # Mutate numeric params by ±20%
        for key in ["range_atr_multiplier", "min_spacing_pct", "leverage",
                     "qty_per_level_pct", "max_open_levels", "max_symbols"]:
            if key in grid:
                val = grid[key]
                grid[key] = round(val * random.uniform(0.8, 1.2), 2)
                if key in ("leverage", "max_open_levels", "max_symbols"):
                    grid[key] = max(1, int(grid[key]))
    else:
        # Position strategy: mutate strategy_params
        sp = p.get("strategy_params", {})
        for key in list(sp.keys()):
            if isinstance(sp[key], (int, float)) and not isinstance(sp[key], bool):
                sp[key] = round(sp[key] * random.uniform(0.8, 1.2), 4)

    # Mutate scanner params
    scanner = p.get("scanner", {})
    if "min_24h_turnover_usdt" in scanner:
        scanner["min_24h_turnover_usdt"] = int(scanner["min_24h_turnover_usdt"] * random.uniform(0.5, 2.0))
    if "min_score" in scanner:
        scanner["min_score"] = max(3, int(scanner["min_score"] * random.uniform(0.7, 1.3)))

    return p


def _generate_profile(strategy_type: str) -> dict:
    """Generate a fresh profile for a given strategy type."""
    # Common scanner settings (random range)
    scanner = {
        "max_days_since_listed": random.choice([30, 60, 180, 365, 730]),
        "min_24h_turnover_usdt": random.choice([500000, 1000000, 3000000, 5000000, 10000000]),
        "max_candidates": random.randint(15, 60),
        "min_score": random.randint(5, 30),
    }
    asset = {
        "max_daily_loss_pct": random.choice([20, 30, 40]),
        "max_daily_trades": random.choice([200, 400, 600]),
        "consecutive_loss_cooldown_after": random.randint(3, 8),
        "consecutive_loss_stop_after": random.randint(8, 20),
    }

    if strategy_type == "grid_bias":
        return {
            "strategy_type": "grid_bias",
            "grid": {
                "range_atr_multiplier": round(random.uniform(0.8, 2.5), 1),
                "min_range_pct": round(random.uniform(0.5, 1.5), 1),
                "max_range_pct": round(random.uniform(5, 12), 0),
                "recenter_interval_minutes": random.choice([60, 90, 120, 180]),
                "recenter_threshold_pct": round(random.uniform(1.5, 4.0), 1),
                "leverage": random.randint(2, 8),
                "qty_per_level_pct": round(random.uniform(1.0, 3.0), 1),
                "max_open_levels": random.randint(4, 12),
                "max_symbols": random.randint(3, 15),
                "min_spacing_pct": round(random.uniform(0.45, 0.70), 2),
                "adaptive_levels": {"enabled": True,
                    "target_spacing_pct": round(random.uniform(0.45, 0.70), 2),
                    "min_levels": random.randint(3, 6),
                    "max_levels": random.randint(10, 20)},
                "dynamic_spacing": {"enabled": True,
                    "atr_lookback_hours": random.choice([6, 12, 24]),
                    "low_vol_multiplier": round(random.uniform(0.5, 0.9), 1),
                    "high_vol_multiplier": round(random.uniform(1.2, 2.0), 1),
                    "vol_ratio_low_threshold": 0.5,
                    "vol_ratio_high_threshold": 1.5},
                "bias": {"enabled": random.choice([True, False]),
                    "threshold": round(random.uniform(0.08, 0.20), 2),
                    "max_level_shift": random.randint(1, 4),
                    "ema_weight": 0.4,
                    "funding_rate": {"enabled": random.choice([True, False]), "weight": 0.3},
                    "btc_eth_weight": 0.3},
                "mtf": {"enabled": random.choice([True, False]),
                    "require_15m_alignment": False,
                    "weight_5m": 0.4, "weight_15m": 0.3, "weight_1h": 0.3},
                "max_drawdown_pct": random.choice([15, 20, 25]),
            },
            "exit": {
                "grid_timeout_hours": random.choice([8, 12, 18, 24]),
                "hard_stop_loss_pct": round(random.uniform(3.0, 8.0), 1),
            },
            "scanner": scanner,
            "asset": asset,
        }
    else:
        # Position-based strategy
        return {
            "strategy_type": strategy_type,
            "strategy_params": _default_strategy_params(strategy_type),
            "exit_params": {
                "stop_loss": {
                    "atr_period": 14,
                    "atr_multiplier": round(random.uniform(1.2, 2.5), 1),
                    "min_pct": 0.3,
                    "max_pct": round(random.uniform(1.5, 3.0), 1),
                },
                "take_profit": {
                    "risk_reward_ratio": round(random.uniform(1.2, 2.5), 1),
                },
                "trailing_stop": {
                    "activation_r": round(random.uniform(0.4, 1.0), 1),
                    "callback_atr_multiplier": round(random.uniform(0.4, 0.8), 1),
                },
                "time_limit": {
                    "max_holding_minutes": random.choice([30, 45, 60, 90]),
                    "warning_minutes": 25,
                },
            },
            "scanner": scanner,
            "asset": asset,
        }


def _default_strategy_params(strategy_type: str) -> dict:
    """Return default params for a position strategy type."""
    if strategy_type == "breakout_scalper":
        return {
            "bb_period": 20, "bb_std": 2.0,
            "volume_multiplier": round(random.uniform(1.0, 1.8), 1),
            "bb_squeeze_threshold": round(random.uniform(0.015, 0.03), 3),
            "min_confidence": 0.5,
        }
    elif strategy_type == "rsi_reversal":
        return {
            "rsi_oversold": random.randint(15, 30),
            "rsi_overbought": random.randint(70, 85),
            "require_reversal_candle": random.choice([True, False]),
            "volume_multiplier": round(random.uniform(0.8, 1.5), 1),
            "min_confidence": 0.5,
        }
    elif strategy_type == "ema_crossover":
        return {
            "ema_fast": random.choice([3, 5, 8]),
            "ema_slow": random.choice([15, 20, 25]),
            "adx_threshold": random.randint(15, 25),
            "require_adx": random.choice([True, False]),
            "volume_multiplier": round(random.uniform(0.8, 1.3), 1),
            "min_confidence": 0.5,
        }
    elif strategy_type == "volatility_breakout":
        return {
            "atr_breakout_multiplier": round(random.uniform(1.2, 2.0), 1),
            "close_position_ratio": round(random.uniform(0.6, 0.8), 1),
            "volume_multiplier": round(random.uniform(1.0, 1.5), 1),
            "min_confidence": 0.5,
        }
    elif strategy_type == "momentum_scalper":
        return {
            "rsi_long_range": [random.randint(35, 50), random.randint(70, 85)],
            "rsi_short_range": [random.randint(15, 30), random.randint(50, 65)],
            "volume_multiplier": round(random.uniform(0.8, 1.5), 1),
            "adx_threshold": random.randint(15, 25),
            "min_confidence": 0.5,
        }
    return {}


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

    # 8. Check for stagnant teams (no new trades vs previous cycle)
    warnings_file = RESULTS_DIR / "warnings.json"
    warnings = {}
    if warnings_file.exists():
        with open(warnings_file) as f:
            warnings = json.load(f)

    if args.cycle > 1:
        for team in TEAMS:
            prev_path = RESULTS_DIR / f"{team}_cycle{args.cycle - 1}.json"
            if prev_path.exists():
                with open(prev_path) as f:
                    prev = json.load(f)
                curr = all_results[team]
                if curr["total_trades"] == prev["total_trades"]:
                    w = warnings.get(team, 0) + 1
                    warnings[team] = w
                    print(f"  [WARNING {w}/3] {team}: no new trades since last cycle!")
                    if w >= 3:
                        print(f"  [PENALTY] {team}: 3 warnings — STRATEGY OVERHAUL!")
                        _overhaul_team(team, profiles, all_results)
                        warnings[team] = 0  # Reset after overhaul
                else:
                    warnings[team] = 0  # Reset on progress
            else:
                warnings[team] = 0

    with open(warnings_file, "w") as f:
        json.dump(warnings, f, indent=2)

    print(f"\n  Results saved to {RESULTS_DIR}/")
    print(f"  Run 'python3 scripts/optimization/report.py' for final report.\n")


if __name__ == "__main__":
    main()
