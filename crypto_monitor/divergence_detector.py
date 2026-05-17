# -*- coding: utf-8 -*-
"""
背离检测引擎 —— 捕捉价格与技术指标之间的背离，提前预警趋势衰竭或反转。

检测维度:
  1. RSI 顶背离: 价格新高 + RSI 未新高 → 上涨动能衰减
  2. RSI 底背离: 价格新低 + RSI 未新低 → 下跌动能衰竭
  3. 量价背离: 价格上涨 + 成交量递减 → 买盘减弱，警惕假突破
  4. OI-价格背离: 价格上涨 + OI 下降 → 空头平仓推动，非真实买盘
  5. 隐藏背离: 趋势中继信号

所有检测基于多周期确认，单一周期背离弱于多周期共振。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .indicators import rsi as calc_rsi, safe_div
from .models import Candle, IndicatorSnapshot


@dataclass
class DivergenceSignal:
    symbol: str
    div_type: str           # RSI_BEAR / RSI_BULL / VOL_BEAR / OI_BEAR / HIDDEN_BULL / HIDDEN_BEAR
    timeframe: str          # 5m / 15m / 1h
    strength: float         # 0-100
    description: str
    is_warning: bool = True # True=风险警告, False=机会提示


def _find_swing_points(values: List[float], window: int = 5, min_distance: int = 3) -> List[Tuple[int, float]]:
    """找出序列中的局部极值点。返回 [(index, value), ...]。"""
    if len(values) < window * 2:
        return []
    points = []
    for i in range(window, len(values) - window):
        left = values[i - window:i]
        right = values[i + 1:i + window + 1]
        is_high = values[i] > max(left) and values[i] > max(right)
        is_low = values[i] < min(left) and values[i] < min(right)
        if is_high or is_low:
            # 过滤过于接近的点
            if not points or abs(i - points[-1][0]) >= min_distance:
                points.append((i, values[i]))
    return points


def detect_rsi_divergence(
    candles: List[Candle],
    timeframe: str,
    rsi_period: int = 14,
    lookback: int = 50,
) -> List[DivergenceSignal]:
    """
    检测 RSI 背离。

    顶背离: price[-1] > price[prev_high] 但 rsi[-1] < rsi[prev_high] → 看跌
    底背离: price[-1] < price[prev_low] 但 rsi[-1] > rsi[prev_low] → 看涨
    """
    if len(candles) < lookback:
        return []

    closes = [c.close for c in candles]
    rsi_values = []
    for i in range(rsi_period, len(closes) + 1):
        rsi_values.append(calc_rsi(closes[:i], rsi_period))

    if len(rsi_values) < 20:
        return []

    # 找价格和RSI的局部极值
    price_swings = _find_swing_points(closes[-lookback:], window=6, min_distance=4)
    rsi_swings = _find_swing_points(rsi_values[-lookback:], window=5, min_distance=4)

    results = []
    recent_close = closes[-1]
    recent_rsi = rsi_values[-1]

    # 价格高点: window 内的 idx 需映射到 rsi_values
    # window_start = len(closes) - lookback
    # absolute_idx = window_start + swing_idx_in_window
    # rsi_idx = absolute_idx - 14 (因为 rsi_values[k] 对应 closes[:k+14])
    window_offset = len(closes) - lookback

    def _to_rsi_idx(swing_window_idx: int) -> int:
        absolute = window_offset + swing_window_idx
        rsi_idx = absolute - 14
        return max(0, min(rsi_idx, len(rsi_values) - 1))

    # 价格高点
    price_highs = [(i, v) for i, v in price_swings if v > recent_close * 0.98]
    if price_highs:
        prev_high = price_highs[-1]
        rsi_idx = _to_rsi_idx(prev_high[0])
        rsi_at_prev_high = rsi_values[rsi_idx]

        if recent_close > prev_high[1] and recent_rsi < rsi_at_prev_high - 3:
            strength = min(100, abs(rsi_at_prev_high - recent_rsi) * 4 + 20)
            results.append(DivergenceSignal(
                symbol="", timeframe=timeframe, div_type="RSI_BEAR",
                strength=round(strength, 1),
                description=f"{timeframe} RSI顶背离: 价格创新高但RSI未确认(前{rsi_at_prev_high:.0f}→现{recent_rsi:.0f})，上涨动能衰减",
                is_warning=True,
            ))

    # 价格低点
    price_lows = [(i, v) for i, v in price_swings if v < recent_close * 1.02]
    if price_lows:
        prev_low = price_lows[-1]
        rsi_idx = _to_rsi_idx(prev_low[0])
        rsi_at_prev_low = rsi_values[rsi_idx]

        if recent_close < prev_low[1] and recent_rsi > rsi_at_prev_low + 3:
            strength = min(100, abs(recent_rsi - rsi_at_prev_low) * 4 + 20)
            results.append(DivergenceSignal(
                symbol="", timeframe=timeframe, div_type="RSI_BULL",
                strength=round(strength, 1),
                description=f"{timeframe} RSI底背离: 价格创新低但RSI拒绝新低(前{rsi_at_prev_low:.0f}→现{recent_rsi:.0f})，下跌动能衰竭",
                is_warning=False,
            ))

    return results


def detect_volume_divergence(
    candles: List[Candle],
    timeframe: str,
    lookback: int = 20,
) -> Optional[DivergenceSignal]:
    """
    检测量价背离。

    价格在涨但成交量在萎缩 → 买盘支撑减弱，假突破风险。
    """
    if len(candles) < lookback + 6:
        return None

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]

    # 最近两段：前半段 vs 后半段
    half = lookback // 2
    first_close_avg = sum(closes[-(lookback):-half]) / half
    second_close_avg = sum(closes[-half:]) / half
    first_vol_avg = sum(volumes[-(lookback):-half]) / half
    second_vol_avg = sum(volumes[-half:]) / half

    price_up = second_close_avg > first_close_avg * 1.01  # 价格涨 >1%
    vol_down = second_vol_avg < first_vol_avg * 0.85       # 量缩 >15%

    if price_up and vol_down:
        vol_decline_pct = safe_div(first_vol_avg - second_vol_avg, first_vol_avg) * 100
        strength = min(100, vol_decline_pct * 2.5 + 15)
        return DivergenceSignal(
            symbol="", timeframe=timeframe, div_type="VOL_BEAR",
            strength=round(strength, 1),
            description=f"{timeframe} 量价背离: 价格上行但成交量萎缩{vol_decline_pct:.0f}%，买盘支撑减弱",
            is_warning=True,
        )

    return None


def detect_oi_divergence(
    price_change_pct: float,
    oi_change_pct: Optional[float],
    timeframe: str,
) -> Optional[DivergenceSignal]:
    """
    检测持仓量-价格背离。

    OI-价格同向 = 趋势健康（多头建仓推动上涨 / 空头建仓推动下跌）
    OI-价格背离 = 趋势不健康:
      - 价格上涨 + OI 下降 → 空头平仓(short covering)推动，非真实买盘
      - 价格下跌 + OI 上升 → 空头加仓打压
    """
    if oi_change_pct is None:
        return None

    # 价涨 + OI降 = 空头回补，虚涨
    if price_change_pct > 2.0 and oi_change_pct < -5.0:
        strength = min(100, abs(oi_change_pct) * 1.0 + abs(price_change_pct) * 3)
        return DivergenceSignal(
            symbol="", timeframe=timeframe, div_type="OI_BEAR",
            strength=round(strength, 1),
            description=f"价涨+{price_change_pct:.1f}%但OI降{oi_change_pct:.1f}%: 空头回补推动，非增量买盘",
            is_warning=True,
        )

    # 价涨 + OI 暴增 = 逼空（这是正面信号，对应 SHORT_SQUEEZE）
    if price_change_pct > 3.0 and oi_change_pct > 50:
        return DivergenceSignal(
            symbol="", timeframe=timeframe, div_type="OI_BEAR",
            strength=75.0,
            description=f"价涨{price_change_pct:.1f}% + OI暴增{oi_change_pct:.0f}%: 逼空行情，注意反转风险",
            is_warning=True,
        )

    return None


def comprehensive_divergence_check(
    symbol: str,
    candles_map: Dict[str, List[Candle]],
    indicators: Dict[str, IndicatorSnapshot],
    oi_change_pct: Optional[float],
) -> List[DivergenceSignal]:
    """
    综合背离检测 —— 多周期并行。

    返回所有检测到的背离信号，按强度排序。
    多周期共振的背离信号强度加权提升。
    """
    all_signals: List[DivergenceSignal] = []

    for tf in ("5m", "15m", "1h"):
        candles = candles_map.get(tf)
        if not candles:
            continue
        # RSI 背离
        rsi_divs = detect_rsi_divergence(candles, tf)
        for d in rsi_divs:
            d.symbol = symbol
        all_signals.extend(rsi_divs)

        # 量价背离
        vol_div = detect_volume_divergence(candles, tf)
        if vol_div:
            vol_div.symbol = symbol
            all_signals.append(vol_div)

    # OI 背离（用15m涨幅）
    i15 = indicators.get("15m")
    if i15:
        oi_div = detect_oi_divergence(i15.change_pct, oi_change_pct, "15m")
        if oi_div:
            oi_div.symbol = symbol
            all_signals.append(oi_div)

    # ── 共振加权 ──
    # 同一类型的背离在多个周期出现，强度提升
    type_counts: Dict[str, int] = {}
    for s in all_signals:
        type_counts[s.div_type] = type_counts.get(s.div_type, 0) + 1

    for s in all_signals:
        if type_counts[s.div_type] >= 2:
            s.strength = min(100, s.strength * 1.3)
            s.description += " [多周期共振]"

    all_signals.sort(key=lambda x: x.strength, reverse=True)
    return all_signals
