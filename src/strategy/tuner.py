"""Strategy parameter auto-tuner.

Monitors signal generation frequency during paper trading and dynamically
adjusts MomentumScalper parameters to achieve a target signal rate.

Features:
- 7 tuning levels (0=default → 6=maximum relaxation)
- Persists state to DB system_state for crash recovery
- Tracks stable-level streak; proposes YAML save after N consecutive stable windows
- Exposes status dict for GUI display

Tuning levels (0 = default, higher = more relaxed):
  Level 0: Config defaults
  Level 1: Widen RSI ranges, lower volume multiplier
  Level 2: Disable VWAP filter
  Level 3: Disable higher TF filter
  Level 4: Further widen RSI, minimal volume requirement
  Level 5: Relax EMA to 2-EMA alignment
  Level 6: Minimal EMA requirement
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger


# How many consecutive stable windows before suggesting YAML save
_STABLE_STREAK_FOR_PROPOSAL = 6


class StrategyTuner:
    """Adaptive parameter tuner for MomentumScalper.

    Parameters
    ----------
    config:
        The ``tuner`` section of ``position.yaml``.
    initial_params:
        The MomentumScalper's starting parameters (will be modified in-place).
    db:
        Optional DatabaseManager for persisting state. If None, no persistence.
    """

    MAX_LEVEL = 6

    def __init__(
        self,
        config: dict,
        initial_params: Dict[str, Any],
        db=None,
    ) -> None:
        self._enabled = config.get("enabled", False)
        self._window = config.get("evaluation_window", 30)
        self._min_signal_rate = config.get("min_signal_rate", 0.05)
        self._max_signal_rate = config.get("max_signal_rate", 0.30)
        self._db = db

        # Track evaluation outcomes
        self._eval_count = 0
        self._signal_count = 0
        self._hold_count = 0

        # Current tuning level
        self._level = 0

        # Snapshot of original config values for reference
        self._original_params = {
            "rsi_long_range": list(initial_params.get("rsi_long_range", [40, 80])),
            "rsi_short_range": list(initial_params.get("rsi_short_range", [20, 60])),
            "volume_multiplier": initial_params.get("volume_multiplier", 1.0),
            "vwap_enabled": initial_params.get("vwap_enabled", True),
            "higher_tf_enabled": initial_params.get("higher_tf", {}).get("enabled", True),
        }

        self._last_tune_time = time.time()
        self._tune_history: list[dict] = []
        self._last_signal_rate: float = 0.0

        # Stable-level streak tracking for YAML proposal
        self._stable_streak = 0
        self._yaml_proposed = False  # True once a proposal has been made

        if self._enabled:
            logger.info(
                "StrategyTuner enabled: window={} min_rate={:.2f} max_rate={:.2f}",
                self._window, self._min_signal_rate, self._max_signal_rate,
            )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def level(self) -> int:
        return self._level

    # ------------------------------------------------------------------
    # Evaluation tracking
    # ------------------------------------------------------------------

    def record_evaluation(self, is_signal: bool) -> None:
        """Record a single strategy evaluation outcome."""
        if not self._enabled:
            return
        self._eval_count += 1
        if is_signal:
            self._signal_count += 1
        else:
            self._hold_count += 1

    def should_tune(self) -> bool:
        """Check if enough evaluations have accumulated to trigger tuning."""
        if not self._enabled:
            return False
        return self._eval_count >= self._window

    # ------------------------------------------------------------------
    # Core tuning logic
    # ------------------------------------------------------------------

    def tune(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze signal rate and adjust parameters if needed."""
        if not self._enabled or self._eval_count == 0:
            return params

        signal_rate = self._signal_count / self._eval_count
        elapsed = time.time() - self._last_tune_time
        self._last_signal_rate = signal_rate

        logger.info(
            "Tuner check: {}/{} signals ({:.1%}) in {:.0f}s, level={}",
            self._signal_count, self._eval_count, signal_rate,
            elapsed, self._level,
        )

        old_level = self._level

        if signal_rate < self._min_signal_rate and self._level < self.MAX_LEVEL:
            self._level += 1
            logger.info(
                "Tuner: signal rate {:.1%} < {:.1%} threshold → RELAXING to level {}",
                signal_rate, self._min_signal_rate, self._level,
            )
        elif signal_rate > self._max_signal_rate and self._level > 0:
            self._level -= 1
            logger.info(
                "Tuner: signal rate {:.1%} > {:.1%} threshold → TIGHTENING to level {}",
                signal_rate, self._max_signal_rate, self._level,
            )
        else:
            logger.info(
                "Tuner: signal rate {:.1%} within target range, keeping level {}",
                signal_rate, self._level,
            )

        if self._level != old_level:
            self._apply_level(params)
            self._stable_streak = 0
            self._tune_history.append({
                "time": time.time(),
                "old_level": old_level,
                "new_level": self._level,
                "signal_rate": round(signal_rate, 4),
                "eval_count": self._eval_count,
            })
        else:
            # Level stayed the same → increment stable streak
            self._stable_streak += 1

        # Reset counters for next window
        self._eval_count = 0
        self._signal_count = 0
        self._hold_count = 0
        self._last_tune_time = time.time()

        # Persist to DB
        self._save_to_db(params)

        # Check if we should propose YAML save
        if (
            self._stable_streak >= _STABLE_STREAK_FOR_PROPOSAL
            and not self._yaml_proposed
        ):
            self._yaml_proposed = True
            logger.info(
                "Tuner: level {} stable for {} windows → "
                "YAML save proposed (use GUI or /api/tuner/apply to apply)",
                self._level, self._stable_streak,
            )

        return params

    # ------------------------------------------------------------------
    # Level application
    # ------------------------------------------------------------------

    def _apply_level(self, params: Dict[str, Any]) -> None:
        """Apply parameter adjustments for the current tuning level."""
        orig = self._original_params

        if self._level == 0:
            params["rsi_long_range"] = list(orig["rsi_long_range"])
            params["rsi_short_range"] = list(orig["rsi_short_range"])
            params["volume_multiplier"] = orig["volume_multiplier"]
            params["vwap_enabled"] = orig["vwap_enabled"]
            params.pop("ema_alignment_mode", None)
            if "higher_tf" in params:
                params["higher_tf"]["enabled"] = orig["higher_tf_enabled"]
            logger.info("Tuner L0: restored original parameters")

        elif self._level == 1:
            params["rsi_long_range"] = [35, 82]
            params["rsi_short_range"] = [18, 65]
            params["volume_multiplier"] = 0.8
            logger.info("Tuner L1: RSI long=[35,82] short=[18,65] vol_mult=0.8")

        elif self._level == 2:
            params["rsi_long_range"] = [30, 85]
            params["rsi_short_range"] = [15, 70]
            params["volume_multiplier"] = 0.7
            params["vwap_enabled"] = False
            logger.info("Tuner L2: RSI=[30,85]/[15,70] vol=0.7 VWAP=OFF")

        elif self._level == 3:
            params["rsi_long_range"] = [25, 85]
            params["rsi_short_range"] = [15, 75]
            params["volume_multiplier"] = 0.6
            params["vwap_enabled"] = False
            if "higher_tf" in params:
                params["higher_tf"]["enabled"] = False
            logger.info("Tuner L3: RSI=[25,85]/[15,75] vol=0.6 VWAP=OFF HTF=OFF")

        elif self._level == 4:
            params["rsi_long_range"] = [20, 88]
            params["rsi_short_range"] = [12, 80]
            params["volume_multiplier"] = 0.5
            params["vwap_enabled"] = False
            if "higher_tf" in params:
                params["higher_tf"]["enabled"] = False
            logger.info("Tuner L4: RSI=[20,88]/[12,80] vol=0.5 VWAP=OFF HTF=OFF")

        elif self._level == 5:
            params["rsi_long_range"] = [20, 88]
            params["rsi_short_range"] = [12, 80]
            params["volume_multiplier"] = 0.4
            params["vwap_enabled"] = False
            params["ema_alignment_mode"] = "relaxed"
            if "higher_tf" in params:
                params["higher_tf"]["enabled"] = False
            logger.info("Tuner L5: EMA=relaxed RSI=[20,88]/[12,80] vol=0.4")

        elif self._level >= 6:
            params["rsi_long_range"] = [15, 90]
            params["rsi_short_range"] = [10, 85]
            params["volume_multiplier"] = 0.3
            params["vwap_enabled"] = False
            params["ema_alignment_mode"] = "minimal"
            if "higher_tf" in params:
                params["higher_tf"]["enabled"] = False
            logger.info("Tuner L6 (MAX): EMA=minimal RSI=[15,90]/[10,85] vol=0.3")

    # ------------------------------------------------------------------
    # DB persistence
    # ------------------------------------------------------------------

    def _save_to_db(self, params: Dict[str, Any]) -> None:
        """Persist current tuner state to DB system_state."""
        if self._db is None:
            return
        try:
            self._db.set_state("tuner_level", str(self._level))
            self._db.set_state("tuner_signal_rate", str(round(self._last_signal_rate, 4)))
            self._db.set_state("tuner_stable_streak", str(self._stable_streak))
            self._db.set_state("tuner_yaml_proposed", "1" if self._yaml_proposed else "0")

            # Save current adjusted params
            tuned_params = {
                "rsi_long_range": params.get("rsi_long_range"),
                "rsi_short_range": params.get("rsi_short_range"),
                "volume_multiplier": params.get("volume_multiplier"),
                "vwap_enabled": params.get("vwap_enabled"),
                "ema_alignment_mode": params.get("ema_alignment_mode", "strict"),
                "higher_tf_enabled": params.get("higher_tf", {}).get("enabled", True),
            }
            self._db.set_state("tuner_params", json.dumps(tuned_params))

            # Save history (last 20 entries)
            self._db.set_state("tuner_history", json.dumps(self._tune_history[-20:]))
        except Exception as exc:
            logger.warning("Tuner: failed to save state to DB: {}", exc)

    def restore_from_db(self, params: Dict[str, Any]) -> None:
        """Restore tuner state from DB system_state on startup.

        Parameters
        ----------
        params:
            MomentumScalper's params dict — will be modified in-place
            if a saved level is restored.
        """
        if self._db is None or not self._enabled:
            return

        try:
            level_raw = self._db.get_state("tuner_level")
            if level_raw is None:
                logger.info("Tuner: no saved state in DB, starting fresh")
                return

            self._level = int(level_raw)
            rate_raw = self._db.get_state("tuner_signal_rate")
            self._last_signal_rate = float(rate_raw) if rate_raw else 0.0

            streak_raw = self._db.get_state("tuner_stable_streak")
            self._stable_streak = int(streak_raw) if streak_raw else 0

            proposed_raw = self._db.get_state("tuner_yaml_proposed")
            self._yaml_proposed = proposed_raw == "1" if proposed_raw else False

            history_raw = self._db.get_state("tuner_history")
            if history_raw:
                self._tune_history = json.loads(history_raw)

            # Apply restored level to params
            if self._level > 0:
                self._apply_level(params)

            logger.info(
                "Tuner: restored from DB: level={} signal_rate={:.1%} "
                "stable_streak={} yaml_proposed={}",
                self._level, self._last_signal_rate,
                self._stable_streak, self._yaml_proposed,
            )
        except Exception as exc:
            logger.warning("Tuner: failed to restore from DB: {}", exc)

    # ------------------------------------------------------------------
    # YAML save (user-initiated)
    # ------------------------------------------------------------------

    def save_to_yaml(self, params: Dict[str, Any]) -> dict:
        """Write current tuned parameters to position.yaml.

        Returns a status dict with success flag and details.
        This should only be called via user action (GUI button / API call).
        """
        yaml_path = Path(__file__).resolve().parent.parent.parent / "config" / "strategy" / "position.yaml"

        try:
            import yaml

            with open(yaml_path, "r") as f:
                cfg = yaml.safe_load(f) or {}

            # Update momentum_scalper section
            ms = cfg.setdefault("strategies", {}).setdefault("momentum_scalper", {})
            ms["rsi_long_range"] = params.get("rsi_long_range", ms.get("rsi_long_range"))
            ms["rsi_short_range"] = params.get("rsi_short_range", ms.get("rsi_short_range"))
            ms["volume_multiplier"] = params.get("volume_multiplier", ms.get("volume_multiplier"))
            ms["vwap_enabled"] = params.get("vwap_enabled", ms.get("vwap_enabled"))

            if "higher_tf" not in ms:
                ms["higher_tf"] = {}
            ms["higher_tf"]["enabled"] = params.get("higher_tf", {}).get("enabled", True)

            # Save tuner metadata as comment-like field
            ms["_tuner_applied"] = {
                "level": self._level,
                "signal_rate": round(self._last_signal_rate, 4),
                "applied_at": int(time.time()),
                "stable_streak": self._stable_streak,
            }

            with open(yaml_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

            logger.info(
                "Tuner: parameters saved to {} (level={})",
                yaml_path, self._level,
            )
            return {
                "success": True,
                "level": self._level,
                "signal_rate": self._last_signal_rate,
                "path": str(yaml_path),
            }

        except Exception as exc:
            logger.error("Tuner: failed to save to YAML: {}", exc)
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Status for GUI
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Return current tuner status for GUI/logging."""
        return {
            "enabled": self._enabled,
            "level": self._level,
            "max_level": self.MAX_LEVEL,
            "eval_count": self._eval_count,
            "signal_count": self._signal_count,
            "hold_count": self._hold_count,
            "signal_rate": round(
                self._signal_count / self._eval_count
                if self._eval_count > 0 else self._last_signal_rate,
                4,
            ),
            "last_signal_rate": round(self._last_signal_rate, 4),
            "stable_streak": self._stable_streak,
            "yaml_proposed": self._yaml_proposed,
            "tune_history": self._tune_history[-10:],
            "current_params": self._get_current_level_params(),
        }

    def _get_current_level_params(self) -> Dict[str, Any]:
        """Return the parameter values for the current level (for display)."""
        level_params = {
            0: {"desc": "Original config", "rsi_long": self._original_params["rsi_long_range"], "rsi_short": self._original_params["rsi_short_range"], "vol_mult": self._original_params["volume_multiplier"], "vwap": self._original_params["vwap_enabled"], "htf": self._original_params["higher_tf_enabled"], "ema": "strict"},
            1: {"desc": "Widen RSI + lower vol", "rsi_long": [35, 82], "rsi_short": [18, 65], "vol_mult": 0.8, "vwap": True, "htf": True, "ema": "strict"},
            2: {"desc": "VWAP off", "rsi_long": [30, 85], "rsi_short": [15, 70], "vol_mult": 0.7, "vwap": False, "htf": True, "ema": "strict"},
            3: {"desc": "HTF off", "rsi_long": [25, 85], "rsi_short": [15, 75], "vol_mult": 0.6, "vwap": False, "htf": False, "ema": "strict"},
            4: {"desc": "Very relaxed", "rsi_long": [20, 88], "rsi_short": [12, 80], "vol_mult": 0.5, "vwap": False, "htf": False, "ema": "strict"},
            5: {"desc": "EMA relaxed", "rsi_long": [20, 88], "rsi_short": [12, 80], "vol_mult": 0.4, "vwap": False, "htf": False, "ema": "relaxed"},
            6: {"desc": "Maximum", "rsi_long": [15, 90], "rsi_short": [10, 85], "vol_mult": 0.3, "vwap": False, "htf": False, "ema": "minimal"},
        }
        return level_params.get(self._level, level_params[0])
