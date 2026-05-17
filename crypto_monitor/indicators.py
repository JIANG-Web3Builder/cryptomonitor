# -*- coding: utf-8 -*-

from typing import Dict, List, Optional

from .models import Candle, IndicatorSnapshot


def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


def safe_div(value: float, divisor: float, default: float = 0.0) -> float:
    if divisor == 0:
        return default
    return value / divisor


def ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * alpha + result * (1 - alpha)
    return result


def rsi(values: List[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    true_ranges = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    window = true_ranges[-period:] if len(true_ranges) >= period else true_ranges
    if not window:
        return 0.0
    return sum(window) / len(window)


def volume_spike(candles: List[Candle], recent: int = 3, baseline: int = 30) -> float:
    if len(candles) < recent + 2:
        return 1.0
    recent_values = [candle.volume for candle in candles[-recent:]]
    baseline_values = [candle.volume for candle in candles[-(baseline + recent):-recent]]
    if not baseline_values:
        baseline_values = [candle.volume for candle in candles[:-recent]]
    recent_avg = sum(recent_values) / len(recent_values)
    baseline_avg = sum(baseline_values) / len(baseline_values) if baseline_values else recent_avg
    return safe_div(recent_avg, baseline_avg, 1.0)


def high_breakout_pct(candles: List[Candle], lookback: int = 40) -> float:
    if len(candles) < 3:
        return 0.0
    current = candles[-1].close
    previous_highs = [candle.high for candle in candles[-(lookback + 1):-1]]
    if not previous_highs:
        return 0.0
    return pct_change(max(previous_highs), current)


def low_distance_pct(candles: List[Candle], lookback: int = 40) -> float:
    if not candles:
        return 0.0
    current = candles[-1].close
    lows = [candle.low for candle in candles[-lookback:]]
    if not lows:
        return 0.0
    return pct_change(min(lows), current)


def trend_slope_pct(values: List[float], lookback: int = 20) -> float:
    if len(values) < 2:
        return 0.0
    window = values[-lookback:] if len(values) >= lookback else values
    return pct_change(window[0], window[-1])


def ema_series(values: List[float], period: int) -> List[float]:
    """返回 EMA 序列，最后一个值是最新的 EMA。"""
    if len(values) < period:
        return []
    alpha = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * alpha + result[-1] * (1 - alpha))
    return result


def slope_pct_per_bar(series: List[float], lookback: int = 5) -> float:
    """计算序列最近 lookback 根 bar 的平均斜率（%/bar）。"""
    if len(series) < lookback + 1:
        return 0.0
    recent = series[-lookback:]
    first = recent[0]
    if first == 0:
        return 0.0
    total_pct = (recent[-1] - first) / first * 100.0
    return total_pct / lookback


def ma_alignment_score(closes: List[float], ema7_series: List[float], ema20_series: List[float], ema50_series: List[float]) -> float:
    """
    评估均线多头排列程度 (0-100)。

    检查三个维度:
      1. 排列顺序: EMA7 > EMA20 > EMA50 且价格在 EMA7 上方
      2. 均线斜率: 三根 MA 的斜率都为正（向上）
      3. 发散程度: MA 之间的距离在扩大（加速上涨）

    返回综合评分。
    """
    if len(closes) < 50 or len(ema7_series) < 3 or len(ema20_series) < 3 or len(ema50_series) < 3:
        return 0.0

    price = closes[-1]
    ema7 = ema7_series[-1]
    ema20 = ema20_series[-1]
    ema50 = ema50_series[-1]

    if ema7 <= 0 or ema20 <= 0 or ema50 <= 0:
        return 0.0

    score = 0.0

    # 1. 排列顺序 (0-40)
    if price > ema7 > ema20 > ema50:
        score += 40
    elif price > ema7 > ema20:
        score += 28
    elif price > ema7 and ema20 > ema50:
        score += 18
    elif price > ema20 and ema20 > ema50:
        score += 12

    # 2. 均线斜率 (0-35)
    s7 = slope_pct_per_bar(ema7_series, 5)
    s20 = slope_pct_per_bar(ema20_series, 8)
    s50 = slope_pct_per_bar(ema50_series, 12)

    up_count = sum(1 for s in (s7, s20, s50) if s > 0.05)
    if up_count == 3:
        score += 35
    elif up_count == 2:
        score += 22
    elif up_count == 1:
        score += 10

    # 3. 发散程度 (0-25) — EMA7 与 EMA20 的差距是否在扩大
    if s7 > s20 > s50 and s7 > 0:
        divergence_boost = min(25, max(0, (s7 - s20) * 60))
        score += divergence_boost
    elif s7 > s20:
        score += min(15, max(0, (s7 - s20) * 40))

    return min(100.0, score)


def sustained_momentum_score(candles: List, min_consecutive: int = 4) -> float:
    """
    评估价格的持续推动力 (0-100)。

    维度:
      1. 连续阳线数量及比例
      2. 阳线量能是否大于阴线量能
      3. 最近阳线的实体是否在放大
      4. 回调幅度是否小（健康上涨的特征）
    """
    if len(candles) < min_consecutive + 6:
        return 0.0

    closes = [c.close for c in candles]
    opens = [c.open for c in candles]
    volumes = [c.volume for c in candles]

    score = 0.0

    # 1. 连续阳线 (0-30)
    con_bull = 0
    for i in range(len(candles) - 1, -1, -1):
        if closes[i] > opens[i]:
            con_bull += 1
        else:
            break

    if con_bull >= 6:
        score += 30
    elif con_bull >= 4:
        score += 22
    elif con_bull >= 3:
        score += 14
    elif con_bull >= 2:
        score += 6

    # 2. 阳线/阴线量能比 (0-30)
    recent = candles[-12:]
    bull_vols = [c.volume for c in recent if c.close > c.open]
    bear_vols = [c.volume for c in recent if c.close < c.open]
    if bull_vols and bear_vols:
        avg_bull_vol = sum(bull_vols) / len(bull_vols)
        avg_bear_vol = sum(bear_vols) / len(bear_vols)
        ratio = safe_div(avg_bull_vol, avg_bear_vol, 0)
        if ratio >= 2.0:
            score += 30
        elif ratio >= 1.5:
            score += 22
        elif ratio >= 1.2:
            score += 14
        elif ratio >= 1.0:
            score += 6
    elif bull_vols and not bear_vols:
        score += 30

    # 3. 阳线实体放大趋势 (0-20)
    bull_bodies = []
    for c in recent:
        if c.close > c.open:
            bull_bodies.append((c.close - c.open) / c.open * 100.0)

    if len(bull_bodies) >= 3:
        half = len(bull_bodies) // 2
        first_half_avg = sum(bull_bodies[:half]) / half if bull_bodies[:half] else 0
        second_half_avg = sum(bull_bodies[half:]) / max(1, len(bull_bodies[half:]))
        if second_half_avg > first_half_avg * 1.3:
            score += 20
        elif second_half_avg > first_half_avg:
            score += 10
    elif len(bull_bodies) >= 1:
        score += 5

    # 4. 回调幅度小 (0-20)
    recent_closes = closes[-8:]
    max_close = max(recent_closes)
    min_since_peak = min(recent_closes[recent_closes.index(max_close):]) if max_close in recent_closes else min(recent_closes)
    drawdown = safe_div(max_close - min_since_peak, max_close) * 100.0
    if drawdown <= 1.0:
        score += 20
    elif drawdown <= 2.5:
        score += 14
    elif drawdown <= 5.0:
        score += 6

    return min(100.0, score)


def volume_profile_score(candles: List) -> float:
    """
    评估成交量特征 (0-100)。

    维度:
      1. 相对基线的放量程度
      2. 量能是否在递增（趋势向上）
      3. 最新量是否异常突出
    """
    if len(candles) < 20:
        return 0.0

    vols = [c.volume for c in candles]

    score = 0.0

    # 1. 放量倍数 (0-40)
    recent_vols = vols[-5:]
    baseline_vols = vols[-25:-5]
    if baseline_vols:
        avg_recent = sum(recent_vols) / len(recent_vols)
        avg_baseline = sum(baseline_vols) / len(baseline_vols)
        spike = safe_div(avg_recent, avg_baseline, 1.0)
        if spike >= 5.0:
            score += 40
        elif spike >= 3.0:
            score += 30
        elif spike >= 2.0:
            score += 20
        elif spike >= 1.5:
            score += 10
        elif spike >= 1.2:
            score += 4

    # 2. 量能递增 (0-35)
    if len(vols) >= 12:
        segments = [vols[i:i+4] for i in range(0, 12, 4)]
        if len(segments) >= 3:
            avgs = [sum(s) / len(s) for s in segments]
            inc_count = sum(1 for j in range(1, len(avgs)) if avgs[j] > avgs[j-1] * 1.05)
            if inc_count == len(avgs) - 1:
                score += 35
            elif inc_count >= 2:
                score += 22
            elif inc_count >= 1:
                score += 10

    # 3. 最新量峰值 (0-25)
    latest_vol = vols[-1]
    avg_vol = sum(vols[-20:]) / 20
    peak_ratio = safe_div(latest_vol, avg_vol, 1.0)
    if peak_ratio >= 4.0:
        score += 25
    elif peak_ratio >= 2.5:
        score += 16
    elif peak_ratio >= 1.5:
        score += 8

    return min(100.0, score)


def oi_surge_level(oi_change_pct: Optional[float]) -> int:
    """返回 OI 爆发等级: 0=无, 1=显著(50%+), 2=爆发(100%+), 3=极限(200%+/3X)。"""
    if oi_change_pct is None:
        return 0
    if oi_change_pct >= 200:
        return 3
    if oi_change_pct >= 100:
        return 2
    if oi_change_pct >= 50:
        return 1
    return 0


def price_surge_level(change_pct: float, tier1: float = 5.0, tier2: float = 10.0, tier3: float = 15.0) -> int:
    """返回价格涨幅等级: 0=无, 1=显著, 2=强力, 3=爆发。"""
    if change_pct >= tier3:
        return 3
    if change_pct >= tier2:
        return 2
    if change_pct >= tier1:
        return 1
    return 0


def build_indicator_snapshot(timeframe: str, candles: List[Candle]) -> Optional[IndicatorSnapshot]:
    if len(candles) < 30:
        return None
    closes = [candle.close for candle in candles]
    close = closes[-1]
    ema20_val = ema(closes[-60:], 20)
    ema50_val = ema(closes[-80:], 50)
    ema7_val = ema(closes[-40:], 7)
    atr14 = atr(candles, 14)

    # 新指标
    ema7_series = ema_series(closes[-60:], 7) if len(closes) >= 60 else ema_series(closes, 7)
    ema20_series = ema_series(closes[-60:], 20) if len(closes) >= 60 else ema_series(closes, 20)
    ema50_series = ema_series(closes[-80:], 50) if len(closes) >= 80 else ema_series(closes, 50)
    ma_align = ma_alignment_score(closes, ema7_series, ema20_series, ema50_series)
    sus_momentum = sustained_momentum_score(candles)
    vol_profile = volume_profile_score(candles)

    # 计算连续阳线
    con_bull = 0
    for i in range(len(candles) - 1, -1, -1):
        if candles[i].close > candles[i].open:
            con_bull += 1
        else:
            break

    # 阳线/阴线量比
    recent12 = candles[-12:] if len(candles) >= 12 else candles
    bull_vols = [c.volume for c in recent12 if c.close > c.open]
    bear_vols = [c.volume for c in recent12 if c.close < c.open]
    if bull_vols and bear_vols:
        bull_vol_ratio = (sum(bull_vols) / len(bull_vols)) / (sum(bear_vols) / len(bear_vols))
    elif bull_vols:
        bull_vol_ratio = 3.0
    else:
        bull_vol_ratio = 0.5

    return IndicatorSnapshot(
        timeframe=timeframe,
        close=close,
        change_pct=pct_change(closes[-2], close) if len(closes) >= 2 else 0.0,
        ema20=ema20_val,
        ema50=ema50_val,
        rsi14=rsi(closes, 14),
        atr14=atr14,
        atr_pct=safe_div(atr14, close) * 100.0,
        volume_spike=volume_spike(candles),
        trend_slope_pct=trend_slope_pct(closes, 20),
        high_breakout_pct=high_breakout_pct(candles),
        low_distance_pct=low_distance_pct(candles),
        ema7=ema7_val,
        ma_alignment_score=round(ma_align, 2),
        sustained_momentum_score=round(sus_momentum, 2),
        volume_profile_score=round(vol_profile, 2),
        consecutive_bull=con_bull,
        bull_volume_ratio=round(bull_vol_ratio, 2),
    )


def build_indicator_map(candles_by_timeframe: Dict[str, List[Candle]]) -> Dict[str, IndicatorSnapshot]:
    snapshots = {}
    for timeframe, candles in candles_by_timeframe.items():
        snapshot = build_indicator_snapshot(timeframe, candles)
        if snapshot:
            snapshots[timeframe] = snapshot
    return snapshots
