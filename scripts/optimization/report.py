#!/usr/bin/env python3
"""Generate final tournament report comparing all teams.

Reads all result JSON files from results/ directory and produces
a comparison report with the winning team's parameters.

Usage:
    python3 scripts/optimization/report.py
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

RESULTS_DIR = Path(__file__).resolve().parent / "results"
PROFILES_PATH = Path(__file__).resolve().parent / "profiles.yaml"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config" / "strategy"


def load_all_results() -> dict:
    """Load all cycle results, grouped by team."""
    teams = defaultdict(list)
    for path in sorted(RESULTS_DIR.glob("*.json")):
        with open(path) as f:
            data = json.load(f)
        teams[data["team"]].append(data)
    return dict(teams)


def aggregate_team(results: list) -> dict:
    """Aggregate metrics across all cycles for a team."""
    if not results:
        return {}

    total_trades = sum(r["total_trades"] for r in results)
    total_pnl = sum(r["total_pnl"] for r in results)
    total_fees = sum(r["total_fees"] for r in results)
    total_wins = sum(r["win_count"] for r in results)
    total_minutes = sum(r["duration_minutes"] for r in results)

    all_symbols = set()
    for r in results:
        all_symbols.update(r.get("symbols_traded", []))

    win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
    avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
    pnl_per_hour = total_pnl / (total_minutes / 60) if total_minutes > 0 else 0

    # Best and worst cycles
    best_cycle = max(results, key=lambda r: r["total_pnl"]) if results else None
    worst_cycle = min(results, key=lambda r: r["total_pnl"]) if results else None

    return {
        "team": results[0]["team"],
        "cycles": len(results),
        "total_minutes": total_minutes,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 6),
        "avg_pnl_per_trade": round(avg_pnl, 6),
        "pnl_per_hour": round(pnl_per_hour, 6),
        "total_fees": round(total_fees, 6),
        "net_pnl": round(total_pnl, 6),
        "symbols_traded": len(all_symbols),
        "best_cycle_pnl": round(best_cycle["total_pnl"], 6) if best_cycle else 0,
        "worst_cycle_pnl": round(worst_cycle["total_pnl"], 6) if worst_cycle else 0,
    }


def determine_winner(aggregates: list) -> dict:
    """Determine the winning team based on multiple criteria."""
    if not aggregates:
        return {}

    # Primary: total PnL
    # Secondary: PnL per hour (efficiency)
    # Tertiary: win rate
    scored = []
    for agg in aggregates:
        if agg["total_trades"] == 0:
            score = -999
        else:
            score = (
                agg["total_pnl"] * 50 +           # 50% weight: absolute profit
                agg["pnl_per_hour"] * 30 +          # 30% weight: efficiency
                agg["win_rate"] * 0.2               # 20% weight: consistency
            )
        scored.append((score, agg))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored else {}


def generate_report(teams_data: dict) -> str:
    """Generate the full text report."""
    lines = []
    lines.append("=" * 60)
    lines.append("  AUTO_BIT OPTIMIZATION TOURNAMENT - FINAL REPORT")
    lines.append(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("=" * 60)
    lines.append("")

    aggregates = []
    for team_name, results in sorted(teams_data.items()):
        agg = aggregate_team(results)
        if agg:
            aggregates.append(agg)

    if not aggregates:
        lines.append("  No results found.")
        return "\n".join(lines)

    # Per-team results
    for agg in aggregates:
        lines.append(f"  Team {agg['team'].upper()}")
        lines.append(f"  {'─' * 40}")
        lines.append(f"  Cycles:          {agg['cycles']}")
        lines.append(f"  Total Time:      {agg['total_minutes']} min")
        lines.append(f"  Total Trades:    {agg['total_trades']}")
        lines.append(f"  Win Rate:        {agg['win_rate']}%")
        lines.append(f"  Total P&L:       {agg['total_pnl']:.6f} USDT")
        lines.append(f"  P&L/Hour:        {agg['pnl_per_hour']:.6f} USDT")
        lines.append(f"  Avg P&L/Trade:   {agg['avg_pnl_per_trade']:.6f} USDT")
        lines.append(f"  Total Fees:      {agg['total_fees']:.6f} USDT")
        lines.append(f"  Symbols Traded:  {agg['symbols_traded']}")
        lines.append(f"  Best Cycle:      {agg['best_cycle_pnl']:.6f} USDT")
        lines.append(f"  Worst Cycle:     {agg['worst_cycle_pnl']:.6f} USDT")
        lines.append("")

    # Comparison table
    lines.append("  COMPARISON TABLE")
    all_teams = ["alpha", "beta", "gamma", "delta", "epsilon",
                  "zeta", "eta", "theta", "iota", "kappa"]
    col_w = 9
    table_width = 18 + (col_w + 1) * len(all_teams)
    lines.append(f"  {'─' * table_width}")
    header = f"  {'Metric':<18}" + "".join(f" {t[:7].capitalize():>{col_w}}" for t in all_teams)
    lines.append(header)
    lines.append(f"  {'─' * table_width}")

    team_map = {a["team"]: a for a in aggregates}
    metrics = [
        ("Trades", "total_trades"),
        ("Win Rate %", "win_rate"),
        ("Total PnL", "total_pnl"),
        ("PnL/Hour", "pnl_per_hour"),
        ("Avg PnL/Trade", "avg_pnl_per_trade"),
        ("Fees", "total_fees"),
        ("Symbols", "symbols_traded"),
    ]

    for label, key in metrics:
        vals = []
        for team in all_teams:
            if team in team_map:
                v = team_map[team].get(key, 0)
                if isinstance(v, float):
                    vals.append(f"{v:>{col_w}.4f}")
                else:
                    vals.append(f"{v:>{col_w}}")
            else:
                vals.append(f"{'N/A':>{col_w}}")
        lines.append(f"  {label:<18}" + " ".join(vals))

    lines.append(f"  {'─' * table_width}")
    lines.append("")

    # Winner
    winner = determine_winner(aggregates)
    if winner:
        lines.append(f"  WINNER: Team {winner['team'].upper()}")
        lines.append(f"  Total P&L: {winner['total_pnl']:.6f} USDT")
        lines.append(f"  Win Rate: {winner['win_rate']}%")
        lines.append(f"  P&L/Hour: {winner['pnl_per_hour']:.6f} USDT")
        lines.append("")

        # Print winning profile parameters
        with open(PROFILES_PATH) as f:
            profiles = yaml.safe_load(f)
        winning_profile = profiles.get(winner["team"], {})
        lines.append(f"  WINNING PARAMETERS (Team {winner['team'].upper()}):")
        lines.append(f"  {'─' * 40}")
        lines.append(yaml.dump(winning_profile, default_flow_style=False, indent=4))

    lines.append("=" * 60)
    return "\n".join(lines)


def apply_winning_config(winner_team: str):
    """Apply the winning team's profile as the new default config."""
    with open(PROFILES_PATH) as f:
        profiles = yaml.safe_load(f)

    profile = profiles.get(winner_team)
    if not profile:
        print(f"[!] No profile found for team {winner_team}")
        return

    # This uses the same logic as run_cycle.py's apply_profile
    # Import and use it
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_cycle",
        str(Path(__file__).resolve().parent / "run_cycle.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod.backup_configs()
    mod.apply_profile(winner_team, profile)
    print(f"[OK] Winning config (Team {winner_team.upper()}) applied to config/strategy/")


def main():
    teams_data = load_all_results()

    if not teams_data:
        print("[!] No results found in", RESULTS_DIR)
        return

    report = generate_report(teams_data)
    print(report)

    # Save report
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = RESULTS_DIR / "final_report.txt"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\n[*] Report saved to: {report_path}")

    # Determine and apply winner
    aggregates = []
    for team_name, results in teams_data.items():
        agg = aggregate_team(results)
        if agg:
            aggregates.append(agg)

    winner = determine_winner(aggregates)
    if winner and winner.get("total_trades", 0) > 0:
        print(f"\n[*] Applying winning config: Team {winner['team'].upper()}")
        apply_winning_config(winner["team"])
    else:
        print("\n[!] No clear winner or no trades. Keeping current config.")


if __name__ == "__main__":
    main()
