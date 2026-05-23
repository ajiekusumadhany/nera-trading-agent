"""
market_context.py - Market Context Engine for NERA QUANT Decision Intelligence System

Provides stateless classifier functions:
  - Trading session detection (ASIA / LONDON / NY / OFF)
  - Volatility classification (LOW / MEDIUM / HIGH / EXTREME)
  - Market regime detection (TRENDING_BULL / TRENDING_BEAR / RANGING / CHOPPY)
"""

from datetime import datetime, timezone
from typing import Optional


# ── Session Windows (UTC hours, inclusive start, exclusive end) ────────
_SESSIONS = {
    'LONDON': (7, 16),   # 07:00–15:59 UTC
    'NY':     (13, 22),  # 13:00–21:59 UTC
    'ASIA':   (0,  8),   # 00:00–07:59 UTC
}


def get_session(utc_hour: Optional[int] = None) -> str:
    """
    Classify trading session based on UTC hour.
    If utc_hour is None, uses current UTC time.
    Returns: 'LONDON' | 'NY' | 'ASIA' | 'OFF'
    Note: LONDON/NY overlap (13–15 UTC) is classified as 'LONDON' (priority order).
    """
    if utc_hour is None:
        utc_hour = datetime.now(timezone.utc).hour

    # Priority order: LONDON > NY > ASIA
    for name, (start, end) in _SESSIONS.items():
        if start <= utc_hour < end:
            return name
    return 'OFF'


def get_session_and_meta(dt: Optional[datetime] = None):
    """
    Returns (session, hour_utc, weekday) from a datetime.
    weekday: 0=Monday … 6=Sunday
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    hour    = dt.hour
    weekday = dt.weekday()
    session = get_session(hour)
    return session, hour, weekday


def classify_volatility(atr_pct: float) -> str:
    """
    Classify volatility based on ATR as % of price.
    Returns: 'LOW' | 'MEDIUM' | 'HIGH' | 'EXTREME'
    """
    if atr_pct < 0.005:    # < 0.5%
        return 'LOW'
    elif atr_pct < 0.012:  # 0.5%–1.2%
        return 'MEDIUM'
    elif atr_pct < 0.025:  # 1.2%–2.5%
        return 'HIGH'
    else:                  # > 2.5%
        return 'EXTREME'


def get_market_regime(htf_features: Optional[dict]) -> str:
    """
    Classify market regime from Higher Timeframe features.
    Returns: 'TRENDING_BULL' | 'TRENDING_BEAR' | 'RANGING' | 'CHOPPY' | 'UNKNOWN'
    """
    if not htf_features:
        return 'UNKNOWN'

    ema_trend = htf_features.get('ema_trend', 0)
    bb_pct    = htf_features.get('bb_pct', 0.5)
    bos       = htf_features.get('bos', 0)
    choch     = htf_features.get('choch', 0)
    vol_ratio = htf_features.get('vol_ratio', 1.0)
    rsi       = htf_features.get('rsi', 50)

    # Strong trend: EMA aligned + BOS + volume confirmation
    if ema_trend == 1 and bos and vol_ratio > 1.2 and rsi > 50:
        return 'TRENDING_BULL'
    if ema_trend == -1 and bos and vol_ratio > 1.2 and rsi < 50:
        return 'TRENDING_BEAR'

    # CHoCH = possible reversal, market is transitioning
    if choch:
        return 'RANGING'

    # BB% near extremes without BOS = possible range
    if 0.2 < bb_pct < 0.8 and not bos:
        return 'RANGING'

    # Low volume, no structure = choppy
    if vol_ratio < 0.8 and not bos and not choch:
        return 'CHOPPY'

    return 'UNKNOWN'


def get_htf_bias(htf_features: Optional[dict]) -> str:
    """
    Get simple HTF directional bias from Higher Timeframe features.
    Returns: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
    """
    if not htf_features:
        return 'NEUTRAL'

    ema_trend   = htf_features.get('ema_trend', 0)
    above_ema50 = htf_features.get('above_ema50', 0)
    rsi         = htf_features.get('rsi', 50)
    macd_pos    = htf_features.get('macd_positive', 0)

    bullish_votes = sum([
        1 if ema_trend == 1 else 0,
        1 if above_ema50 else 0,
        1 if rsi > 52 else 0,
        1 if macd_pos else 0,
    ])
    bearish_votes = sum([
        1 if ema_trend == -1 else 0,
        1 if not above_ema50 else 0,
        1 if rsi < 48 else 0,
        1 if not macd_pos else 0,
    ])

    if bullish_votes >= 3:
        return 'BULLISH'
    if bearish_votes >= 3:
        return 'BEARISH'
    return 'NEUTRAL'


def is_weekend() -> bool:
    """True if current UTC time is Saturday or Sunday."""
    return datetime.now(timezone.utc).weekday() >= 5


def get_full_context(features: dict, htf_features: Optional[dict] = None, dt: Optional[datetime] = None) -> dict:
    """
    One-shot context snapshot — combines all classifiers.
    Returns a dict ready to pass to log_trade_open().
    """
    session, hour, weekday = get_session_and_meta(dt)
    htf_bias  = get_htf_bias(htf_features)
    atr_pct   = features.get('atr_pct', 0.0)
    volatility = classify_volatility(atr_pct)
    regime    = get_market_regime(htf_features)

    return {
        'session':    session,
        'hour_utc':   hour,
        'weekday':    weekday,
        'htf_bias':   htf_bias,
        'volatility': volatility,
        'regime':     regime,
    }
