# -*- coding: utf-8 -*-
"""
市场结构分析 —— 判断当前市场处于什么状态。

分析维度:
  1. 趋势强度 (ADX-like): 市场是趋势市还是震荡市
  2. 多空偏向: 多头主导 / 空头主导 / 中性
  3. 波动率区间: 高波动 / 正常 / 低波动
  4. 市场宽度: 多少币种参与上涨（广度）
  5. 资金流向: 增量资金进场 or 存量博弈

结果用于:
  - 预警摘要中展示市场上下文
  - 调整预警灵敏度（震荡市减少噪音，趋势市提高敏感度）
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from .indicators import ema, rsi, safe_div
from .models import IndicatorSnapshot, SymbolMarket


@dataclass
class MarketStructure:
    regime: str             # TRENDING / RANGING / CHOPPY
    bias: str               # BULLISH / BEARISH / NEUTRAL
    volatility: str         # HIGH / NORMAL / LOW
    breadth_pct: float      # % of coins above EMA20
    breadth_strong_pct: float  # % of coins above EMA50
    avg_volume_spike: float    # 平均量能倍数
    avg_oi_change: Optional[float]  # 平均 OI 变化
    hot_sectors: List[str]      # 热门板块
    score: float            # 综合市场评分 0-100
    summary: str            # 一句话总结


# 板块映射（按币种前缀/后缀粗分类）
SECTOR_MAP = {
    "DEFI": ["UNI", "AAVE", "MKR", "COMP", "CRV", "SUSHI", "YFI", "1INCH", "DYDX", "SNX", "LDO", "PENDLE", "CAKE"],
    "L2": ["ARB", "OP", "MATIC", "POL", "IMX", "STRK", "ZK", "SCROLL", "MNT", "METIS"],
    "L1": ["SOL", "AVAX", "NEAR", "ATOM", "FTM", "APT", "SUI", "SEI", "INJ", "TIA", "CRO", "ROSE", "KAS"],
    "AI": ["FET", "AGIX", "OCEAN", "RNDR", "WLD", "ARKM", "TAO", "AKT", "PRIME"],
    "MEME": ["DOGE", "SHIB", "PEPE", "BONK", "FLOKI", "WIF", "BOME", "TURBO", "MEME", "PEOPLE"],
    "GAMING": ["SAND", "MANA", "AXS", "GALA", "ENJ", "RON", "SUPER", "YGG", "NAKA"],
    "RWA": ["ONDO", "TRU", "TRB", "LINK", "MKR"],
    "DEPIN": ["HNT", "HONEY", "IOTX", "AKT", "RNDR"],
}


def classify_sector(symbol: str) -> str:
    """将币种归类到板块。"""
    base = symbol.split("/")[0].upper() if "/" in symbol else symbol.upper()
    for sector, coins in SECTOR_MAP.items():
        if base in coins:
            return sector
    return "OTHER"


def analyze_structure(
    markets: List[SymbolMarket],
    indicators_map: Optional[Dict[str, Dict[str, IndicatorSnapshot]]] = None,
    oi_changes: Optional[Dict[str, float]] = None,
) -> MarketStructure:
    """
    分析当前市场结构。

    Args:
        markets: 全市场行情列表
        indicators_map: symbol -> {tf: IndicatorSnapshot} (可选，有则更精准)
        oi_changes: symbol -> OI变化率 (可选)
    """
    if not markets:
        return MarketStructure("RANGING", "NEUTRAL", "NORMAL", 50, 40, 1.0, None, [], 50, "数据不足")

    n = len(markets)

    # ── 1. 多空偏向 ──
    up_count = sum(1 for m in markets if m.percentage > 0)
    strong_up = sum(1 for m in markets if m.percentage >= 5)
    strong_down = sum(1 for m in markets if m.percentage <= -5)
    up_ratio = safe_div(up_count, n)

    if up_ratio >= 0.65 and strong_up >= strong_down * 2:
        bias = "BULLISH"
    elif up_ratio <= 0.35 and strong_down >= strong_up * 2:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    # ── 2. 波动率区间 ──
    avg_change_abs = sum(abs(m.percentage) for m in markets) / n
    if avg_change_abs > 12:
        volatility = "HIGH"
    elif avg_change_abs > 5:
        volatility = "NORMAL"
    else:
        volatility = "LOW"

    # ── 3. 市场宽度 ──
    breadth = safe_div(up_count, n) * 100
    breadth_strong = safe_div(strong_up, n) * 100

    # ── 4. 趋势 vs 震荡 ──
    # 用涨幅的标准差判断：标准差大 = 分化明显（趋势市），标准差小 = 齐涨齐跌（或震荡）
    changes = [m.percentage for m in markets]
    mean_chg = sum(changes) / n
    variance = sum((c - mean_chg) ** 2 for c in changes) / n
    std_dev = variance ** 0.5

    if std_dev > 15:
        regime = "TRENDING"  # 分化大 = 有明确方向
    elif std_dev > 8:
        regime = "RANGING"
    else:
        regime = "CHOPPY"

    # ── 5. 量能 ──
    avg_volume_spike = 1.0
    if indicators_map:
        spikes = []
        for sym, inds in indicators_map.items():
            for tf in ("15m", "5m"):
                i = inds.get(tf)
                if i and i.volume_spike > 1.0:
                    spikes.append(i.volume_spike)
                    break
        if spikes:
            avg_volume_spike = sum(spikes) / len(spikes)

    # ── 6. OI ──
    avg_oi = None
    if oi_changes:
        values = list(oi_changes.values())
        avg_oi = sum(values) / len(values) if values else None

    # ── 7. 板块热度 ──
    sector_perf: Dict[str, List[float]] = {}
    for m in markets:
        sec = classify_sector(m.symbol)
        if sec not in sector_perf:
            sector_perf[sec] = []
        sector_perf[sec].append(m.percentage)

    sector_scores = {}
    for sec, perfs in sector_perf.items():
        if len(perfs) >= 2:  # 至少 2 个代表币种
            sector_scores[sec] = sum(perfs) / len(perfs)

    hot_sectors = sorted(sector_scores, key=sector_scores.get, reverse=True)[:3]
    hot_sectors = [f"{s}({sector_scores[s]:+.1f}%)" for s in hot_sectors if sector_scores[s] > 1.0]

    # ── 8. 综合评分 ──
    score = 50.0
    if regime == "TRENDING":
        score += 15
    elif regime == "CHOPPY":
        score -= 10
    if bias == "BULLISH":
        score += 15
    elif bias == "BEARISH":
        score -= 10
    if breadth > 60:
        score += 10
    elif breadth < 40:
        score -= 10
    if avg_volume_spike > 2.0:
        score += 10
    if avg_oi is not None and avg_oi > 10:
        score += 5
    score = max(0, min(100, score))

    # ── 9. 一句话总结 ──
    regime_cn = {"TRENDING": "趋势市", "RANGING": "震荡市", "CHOPPY": "杂乱市"}
    bias_cn = {"BULLISH": "多头主导", "BEARISH": "空头主导", "NEUTRAL": "多空均衡"}
    vol_cn = {"HIGH": "高波动", "NORMAL": "正常波动", "LOW": "低波动"}

    parts = [
        f"{regime_cn[regime]}",
        f"{bias_cn[bias]}",
        f"{vol_cn[volatility]}",
        f"宽度{breadth:.0f}%",
    ]
    if hot_sectors:
        parts.append(f"热点: {', '.join(hot_sectors[:2])}")
    summary = " | ".join(parts)

    return MarketStructure(
        regime=regime,
        bias=bias,
        volatility=volatility,
        breadth_pct=round(breadth, 1),
        breadth_strong_pct=round(breadth_strong, 1),
        avg_volume_spike=round(avg_volume_spike, 2),
        avg_oi_change=round(avg_oi, 1) if avg_oi is not None else None,
        hot_sectors=hot_sectors,
        score=round(score, 1),
        summary=summary,
    )
