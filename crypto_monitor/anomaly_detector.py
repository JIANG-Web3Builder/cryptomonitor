# -*- coding: utf-8 -*-
"""
市场异常检测引擎 —— 捕捉非典型价格/量能/OI 行为。

与 pump_detector 的区别:
  - pump_detector 专注「持续性强涨」的拉盘形态
  - anomaly_detector 捕捉「一切不正常」的市场异象

检测类型:
  1. WHALE_FOOTPRINT: 单根 K 线量能+振幅异常巨大（大资金进场痕迹）
  2. VOLATILITY_BURST: ATR 突然爆炸式增长（暴风雨来临）
  3. DEAD_COIN_REVIVAL: 长期低量币种突然放量拉升（沉寂币复活）
  4. OI_ANOMALY: 持仓量相对该币自身历史异常变化
  5. FLASH_MOVE: 短时间内价格跳跃式移动（对倒/大单砸盘）
  6. VOLUME_ANOMALY: 成交量偏离该币自身历史均值异常远
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .indicators import pct_change, safe_div


@dataclass
class AnomalySignal:
    symbol: str
    anomaly_type: str       # WHALE_FOOTPRINT / VOLATILITY_BURST / DEAD_COIN_REVIVAL / OI_ANOMALY / FLASH_MOVE / VOLUME_ANOMALY
    severity: str           # CRITICAL / HIGH / MEDIUM / LOW
    confidence: float       # 0-100
    description: str
    details: Dict = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)


# ── 1. 大户脚印 ──────────────────────────────────────────

def detect_whale_footprint(
    symbol: str,
    candles: List[Dict],
    timeframe: str = "5m",
    vol_threshold: float = 8.0,    # 量能超过均值的倍数
    body_threshold: float = 3.0,   # 实体超过均值的倍数
) -> Optional[AnomalySignal]:
    """检测单根 K 线的异常巨量+大实体 = 大资金进场痕迹。"""
    if len(candles) < 20:
        return None

    vols = [c["volume"] for c in candles]
    bodies = [abs(c["close"] - c["open"]) / c["open"] * 100 for c in candles]

    avg_vol = sum(vols[:-1]) / (len(vols) - 1) if len(vols) > 1 else vols[0]
    avg_body = sum(bodies[:-1]) / (len(bodies) - 1) if len(bodies) > 1 else bodies[0]

    latest_vol = vols[-1]
    latest_body = bodies[-1]
    latest_change = pct_change(candles[-2]["close"], candles[-1]["close"]) if len(candles) >= 2 else 0

    vol_ratio = safe_div(latest_vol, avg_vol, 1.0)
    body_ratio = safe_div(latest_body, avg_body, 1.0) if avg_body > 0 else 1.0

    # 巨量 + 大实体
    if vol_ratio >= vol_threshold and body_ratio >= body_threshold:
        confidence = min(100, (vol_ratio * 7) + (abs(latest_change) * 3))
        direction = "拉升" if latest_change > 0 else "砸盘"

        return AnomalySignal(
            symbol=symbol,
            anomaly_type="WHALE_FOOTPRINT",
            severity="HIGH" if vol_ratio >= 12 else "MEDIUM",
            confidence=round(confidence, 1),
            description=f"{timeframe} 大户脚印: 单根{timeframe}K线量能{vol_ratio:.1f}x均值, 实体{body_ratio:.1f}x均值, {direction}{abs(latest_change):.1f}%",
            details={"vol_ratio": round(vol_ratio, 1), "body_ratio": round(body_ratio, 1),
                     "change_pct": round(latest_change, 2), "direction": direction},
            tags=["WHALE", f"VOL_{vol_ratio:.0f}X"],
        )

    return None


# ── 2. 波动率爆发 ────────────────────────────────────────

def detect_volatility_burst(
    symbol: str,
    candles: List[Dict],
    timeframe: str = "15m",
    atr_spike_threshold: float = 3.0,
) -> Optional[AnomalySignal]:
    """检测 ATR 突然暴增 = 极端行情启动。"""
    if len(candles) < 30:
        return None

    # 简化 ATR: 用 (high-low)/close 的均值
    ranges = []
    for c in candles:
        r = safe_div(c["high"] - c["low"], c["close"]) * 100
        ranges.append(r)

    # 前 20 根作为基线，最近 5 根作为当前
    baseline_ranges = ranges[-25:-5] if len(ranges) >= 25 else ranges[:-5]
    recent_ranges = ranges[-5:]

    if not baseline_ranges or not recent_ranges:
        return None

    avg_baseline = sum(baseline_ranges) / len(baseline_ranges)
    avg_recent = sum(recent_ranges) / len(recent_ranges)

    ratio = safe_div(avg_recent, avg_baseline, 1.0)

    if ratio >= atr_spike_threshold and avg_recent > 0.5:
        confidence = min(100, (ratio - 2) * 25 + 30)
        severity = "CRITICAL" if ratio >= 5 else ("HIGH" if ratio >= 4 else "MEDIUM")

        return AnomalySignal(
            symbol=symbol,
            anomaly_type="VOLATILITY_BURST",
            severity=severity,
            confidence=round(confidence, 1),
            description=f"{timeframe} 波动率爆发: 振幅从{avg_baseline:.1f}%暴增至{avg_recent:.1f}% ({ratio:.1f}x)，极端行情启动",
            details={"baseline_range": round(avg_baseline, 2), "recent_range": round(avg_recent, 2),
                     "ratio": round(ratio, 1)},
            tags=["VOLATILITY", "EXTREME"],
        )

    return None


# ── 3. 沉寂币复活 ────────────────────────────────────────

def detect_dead_coin_revival(
    symbol: str,
    candles: List[Dict],
    quote_volume_24h: float,
    timeframe: str = "15m",
    dead_vol_threshold: float = 0.3,  # 前段量 < 均值 30% = 沉寂
    revival_multiplier: float = 5.0,  # 当前量 > 均值 5x = 复活
) -> Optional[AnomalySignal]:
    """检测长期低量币突然放量。"""
    if len(candles) < 40:
        return None

    vols = [c["volume"] for c in candles]
    avg_all = sum(vols) / len(vols) if vols else 0

    earlier_vols = vols[-40:-10] if len(vols) >= 40 else vols[:-10]
    recent_vols = vols[-6:]

    if not earlier_vols or not recent_vols:
        return None

    avg_earlier = sum(earlier_vols) / len(earlier_vols)
    avg_recent = sum(recent_vols) / len(recent_vols)

    # 之前很沉寂
    was_dead = avg_earlier < avg_all * dead_vol_threshold
    # 现在活过来了
    is_alive = avg_recent > avg_all * revival_multiplier

    if was_dead and is_alive:
        ratio = safe_div(avg_recent, avg_earlier, 1.0)
        confidence = min(100, (ratio / 5) * 30 + 35)

        return AnomalySignal(
            symbol=symbol,
            anomaly_type="DEAD_COIN_REVIVAL",
            severity="HIGH" if ratio >= 10 else "MEDIUM",
            confidence=round(confidence, 1),
            description=f"{timeframe} 沉寂币复活: 前期均量仅{avg_earlier/avg_all*100:.0f}%均值，近6根暴增至{ratio:.1f}x",
            details={"was_ratio": round(avg_earlier/avg_all, 2), "now_ratio": round(ratio, 1),
                     "quote_volume_24h": quote_volume_24h},
            tags=["REVIVAL", "DEAD_COIN"],
        )

    return None


# ── 4. OI 异常 ────────────────────────────────────────────

def detect_oi_anomaly(
    symbol: str,
    oi_change_pct: Optional[float],
    oi_history: Optional[List[Tuple[float, float]]] = None,
    extreme_threshold: float = 80.0,
) -> Optional[AnomalySignal]:
    """检测 OI 的极端单向变化。"""
    if oi_change_pct is None:
        return None

    abs_oi = abs(oi_change_pct)

    if abs_oi >= extreme_threshold:
        direction = "暴增" if oi_change_pct > 0 else "骤降"
        severity = "CRITICAL" if abs_oi >= 200 else ("HIGH" if abs_oi >= 120 else "MEDIUM")

        return AnomalySignal(
            symbol=symbol,
            anomaly_type="OI_ANOMALY",
            severity=severity,
            confidence=min(100, abs_oi * 0.4 + 10),
            description=f"OI异常: 持仓量{direction} {oi_change_pct:+.0f}%，资金大幅{'涌入' if oi_change_pct > 0 else '撤离'}",
            details={"oi_change_pct": round(oi_change_pct, 1), "direction": direction},
            tags=["OI_EXTREME", f"OI_{direction}"],
        )

    return None


# ── 5. 闪击移动 ──────────────────────────────────────────

def detect_flash_move(
    symbol: str,
    candles: List[Dict],
    timeframe: str = "5m",
    move_threshold: float = 4.0,  # 单根或两根内涨跌幅超过此值
) -> Optional[AnomalySignal]:
    """检测短时间内价格跳跃式移动（对倒砸盘/大单扫货）。"""
    if len(candles) < 3:
        return None

    # 最近 1-2 根的累计涨跌幅
    changes = []
    for i in range(1, min(4, len(candles))):
        prev_close = candles[-(i+1)]["close"]
        curr_close = candles[-i]["close"]
        changes.append(pct_change(prev_close, curr_close))

    # 找单根最大跳变
    max_single = max(abs(c) for c in changes) if changes else 0

    if max_single >= move_threshold:
        direction = "上跳" if changes[0] > 0 else "下跳"
        severity = "CRITICAL" if max_single >= 15 else ("HIGH" if max_single >= 10 else "MEDIUM")

        return AnomalySignal(
            symbol=symbol,
            anomaly_type="FLASH_MOVE",
            severity=severity,
            confidence=min(100, max_single * 5 + 15),
            description=f"{timeframe} 闪击移动: {direction} {max_single:.1f}%，疑似大户扫货/对倒",
            details={"max_move_pct": round(max_single, 2), "direction": direction,
                     "consecutive": [round(c, 2) for c in changes[:3]]},
            tags=["FLASH", f"JUMP_{max_single:.0f}PCT"],
        )

    return None


# ── 6. 量能异常（相对自身历史） ──────────────────────────

def detect_volume_anomaly(
    symbol: str,
    candles: List[Dict],
    timeframe: str = "15m",
    zscore_threshold: float = 3.5,
) -> Optional[AnomalySignal]:
    """用量能 Z-Score 检测异常放量（相对该币自身历史）。"""
    if len(candles) < 30:
        return None

    vols = [c["volume"] for c in candles]
    historical_vols = vols[:-3]
    recent_vols = vols[-3:]

    if not historical_vols:
        return None

    mean_vol = sum(historical_vols) / len(historical_vols)
    variance = sum((v - mean_vol) ** 2 for v in historical_vols) / len(historical_vols)
    std_vol = variance ** 0.5

    if std_vol == 0:
        return None

    avg_recent = sum(recent_vols) / len(recent_vols)
    zscore = (avg_recent - mean_vol) / std_vol

    if zscore >= zscore_threshold:
        confidence = min(100, (zscore - 2.5) * 18 + 20)
        severity = "HIGH" if zscore >= 5 else "MEDIUM"

        return AnomalySignal(
            symbol=symbol,
            anomaly_type="VOLUME_ANOMALY",
            severity=severity,
            confidence=round(confidence, 1),
            description=f"{timeframe} 量能异常: Z-Score={zscore:.1f}, 近期均量偏离历史{((avg_recent/mean_vol)-1)*100:.0f}%",
            details={"zscore": round(zscore, 1), "mean_vol": round(mean_vol, 0),
                     "recent_vol": round(avg_recent, 0), "ratio": round(avg_recent/mean_vol, 1)},
            tags=["VOL_ANOMALY", f"Z{zscore:.0f}"],
        )

    return None


# ── 综合异常扫描 ──────────────────────────────────────────

def scan_anomalies(
    symbol: str,
    candles_5m: Optional[List[Dict]],
    candles_15m: Optional[List[Dict]],
    quote_volume_24h: float = 0.0,
    oi_change_pct: Optional[float] = None,
    oi_history: Optional[List[Tuple[float, float]]] = None,
) -> List[AnomalySignal]:
    """
    对单个币种执行全部异常检测。

    返回所有检测到的异常信号，按严重度排序。
    """
    all_anomalies: List[AnomalySignal] = []

    # 5m 周期检测
    if candles_5m:
        whale = detect_whale_footprint(symbol, candles_5m, "5m")
        if whale:
            all_anomalies.append(whale)

        flash = detect_flash_move(symbol, candles_5m, "5m")
        if flash:
            all_anomalies.append(flash)

    # 15m 周期检测
    if candles_15m:
        vol_burst = detect_volatility_burst(symbol, candles_15m, "15m")
        if vol_burst:
            all_anomalies.append(vol_burst)

        dead = detect_dead_coin_revival(symbol, candles_15m, quote_volume_24h, "15m")
        if dead:
            all_anomalies.append(dead)

        vol_anom = detect_volume_anomaly(symbol, candles_15m, "15m")
        if vol_anom:
            all_anomalies.append(vol_anom)

    # OI 异常（不需要 K 线）
    oi_anom = detect_oi_anomaly(symbol, oi_change_pct, oi_history)
    if oi_anom:
        all_anomalies.append(oi_anom)

    # 排序: CRITICAL > HIGH > MEDIUM > LOW
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    all_anomalies.sort(key=lambda a: (severity_order.get(a.severity, 4), -a.confidence))

    return all_anomalies
