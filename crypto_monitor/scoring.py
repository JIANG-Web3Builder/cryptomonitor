# -*- coding: utf-8 -*-

from typing import Dict, List, Optional

from .config import SCORE, SCORE_WEIGHTS, SIGNAL
from .indicators import safe_div
from .models import ContextInfo, IndicatorSnapshot, Opportunity, SymbolMarket


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def weighted(value: float, weight: float) -> float:
    return clamp(value) * weight / 100.0


def grade_for_score(score: float) -> str:
    if score >= SCORE.s_grade_score:
        return "S"
    if score >= SCORE.a_grade_score:
        return "A"
    if score >= SCORE.b_grade_score:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def score_trend(indicators: Dict[str, IndicatorSnapshot]) -> float:
    score = 0.0
    checks = 0
    for timeframe in ("5m", "15m", "1h", "4h"):
        item = indicators.get(timeframe)
        if not item:
            continue
        checks += 1
        if item.close > item.ema20 > item.ema50:
            score += 35
        elif item.close > item.ema20:
            score += 25
        elif item.ema20 > item.ema50:
            score += 15
        if item.trend_slope_pct > 6:
            score += 25
        elif item.trend_slope_pct > 2:
            score += 18
        elif item.trend_slope_pct > 0:
            score += 10
        if 48 <= item.rsi14 <= 78:
            score += 20
        elif 78 < item.rsi14 <= 86:
            score += 10
        if item.atr_pct >= 0.5:
            score += 20
    if checks == 0:
        return 0.0
    return clamp(score / checks)


def score_momentum(indicators: Dict[str, IndicatorSnapshot], market: SymbolMarket) -> float:
    five = indicators.get("5m")
    fifteen = indicators.get("15m")
    one_hour = indicators.get("1h")
    score = 0.0
    if five and five.change_pct > 0:
        score += clamp(five.change_pct * 9, 0, 28)
    if fifteen and fifteen.trend_slope_pct > 0:
        score += clamp(fifteen.trend_slope_pct * 4, 0, 30)
    if one_hour and one_hour.trend_slope_pct > 0:
        score += clamp(one_hour.trend_slope_pct * 2.2, 0, 25)
    if market.percentage > 0:
        score += clamp(market.percentage * 0.5, 0, 17)
    return clamp(score)


def score_volume(indicators: Dict[str, IndicatorSnapshot], market: SymbolMarket) -> float:
    spike_values = [item.volume_spike for item in indicators.values()]
    spike = max(spike_values) if spike_values else 1.0
    score = clamp((spike - 1.0) * 34, 0, 70)
    if market.quote_volume >= 300_000_000:
        score += 30
    elif market.quote_volume >= 120_000_000:
        score += 24
    elif market.quote_volume >= 50_000_000:
        score += 16
    elif market.quote_volume >= 20_000_000:
        score += 8
    return clamp(score)


def score_open_interest(open_interest_change_pct: Optional[float]) -> float:
    if open_interest_change_pct is None:
        return 45.0
    if open_interest_change_pct <= 0:
        return clamp(35 + open_interest_change_pct * 3, 0, 45)
    if open_interest_change_pct >= 200:
        return 100.0
    if open_interest_change_pct >= 100:
        return 90.0
    if open_interest_change_pct >= 50:
        return 80.0
    return clamp(45 + open_interest_change_pct * 7, 0, 100)


def score_ma_alignment(indicators: Dict[str, IndicatorSnapshot]) -> float:
    """均线多头排列 + 持续动量 综合评分。"""
    best = 0.0
    for tf in ("15m", "5m", "1h"):
        ind = indicators.get(tf)
        if ind and ind.ma_alignment_score > best:
            best = ind.ma_alignment_score
    for tf in ("15m", "5m"):
        ind = indicators.get(tf)
        if ind and ind.sustained_momentum_score > 0:
            best = max(best, ind.sustained_momentum_score * 0.6 + ind.ma_alignment_score * 0.4)
    return clamp(best)


def score_breakout(indicators: Dict[str, IndicatorSnapshot]) -> float:
    breakout_values = [item.high_breakout_pct for item in indicators.values()]
    best_breakout = max(breakout_values) if breakout_values else 0.0
    score = 0.0
    if best_breakout > 0:
        score += clamp(best_breakout * 18, 0, 60)
    if best_breakout > 3:
        score += clamp((best_breakout - 3) * 15, 0, 40)
    return clamp(score)


