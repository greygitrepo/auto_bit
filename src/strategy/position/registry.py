"""Strategy plugin registry — maps strategy names to classes.

New strategies are registered here. P2 uses this to instantiate
the correct strategy based on config.
"""

# Registry dicts populated by imports below
POSITION_STRATEGIES = {}   # name → class (returns PositionSignal)
GRID_STRATEGIES = {}       # name → class (returns List[GridSignal])

def register_position(name):
    """Decorator to register a single-position strategy."""
    def wrapper(cls):
        POSITION_STRATEGIES[name] = cls
        return cls
    return wrapper

def register_grid(name):
    """Decorator to register a grid strategy."""
    def wrapper(cls):
        GRID_STRATEGIES[name] = cls
        return cls
    return wrapper

def get_strategy_class(name):
    """Look up strategy by name. Returns (cls, is_grid)."""
    if name in GRID_STRATEGIES:
        return GRID_STRATEGIES[name], True
    if name in POSITION_STRATEGIES:
        return POSITION_STRATEGIES[name], False
    return None, False

def list_strategies():
    """Return all registered strategy names."""
    return {
        "position": list(POSITION_STRATEGIES.keys()),
        "grid": list(GRID_STRATEGIES.keys()),
    }

_loaded = False

def ensure_loaded():
    """Explicitly import all strategy modules to trigger @register decorators.

    Called once by P2 before strategy lookup. Avoids circular imports
    at module load time by deferring imports until needed.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    from src.strategy.position.momentum_scalper import MomentumScalper  # noqa
    from src.strategy.position.grid_bias import GridBiasStrategy  # noqa
    from src.strategy.position.breakout_scalper import BreakoutScalper  # noqa
    from src.strategy.position.rsi_reversal import RSIReversal  # noqa
    from src.strategy.position.ema_crossover import EMACrossover  # noqa
    from src.strategy.position.volatility_breakout import VolatilityBreakout  # noqa
