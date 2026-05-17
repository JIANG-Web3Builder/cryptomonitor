# -*- coding: utf-8 -*-
"""
拉盘检测引擎 v2.0 —— 5m/15m 周期 + 均线对齐 + 持续动量 + OI 爆发。

核心理念:
  不再用 1m/3m 的微波动来做信号，而是专注 5m/15m 的持续性强涨。
  只有满足「多根均线一起上涨 + 持续阳线放量 + OI 确认」的形态才会触发。

检测维度:
  1. MA 对齐: EMA7 > EMA20 > EMA50 且三线斜率向上
  2. 持续动量: 连续阳线 + 阳线量 > 阴线量 + 阳线实体放大
  3. 量能特征: 相对基线的放量倍数 + 量能递增趋势
  4. OI 爆发: 50%/100%/200% 分级
  5. 价格爆发: 5%/10%/15% 分级
  6. 形态分类: MOMENTUM_SURGE / OI_EXPLOSION / BREAKOUT_SPIKE / STRONG_RUN
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import PUMP
from .indicators import pct_change, safe_div


# ── 形态枚举 ──────────────────────────────────────────────
PATTERN_MOMENTUM_SURGE = "MOMENTUM_SURGE"    # 持续动量推动，均线多头排列 + 连续阳线
PATTERN_OI_EXPLOSION = "OI_EXPLOSION"         # OI 暴涨 2X/3X+ 驱动价格
PATTERN_BREAKOUT_SPIKE = "BREAKOUT_SPIKE"     # 突破关键位 + 量价齐升
PATTERN_STRONG_RUN = "STRONG_RUN"             # 强势连阳但 OI 配合一般
PATTERN_SHORT_SQUEEZE = "SHORT_SQUEEZE"       # 负费率 + OI暴涨 = 逼空
PATTERN_NONE = "NONE"

# ── RSI 简单实现（不依赖 indicators 模块，保持轻量） ──────────


def _rsi_fast(values: List[float], period: int = 7) -> float:
    if len(values) <= period:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(values)):
        chg = values[i] - values[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(abs(min(chg, 0.0)))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    alpha = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * alpha + result[-1] * (1 - alpha))
    return result


def _slope_pct(series: List[float], lookback: int = 5) -> float:
    """最近 lookback 根 bar 的平均斜率 (%/bar)。"""
    if len(series) < lookback + 1:
        return 0.0
    recent = series[-lookback:]
    if recent[0] == 0:
        return 0.0
    return ((recent[-1] - recent[0]) / recent[0] * 100.0) / lookback


# ── MA 对齐评分（轻量版，直接用 K 线原始数据） ─────────────


def _ma_alignment_rapid(candles: List[Dict], min_consecutive: int = 24) -> Tuple[float, bool, str]:
    """
    对快速扫描的 K 线做 MA 对齐判断。

    返回 (评分0-100, 是否对齐, 描述)。
    """
    if len(candles) < min_consecutive:
        return 0.0, False, "K线不足"

    closes = [c["close"] for c in candles]
    ema7 = _ema(closes, 7)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)

    if len(ema7) < 3 or len(ema20) < 3 or len(ema50) < 3:
        return 0.0, False, "均线不足"

    e7, e20, e50 = ema7[-1], ema20[-1], ema50[-1]
    if e7 <= 0 or e20 <= 0 or e50 <= 0:
        return 0.0, False, "均线异常"

    score = 0.0

    # 1. 排列顺序
    price = closes[-1]
    if price > e7 > e20 > e50:
        score += 40
        order_ok = True
    elif price > e7 > e20:
        score += 25
        order_ok = True
    elif price > e20 and e20 > e50:
        score += 12
        order_ok = False
    else:
        order_ok = False

    # 2. 斜率
    s7 = _slope_pct(ema7, 5)
    s20 = _slope_pct(ema20, 8)
    s50 = _slope_pct(ema50, 12)

    up_count = sum(1 for s in (s7, s20, s50) if s > PUMP.ma_slope_min_pct)
    if up_count == 3:
        score += 35
    elif up_count == 2:
        score += 20
    elif up_count == 1:
        score += 8

    # 3. 发散
    if s7 > s20 > s50 and s7 > 0:
        score += min(25, max(0, (s7 - s20) * 50))
    elif s7 > s20:
        score += min(12, max(0, (s7 - s20) * 30))

    aligned = order_ok and up_count >= PUMP.ma_alignment_min
    desc = f"EMA7>{'EMA20>' if e7 > e20 else ''}{'EMA50' if e20 > e50 else ''} | slope:{up_count}/3"

    return min(100.0, score), aligned, desc


# ── 持续动量评分 ─────────────────────────────────────────


def _sustained_momentum_rapid(candles: List[Dict]) -> Tuple[float, int, float, str]:
    """
    评估持续动量。

    返回 (评分0-100, 连续阳线数, 阳/阴量比, 描述)。
    """
    if len(candles) < 8:
        return 0.0, 0, 1.0, "K线不足"

    closes = [c["close"] for c in candles]
    opens = [c["open"] for c in candles]
    vols = [c["volume"] for c in candles]

    # 连续阳线
    con_bull = 0
    for i in range(len(candles) - 1, -1, -1):
        if closes[i] > opens[i]:
            con_bull += 1
        else:
            break

    score = 0.0

    # 1. 连续性评分
    if con_bull >= 6:
        score += 30
    elif con_bull >= 4:
        score += 22
    elif con_bull >= 3:
        score += 14
    elif con_bull >= 2:
        score += 5

    # 2. 阳线/阴线量比
    recent = candles[-12:] if len(candles) >= 12 else candles
    bull_vols = [c["volume"] for c in recent if c["close"] > c["open"]]
    bear_vols = [c["volume"] for c in recent if c["close"] < c["open"]]

    if bull_vols and bear_vols:
        ratio = (sum(bull_vols) / len(bull_vols)) / (sum(bear_vols) / len(bear_vols))
    elif bull_vols:
        ratio = 3.0
    else:
        ratio = 0.5

    if ratio >= 2.0:
        score += 30
    elif ratio >= 1.5:
        score += 22
    elif ratio >= 1.2:
        score += 14
    elif ratio >= 1.0:
        score += 5

    # 3. 阳线实体放大趋势
    bull_bodies = []
    for c in recent:
        if c["close"] > c["open"]:
            bull_bodies.append((c["close"] - c["open"]) / c["open"] * 100.0)

    if len(bull_bodies) >= 3:
        half = len(bull_bodies) // 2
        first_avg = sum(bull_bodies[:half]) / half if bull_bodies[:half] else 0
        second_avg = sum(bull_bodies[half:]) / max(1, len(bull_bodies[half:]))
        if second_avg > first_avg * 1.4:
            score += 25
        elif second_avg > first_avg:
            score += 12

    # 4. 浅回调
    recent_closes = closes[-8:]
    peak = max(recent_closes)
    peak_idx = recent_closes.index(peak)
    trough = min(recent_closes[peak_idx:])
    drawdown = safe_div(peak - trough, peak) * 100.0
    if drawdown <= 1.5:
        score += 15
    elif drawdown <= 3.0:
        score += 8

    desc = f"连阳{con_bull} | 量比{ratio:.1f}"
    return min(100.0, score), con_bull, round(ratio, 2), desc


# ── 量能特征评分 ─────────────────────────────────────────


def _volume_profile_rapid(candles: List[Dict]) -> Tuple[float, float, bool, str]:
    """
    量能特征分析。

    返回 (评分0-100, 放量倍数, 量能是否递增中, 描述)。
    """
    if len(candles) < 20:
        return 0.0, 1.0, False, "K线不足"

    vols = [c["volume"] for c in candles]
    score = 0.0

    # 1. 放量倍数
    recent_vols = vols[-5:]
    baseline_vols = vols[-25:-5] if len(vols) >= 25 else vols[:-5]
    if baseline_vols:
        avg_recent = sum(recent_vols) / len(recent_vols)
        avg_baseline = sum(baseline_vols) / len(baseline_vols)
        spike = safe_div(avg_recent, avg_baseline, 1.0)
    else:
        spike = 1.0

    if spike >= 5.0:
        score += 40
    elif spike >= 3.0:
        score += 30
    elif spike >= 2.0:
        score += 20
    elif spike >= 1.5:
        score += 10

    # 2. 量能递增
    trending_up = False
    if len(vols) >= 12:
        segs = [sum(vols[i:i+4]) / 4 for i in range(0, 12, 4)]
        inc_count = sum(1 for j in range(1, len(segs)) if segs[j] > segs[j-1] * 1.05)
        if inc_count == len(segs) - 1:
            score += 35
            trending_up = True
        elif inc_count >= 2:
            score += 20
        elif inc_count >= 1:
            score += 8

    # 3. 最新峰值
    latest = vols[-1]
    avg_vol = sum(vols[-20:]) / 20
    peak_ratio = safe_div(latest, avg_vol, 1.0)
    if peak_ratio >= 4.0:
        score += 25
    elif peak_ratio >= 2.5:
        score += 15
    elif peak_ratio >= 1.5:
        score += 6

    desc = f"量能{spike:.1f}x | {'递增' if trending_up else '脉冲'}"
    return min(100.0, score), round(spike, 2), trending_up, desc


# ── 阶段与形态分类 ────────────────────────────────────────


def _classify_pattern(
    ma_score: float,
    ma_aligned: bool,
    momentum_score: float,
    con_bull: int,
    vol_score: float,
    vol_spike: float,
    price_level: int,
    oi_level: int,
    rsi_5m: float,
    rsi_15m: float,
    funding_rate: Optional[float] = None,
) -> str:
    """根据综合特征分类形态。"""

    # 逼空型 — 负费率 + OI暴涨 + 价格加速
    if (
        funding_rate is not None
        and funding_rate < -0.0005  # 负费率 -0.05%
        and oi_level >= 1
        and price_level >= 1
        and vol_spike >= 1.8
    ):
        return PATTERN_SHORT_SQUEEZE

    # OI 爆发型 — OI 2X+ 且价格在涨
    if oi_level >= 2 and price_level >= 1 and vol_spike >= 2.0:
        return PATTERN_OI_EXPLOSION

    # 突破型 — 量价齐升 + MA 对齐
    if ma_aligned and vol_spike >= 3.0 and price_level >= 1:
        return PATTERN_BREAKOUT_SPIKE

    # 动量持续型 — MA 对齐 + 连续阳线
    if ma_aligned and con_bull >= PUMP.min_consecutive_bull_5m and momentum_score >= 45:
        return PATTERN_MOMENTUM_SURGE

    # 强势连阳型 — 连续阳线但 MA 未完全对齐
    if con_bull >= 5 and momentum_score >= 50:
        return PATTERN_STRONG_RUN

    return PATTERN_NONE


# ── 综合评估 ─────────────────────────────────────────────


@dataclass
class RapidSnapshot:
    """5m / 15m 快照。"""
    timeframe: str
    close: float
    change_pct: float               # 单根 K 线涨跌幅
    change_3bar: float              # 最近 3 根 K 线累计涨跌幅
    change_6bar: float              # 最近 6 根 K 线累计涨跌幅
    ma_alignment_score: float
    ma_aligned: bool
    sustained_momentum_score: float
    consecutive_bull: int
    bull_volume_ratio: float
    volume_profile_score: float
    volume_spike: float
    vol_trending_up: bool
    rsi7: float


@dataclass
class PumpSignal:
    symbol: str
    pattern: str                   # MOMENTUM_SURGE / OI_EXPLOSION / BREAKOUT_SPIKE / STRONG_RUN / NONE
    confidence: float              # 0-100
    price_level: int               # 价格涨幅等级 0-3
    oi_level: int                  # OI 爆发等级 0-3
    tags: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    alert_priority: str = "NORMAL"  # URGENT / HIGH / NORMAL
    change_5m: float = 0.0
    change_15m: float = 0.0
    vol_spike_5m: float = 1.0
    vol_spike_15m: float = 1.0
    ma_score_5m: float = 0.0
    ma_score_15m: float = 0.0
    rsi_5m: float = 50.0
    consecutive_bull_5m: int = 0
    oi_change_pct: Optional[float] = None


def build_rapid_snapshot(timeframe: str, candles: List[Dict]) -> Optional[RapidSnapshot]:
    if len(candles) < 24:
        return None

    closes = [c["close"] for c in candles]
    close = closes[-1]
    change = pct_change(closes[-2], close) if len(closes) >= 2 else 0.0

    # 多根 K 线累计涨幅
    change_3 = pct_change(closes[-4], close) if len(closes) >= 4 else change
    change_6 = pct_change(closes[-7], close) if len(closes) >= 7 else change_3

    ma_score, ma_aligned, ma_desc = _ma_alignment_rapid(candles)
    mom_score, con_bull, vol_ratio, mom_desc = _sustained_momentum_rapid(candles)
    vol_score, vol_spike, vol_trend, vol_desc = _volume_profile_rapid(candles)

    return RapidSnapshot(
        timeframe=timeframe,
        close=close,
        change_pct=round(change, 2),
        change_3bar=round(change_3, 2),
        change_6bar=round(change_6, 2),
        ma_alignment_score=round(ma_score, 2),
        ma_aligned=ma_aligned,
        sustained_momentum_score=round(mom_score, 2),
        consecutive_bull=con_bull,
        bull_volume_ratio=vol_ratio,
        volume_profile_score=round(vol_score, 2),
        volume_spike=vol_spike,
        vol_trending_up=vol_trend,
        rsi7=round(_rsi_fast(closes, 7), 1),
    )


def evaluate_pump(
    symbol: str,
    candles_5m: Optional[List[Dict]],
    candles_15m: Optional[List[Dict]],
    change_24h_pct: float = 0.0,
    open_interest_change_pct: Optional[float] = None,
    funding_rate: Optional[float] = None,
) -> PumpSignal:
    """
    主入口: 用 5m / 15m 评估拉盘形态。

    优先用 15m 做趋势判断，5m 做精确定位。
    只有满足「持续性强涨」形态的才会被检出。
    """
    snap_5m = build_rapid_snapshot("5m", candles_5m) if candles_5m else None
    snap_15m = build_rapid_snapshot("15m", candles_15m) if candles_15m else None

    if not snap_5m and not snap_15m:
        return PumpSignal(symbol=symbol, pattern=PATTERN_NONE, confidence=0.0, price_level=0, oi_level=0)

    # ── 价格涨幅等级（用多根累计涨幅，非单根） ──
    # 5m: 3-bar(~15min)做T1, 6-bar(~30min)做T2/T3
    # 15m: 3-bar(~45min)做T1, 6-bar(~90min)做T2/T3
    def _calc_level(change_3: float, change_6: float, t1: float, t2: float, t3: float) -> int:
        best_up = max(change_3, change_6)
        if change_3 < 0 and change_6 < 0:
            return 0
        if best_up >= t3:
            return 3
        if best_up >= t2:
            return 2
        if best_up >= t1:
            return 1
        return 0

    p_level_5m = _calc_level(
        snap_5m.change_3bar if snap_5m else 0, snap_5m.change_6bar if snap_5m else 0,
        PUMP.price_surge_5m_t1, PUMP.price_surge_5m_t2, PUMP.price_surge_5m_t3,
    ) if snap_5m else 0

    p_level_15m = _calc_level(
        snap_15m.change_3bar if snap_15m else 0, snap_15m.change_6bar if snap_15m else 0,
        PUMP.price_surge_15m_t1, PUMP.price_surge_15m_t2, PUMP.price_surge_15m_t3,
    ) if snap_15m else 0

    m5_change = snap_5m.change_3bar if snap_5m else 0.0
    m15_change = snap_15m.change_3bar if snap_15m else 0.0

    price_level = max(p_level_5m, p_level_15m)

    # ── OI 爆发等级 ──
    oi_level = 0
    if open_interest_change_pct is not None:
        if open_interest_change_pct >= PUMP.oi_surge_t3:
            oi_level = 3
        elif open_interest_change_pct >= PUMP.oi_surge_t2:
            oi_level = 2
        elif open_interest_change_pct >= PUMP.oi_surge_t1:
            oi_level = 1

    # ── 防止单独依赖小周期：5m 有涨幅但 15m 没确认且无OI的不算 ──
    if p_level_5m >= 2 and p_level_15m == 0 and oi_level == 0:
        price_level = max(0, price_level - 1)

    # ── 取综合指标 ──
    # 以 15m 为趋势锚，5m 为精度参考
    ma_score = snap_15m.ma_alignment_score if snap_15m else (snap_5m.ma_alignment_score if snap_5m else 0)
    ma_aligned = snap_15m.ma_aligned if snap_15m else (snap_5m.ma_aligned if snap_5m else False)
    mom_score = snap_15m.sustained_momentum_score if snap_15m else (snap_5m.sustained_momentum_score if snap_5m else 0)
    con_bull_main = snap_15m.consecutive_bull if snap_15m else (snap_5m.consecutive_bull if snap_5m else 0)
    con_bull_5m = snap_5m.consecutive_bull if snap_5m else 0
    vol_score = max(
        snap_5m.volume_profile_score if snap_5m else 0,
        snap_15m.volume_profile_score if snap_15m else 0,
    )
    vol_spike_combined = max(
        snap_5m.volume_spike if snap_5m else 1.0,
        snap_15m.volume_spike if snap_15m else 1.0,
    )
    vol_trending = (snap_5m.vol_trending_up if snap_5m else False) or (snap_15m.vol_trending_up if snap_15m else False)
    rsi_5m = snap_5m.rsi7 if snap_5m else 50.0
    rsi_15m = snap_15m.rsi7 if snap_15m else 50.0

    # ── 前置过滤：必须满足最低要求才继续 ──
    # 要求: 价格涨幅至少达 T1 OR OI 爆发 OR 量能强劲
    has_minimum_signal = (
        price_level >= 1
        or oi_level >= 1
        or vol_spike_combined >= PUMP.vol_spike_5m
    )
    if not has_minimum_signal:
        return PumpSignal(symbol=symbol, pattern=PATTERN_NONE, confidence=0.0, price_level=price_level, oi_level=oi_level)

    # ── 形态分类 ──
    pattern = _classify_pattern(
        ma_score=ma_score,
        ma_aligned=ma_aligned,
        momentum_score=mom_score,
        con_bull=max(con_bull_main, con_bull_5m),
        vol_score=vol_score,
        vol_spike=vol_spike_combined,
        price_level=price_level,
        oi_level=oi_level,
        rsi_5m=rsi_5m,
        rsi_15m=rsi_15m,
        funding_rate=funding_rate,
    )

    if pattern == PATTERN_NONE:
        return PumpSignal(symbol=symbol, pattern=PATTERN_NONE, confidence=0.0, price_level=price_level, oi_level=oi_level)

    # ── RSI 过热过滤 ──
    # 只有「RSI超买 + 量能衰减 + 阳线中断」三者同时满足才算强弩之末
    # 单纯的 RSI 超买 + 量仍在放大 = 强势延续，不拦截
    rsi_overbought_5m = rsi_5m >= PUMP.rsi_overbought_5m
    rsi_overbought_15m = rsi_15m >= PUMP.rsi_overbought_15m
    vol_declining = not vol_trending and vol_spike_combined < 3.0
    bull_fading = con_bull_5m <= 1 or con_bull_main <= 1

    if rsi_overbought_5m and rsi_overbought_15m and vol_declining and bull_fading:
        # 双周期超买 + 量衰减 + 阳线中断 = 大概率尾声
        return PumpSignal(symbol=symbol, pattern=PATTERN_NONE, confidence=0.0, price_level=price_level, oi_level=oi_level)

    # 温和惩罚：RSI 超买但量仍旺 → 降 confidence，不拦截
    if rsi_overbought_5m and rsi_overbought_15m:
        confidence_penalty = 10.0
    elif rsi_overbought_5m or rsi_overbought_15m:
        confidence_penalty = 4.0
    else:
        confidence_penalty = 0.0

    # ── 综合信心分 (0-100) ──
    confidence = 0.0
    tags: List[str] = []
    reasons: List[str] = []

    # MA 对齐分 (0-25)
    ma_contrib = ma_score * 0.25
    confidence += ma_contrib
    if ma_aligned:
        tags.append("MA_ALIGNED")
        reasons.append(f"均线多头排列 (EMA>{'>'.join(['7','20','50'])} 向上)")
    elif ma_score >= 35:
        tags.append("MA_PARTIAL")

    # 持续动量分 (0-25)
    mom_contrib = mom_score * 0.25
    confidence += mom_contrib
    if con_bull_5m >= PUMP.min_consecutive_bull_5m:
        tags.append("SUSTAINED_RUN")
        reasons.append(f"连续阳线{con_bull_5m}根(5m)，买盘持续")
    if con_bull_main >= 6:
        tags.append("STRONG_MOMENTUM")

    # 量能分 (0-20)
    vol_contrib = vol_score * 0.20
    confidence += vol_contrib
    if vol_spike_combined >= PUMP.vol_spike_5m:
        tags.append("VOL_SURGE")
        reasons.append(f"量能 {vol_spike_combined:.1f}x 均值")
    if vol_trending:
        tags.append("VOL_TRENDING")
        reasons.append("量能递增，资金持续流入")

    # OI 爆发分 (0-20)
    oi_contrib = 0.0
    if oi_level >= 3:
        oi_contrib = 20
        tags.append("OI_3X")
        reasons.append(f"OI 暴增 {open_interest_change_pct:.0f}% (3X+)")
    elif oi_level >= 2:
        oi_contrib = 14
        tags.append("OI_2X")
        reasons.append(f"OI 激增 {open_interest_change_pct:.0f}% (2X+)")
    elif oi_level >= 1:
        oi_contrib = 8
        tags.append("OI_RISING")
        reasons.append(f"OI 上升 {open_interest_change_pct:.0f}%")
    confidence += oi_contrib

    # 价格等级分 (0-10)
    price_contrib = price_level * 3.33
    confidence += price_contrib
    if price_level >= 3:
        tags.append("PRICE_T3")
        reasons.append(f"价格暴涨 15%+")
    elif price_level >= 2:
        tags.append("PRICE_T2")
        reasons.append(f"价格急涨 10%+")
    elif price_level >= 1:
        tags.append("PRICE_T1")

    # 形态加分
    pattern_bonus = {
        PATTERN_SHORT_SQUEEZE: 10,
        PATTERN_OI_EXPLOSION: 8,
        PATTERN_BREAKOUT_SPIKE: 5,
        PATTERN_MOMENTUM_SURGE: 5,
        PATTERN_STRONG_RUN: 0,
    }
    confidence += pattern_bonus.get(pattern, 0)

    # RSI 动量区间加分
    if PUMP.rsi_momentum_zone_low <= rsi_15m <= PUMP.rsi_momentum_zone_high:
        confidence += 5  # 在健康的动量区间，还有空间

    # RSI 超买惩罚（从上面的过滤逻辑传递下来）
    confidence -= confidence_penalty

    confidence = max(0.0, min(100.0, confidence))

    # ── 优先级 ──
    if (oi_level >= 2 and price_level >= 1) or (price_level >= 3):
        alert_priority = "URGENT"
    elif pattern in (PATTERN_MOMENTUM_SURGE, PATTERN_BREAKOUT_SPIKE) and confidence >= 55:
        alert_priority = "HIGH"
    elif pattern == PATTERN_OI_EXPLOSION and confidence >= 50:
        alert_priority = "HIGH"
    elif confidence >= 45:
        alert_priority = "NORMAL"
    else:
        alert_priority = "NORMAL"

    return PumpSignal(
        symbol=symbol,
        pattern=pattern,
        confidence=round(confidence, 1),
        price_level=price_level,
        oi_level=oi_level,
        tags=tags,
        reasons=reasons,
        alert_priority=alert_priority,
        change_5m=round(m5_change, 2),
        change_15m=round(m15_change, 2),
        vol_spike_5m=round(snap_5m.volume_spike, 2) if snap_5m else 1.0,
        vol_spike_15m=round(snap_15m.volume_spike, 2) if snap_15m else 1.0,
        ma_score_5m=round(snap_5m.ma_alignment_score, 1) if snap_5m else 0.0,
        ma_score_15m=round(snap_15m.ma_alignment_score, 1) if snap_15m else 0.0,
        rsi_5m=round(rsi_5m, 1),
        consecutive_bull_5m=con_bull_5m,
        oi_change_pct=round(open_interest_change_pct, 2) if open_interest_change_pct is not None else None,
    )