def score_funding(funding_rate: Optional[float]) -> float:
    if funding_rate is None:
        return 60.0
    abs_rate = abs(funding_rate)
    # 负费率 = 逼空潜力
    if funding_rate < -0.001:
        return clamp(80 + min(abs_rate * 8000, 20), 0, 100)
    if funding_rate < 0:
        return clamp(70 + abs_rate * 5000, 0, 100)
    if abs_rate <= 0.0003:
        return 85.0
    if abs_rate <= 0.001:
        return 65.0
    if abs_rate <= SCORE.max_funding_rate_warn:
        return 45.0
    return 20.0


def build_reasons(
    component_scores: Dict[str, float],
    indicators: Dict[str, IndicatorSnapshot],
    open_interest_change_pct: Optional[float],
    funding_rate: Optional[float],
) -> List[str]:
    reasons = []
    primary = indicators.get("15m") or indicators.get("5m") or next(iter(indicators.values()))

    if component_scores.get("trend", 0) >= 70:
        reasons.append("多周期趋势向上，价格在 EMA20/EMA50 上方运行")
    if component_scores.get("ma_alignment", 0) >= 60:
        reasons.append("均线多头排列(EMA7>EMA20>EMA50)，趋势结构健康")
    if component_scores.get("momentum", 0) >= 65:
        reasons.append(f"5m/15m 动量强劲，短线资金持续流入")
    if component_scores.get("volume", 0) >= 70:
        reasons.append(f"成交量显著放大，短线量能约为均值 {primary.volume_spike:.1f} 倍")
    if open_interest_change_pct is not None:
        if open_interest_change_pct >= 200:
            reasons.append(f"OI 爆发 +{open_interest_change_pct:.0f}% (3X+)，大量增量资金涌入")
        elif open_interest_change_pct >= 100:
            reasons.append(f"OI 激增 +{open_interest_change_pct:.0f}% (2X+)，主力资金进场迹象")
        elif open_interest_change_pct >= 50:
            reasons.append(f"OI 大幅上升 +{open_interest_change_pct:.0f}%，增量资金明确")
        elif open_interest_change_pct >= SCORE.min_oi_change_pct:
            reasons.append(f"OI 上升 +{open_interest_change_pct:.0f}%，资金开始关注")
    if funding_rate is not None:
        if funding_rate <= -SIGNAL.push_funding_rate_abs_threshold:
            reasons.append(f"资金费率深度为负 ({funding_rate*100:.3f}%)，存在逼空条件")
        elif funding_rate >= SIGNAL.push_funding_rate_abs_threshold:
            reasons.append(f"资金费率偏高 ({funding_rate*100:.3f}%)，多头拥挤需警惕")
    if component_scores.get("breakout", 0) >= 65:
        reasons.append("价格突破近期压力位，上方空间打开")

    if not reasons:
        reasons.append("出现异动但信号仍需继续确认")
    return reasons[:6]


def build_opportunity(
    market: SymbolMarket,
    indicators: Dict[str, IndicatorSnapshot],
    context: ContextInfo,
    funding_rate: Optional[float],
    open_interest: Optional[float],
    open_interest_change_pct: Optional[float],
    quote_volume_4h: float = 0.0,
) -> Opportunity:
    component_scores = {
        "trend": score_trend(indicators),
        "momentum": score_momentum(indicators, market),
        "volume": score_volume(indicators, market),
        "open_interest": score_open_interest(open_interest_change_pct),
        "ma_alignment": score_ma_alignment(indicators),
        "breakout": score_breakout(indicators),
        "funding": score_funding(funding_rate),
    }
    total = 0.0
    for name, value in component_scores.items():
        total += weighted(value, SCORE_WEIGHTS.get(name, 0.0))
    total = clamp(total)
    grade = grade_for_score(total)

    return Opportunity(
        symbol=market.symbol,
        market_id=market.market_id,
        base=market.base,
        score=round(total, 2),
        grade=grade,
        current_price=market.last,
        quote_volume=market.quote_volume,
        quote_volume_4h=quote_volume_4h,
        change_24h_pct=market.percentage,
        funding_rate=funding_rate,
        open_interest=open_interest,
        open_interest_change_pct=open_interest_change_pct,
        indicators=indicators,
        context=context,
        component_scores={key: round(value, 2) for key, value in component_scores.items()},
        reasons=build_reasons(component_scores, indicators, open_interest_change_pct, funding_rate),
    )
