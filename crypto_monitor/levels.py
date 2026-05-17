# -*- coding: utf-8 -*-
"""轻量级支撑/压力位计算，仅供预警上下文参考。"""

from typing import Dict, List

from .indicators import safe_div
from .models import Candle, ContextInfo, IndicatorSnapshot


def recent_support(candles: List[Candle], lookback: int = 50) -> float:
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if not window:
        return 0.0
    lows = sorted(candle.low for candle in window)
    index = max(0, int(len(lows) * 0.15) - 1)
    return lows[index]


def recent_resistance(candles: List[Candle], lookback: int = 80) -> float:
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if not window:
        return 0.0
    highs = sorted((candle.high for candle in window), reverse=True)
    index = max(0, int(len(highs) * 0.12) - 1)
    return highs[index]


def build_context(
    candles_by_timeframe: Dict[str, List[Candle]],
    indicators: Dict[str, IndicatorSnapshot],
) -> ContextInfo:
    primary = candles_by_timeframe.get("15m") or candles_by_timeframe.get("5m") or next(iter(candles_by_timeframe.values()))
    current = primary[-1].close if primary else 0.0
    indicator = indicators.get("15m") or indicators.get("5m") or next(iter(indicators.values()))

    support = recent_support(primary)
    resistance = recent_resistance(primary)
    atr_pct = indicator.atr_pct if indicator else 0.0
    ema20 = indicator.ema20 if indicator else current
    ema_ext = safe_div(current - ema20, ema20) * 100.0 if ema20 > 0 else 0.0

    return ContextInfo(
        support=support,
        resistance=resistance,
        atr_pct=round(atr_pct, 2),
        ema20=ema20,
        ema_extension_pct=round(ema_ext, 2),
    )
