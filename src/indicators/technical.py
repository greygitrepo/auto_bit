"""Technical indicator engine using the ``ta`` library.

Calculates all technical indicators required by the strategy pipeline.
Takes pandas DataFrames with OHLCV data and returns indicator values.

Tasks D-06 (core indicators) and D-07 (additional indicators).
"""

from __future__ import annotations

import pandas as pd
import ta as ta_lib


class IndicatorEngine:
    """Technical indicator calculator using the *ta* library.

    All methods are static and operate on pandas DataFrames/Series.
    Insufficient data for a given indicator window will produce NaN values
    rather than raising exceptions.
    """

    # ------------------------------------------------------------------
    # Public aggregate method
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_all(
        df: pd.DataFrame, include_trend: bool = False
    ) -> pd.DataFrame:
        """Calculate all indicators on an OHLCV DataFrame.

        Args:
            df: DataFrame with columns [open, high, low, close, volume, timestamp].
            include_trend: If ``True``, also calculate EMA-50 and EMA-200
                (intended for BTC/ETH 1-hour trend analysis).

        Returns:
            Copy of *df* with all indicator columns appended.
        """
        result = df.copy()

        # --- D-06 core indicators ---
        result["ema_5"] = IndicatorEngine.calculate_ema(result["close"], 5)
        result["ema_10"] = IndicatorEngine.calculate_ema(result["close"], 10)
        result["ema_20"] = IndicatorEngine.calculate_ema(result["close"], 20)

        if include_trend:
            result["ema_50"] = IndicatorEngine.calculate_ema(result["close"], 50)
            result["ema_200"] = IndicatorEngine.calculate_ema(result["close"], 200)

        result["rsi_14"] = IndicatorEngine.calculate_rsi(result)
        result["vwap"] = IndicatorEngine.calculate_vwap(result)
        result["atr_14"] = IndicatorEngine.calculate_atr(result)

        # --- D-07 additional indicators ---
        bb = IndicatorEngine.calculate_bollinger(result)
        for col in ("bb_upper", "bb_mid", "bb_lower", "bb_width"):
            result[col] = bb[col]

        vol = IndicatorEngine.calculate_volume_indicators(result)
        for col in ("vol_ma5", "vol_ma20", "vol_ratio"):
            result[col] = vol[col]

        # --- ADX (trend strength) ---
        result["adx_14"] = IndicatorEngine.calculate_adx(result)

        return result

    # ------------------------------------------------------------------
    # Individual indicator methods
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_ema(series: pd.Series, period: int) -> pd.Series:
        """Calculate Exponential Moving Average."""
        if len(series) < period:
            return pd.Series(float("nan"), index=series.index)
        return ta_lib.trend.ema_indicator(series, window=period)

    @staticmethod
    def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Relative Strength Index (0-100)."""
        if len(df) < period + 1:
            return pd.Series(float("nan"), index=df.index)
        return ta_lib.momentum.rsi(df["close"], window=period)

    @staticmethod
    def calculate_vwap(df: pd.DataFrame) -> pd.Series:
        """Calculate Volume-Weighted Average Price with intraday reset."""
        if df.empty:
            return pd.Series(dtype=float)

        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0

        if "timestamp" in df.columns:
            try:
                ts = pd.to_datetime(df["timestamp"], utc=True)
                day = ts.dt.date
            except Exception:
                day = pd.Series(0, index=df.index)
        else:
            day = pd.Series(0, index=df.index)

        tp_vol = typical_price * df["volume"]
        cum_tp_vol = tp_vol.groupby(day).cumsum()
        cum_vol = df["volume"].groupby(day).cumsum()
        vwap = cum_tp_vol / cum_vol.replace(0, float("nan"))
        return vwap

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average True Range."""
        if len(df) < period:
            return pd.Series(float("nan"), index=df.index)
        return ta_lib.volatility.average_true_range(
            df["high"], df["low"], df["close"], window=period
        )

    @staticmethod
    def calculate_bollinger(
        df: pd.DataFrame, period: int = 20, std: float = 2.0
    ) -> pd.DataFrame:
        """Calculate Bollinger Bands.

        Returns:
            DataFrame with columns bb_upper, bb_mid, bb_lower, bb_width.
        """
        nan_df = pd.DataFrame(
            {"bb_upper": float("nan"), "bb_mid": float("nan"),
             "bb_lower": float("nan"), "bb_width": float("nan")},
            index=df.index,
        )
        if len(df) < period:
            return nan_df

        bb = ta_lib.volatility.BollingerBands(
            df["close"], window=period, window_dev=std
        )
        result = pd.DataFrame(index=df.index)
        result["bb_upper"] = bb.bollinger_hband()
        result["bb_mid"] = bb.bollinger_mavg()
        result["bb_lower"] = bb.bollinger_lband()
        mid = result["bb_mid"]
        result["bb_width"] = (
            (result["bb_upper"] - result["bb_lower"])
            / mid.replace(0, float("nan"))
        )
        return result

    @staticmethod
    def calculate_volume_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """Calculate volume MA(5), MA(20), and volume ratio."""
        result = pd.DataFrame(index=df.index)
        vol = df["volume"]

        result["vol_ma5"] = (
            vol.rolling(window=5).mean() if len(vol) >= 5
            else pd.Series(float("nan"), index=df.index)
        )
        result["vol_ma20"] = (
            vol.rolling(window=20).mean() if len(vol) >= 20
            else pd.Series(float("nan"), index=df.index)
        )
        result["vol_ratio"] = vol / result["vol_ma5"].replace(0, float("nan"))
        return result

    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average Directional Index (trend strength, 0-100)."""
        if len(df) < period * 2:
            return pd.Series(float("nan"), index=df.index)
        return ta_lib.trend.adx(df["high"], df["low"], df["close"], window=period)

    # ------------------------------------------------------------------
    # Trend / alignment helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_ema_alignment(
        df: pd.DataFrame,
        fast: int = 20,
        mid: int = 50,
        slow: int = 200,
    ) -> str:
        """Check EMA alignment for trend determination.

        Returns:
            ``"bullish"`` if fast > mid > slow,
            ``"bearish"`` if fast < mid < slow,
            ``"neutral"`` otherwise.
        """
        if len(df) < slow:
            return "neutral"

        ema_fast = IndicatorEngine.calculate_ema(df["close"], fast)
        ema_mid = IndicatorEngine.calculate_ema(df["close"], mid)
        ema_slow = IndicatorEngine.calculate_ema(df["close"], slow)

        f_val = ema_fast.iloc[-1]
        m_val = ema_mid.iloc[-1]
        s_val = ema_slow.iloc[-1]

        if pd.isna(f_val) or pd.isna(m_val) or pd.isna(s_val):
            return "neutral"

        if f_val > m_val > s_val:
            return "bullish"
        if f_val < m_val < s_val:
            return "bearish"
        return "neutral"

    @staticmethod
    def get_market_trend(
        btc_df: pd.DataFrame, eth_df: pd.DataFrame
    ) -> str:
        """Determine overall market trend from BTC and ETH 1-hour data.

        Returns:
            ``"bull"``, ``"bear"``, or ``"mixed"``.
        """
        btc_trend = IndicatorEngine.get_ema_alignment(btc_df)
        eth_trend = IndicatorEngine.get_ema_alignment(eth_df)

        if btc_trend == "bullish" and eth_trend == "bullish":
            return "bull"
        if btc_trend == "bearish" and eth_trend == "bearish":
            return "bear"
        return "mixed"
