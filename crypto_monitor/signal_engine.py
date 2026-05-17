# -*- coding: utf-8 -*-
"""信号引擎 v2.0 — 纯预警评估，无交易建议。"""

from dataclasses import dataclass, field
from typing import List

from .config import SCORE, SIGNAL
from .indicators import safe_div
from .models import IndicatorSnapshot, Opportunity, SymbolMarket
from .scoring import grade_for_score


@dataclass
class SignalAssessment:
    symbol: str
    tags: List[str] = field(default_factory=list)
    blocked_reasons: List[str] = field(default_factory=list)
    trigger_reason: str = ""
    market_cold: bool = False
    is_spike_alert: bool = False
    spike_tier: int = 0          # 0=无, 1=T1(5%), 2=T2(10%), 3=T3(15%)
    alert_level: str = "NORMAL"  # URGENT / HIGH / NORMAL / WATCH / SUPPRESS


GRADE_RANK = {"D": 0, "C": 1, "B": 2, "A": 3, "S": 4}


def primary_indicator(opportunity: Opportunity) -> IndicatorSnapshot:
    return opportunity.indicators.get("15m") or opportunity.indicators.get("5m") or next(iter(opportunity.indicators.values()))


def is_spike_alert(opportunity: Opportunity) -> bool:
    if not SIGNAL.spike_alert_enabled:
        return False
    i5 = opportunity.indicators.get("5m")
    i15 = opportunity.indicators.get("15m")
    if not i5 and not i15:
        return False
    change_5m = i5.change_pct if i5 else 0.0
    change_15m = i15.change_pct if i15 else 0.0
    if change_15m >= SIGNAL.spike_alert_15m_t1:
        return True
    if change_5m >= SIGNAL.spike_alert_5m_t2:
        return True
    return False


def spike_tier(opportunity: Opportunity) -> int:
    i5 = opportunity.indicators.get("5m")
    i15 = opportunity.indicators.get("15m")
    c5 = i5.change_pct if i5 else 0.0
    c15 = i15.change_pct if i15 else 0.0
    tier = 0
    if c15 >= SIGNAL.spike_alert_15m_t3 or c5 >= SIGNAL.spike_alert_5m_t3:
        tier = 3
    elif c15 >= SIGNAL.spike_alert_15m_t2 or c5 >= SIGNAL.spike_alert_5m_t2:
        tier = 2
    elif c15 >= SIGNAL.spike_alert_15m_t1 or c5 >= SIGNAL.spike_alert_5m_t1:
        tier = 1
    return tier


def oi_spike_tier(oi_change_pct) -> int:
    if oi_change_pct is None:
        return 0
    if oi_change_pct >= SIGNAL.spike_alert_oi_t3:
        return 3
    if oi_change_pct >= SIGNAL.spike_alert_oi_t2:
        return 2
    if oi_change_pct >= SIGNAL.spike_alert_oi_t1:
        return 1
    return 0


def spike_alert_reason(opportunity: Opportunity) -> str:
    tier = spike_tier(opportunity)
    i5 = opportunity.indicators.get("5m")
    i15 = opportunity.indicators.get("15m")
    oi_tier = oi_spike_tier(opportunity.open_interest_change_pct)
    parts = []
    if i15:
        parts.append(f"15m {i15.change_pct:.1f}%")
    if i5:
        parts.append(f"5m {i5.change_pct:.1f}%")
    tier_label = {1: "T1", 2: "T2⚡", 3: "T3🚨"}.get(tier, "")
    if tier_label:
        parts.insert(0, tier_label)
    if oi_tier >= 2:
        parts.append(f"OI +{opportunity.open_interest_change_pct:.0f}%")
    return "异动: " + " | ".join(parts)


def market_allowed(market: SymbolMarket) -> bool:
    base = market.base.upper()
    if SIGNAL.allow_bases and base not in SIGNAL.allow_bases:
        return False
    if base in SIGNAL.block_bases:
        return False
    return True


def is_cold_market(markets: List[SymbolMarket]) -> bool:
    hot = [item for item in markets if item.percentage >= SIGNAL.min_hot_change_pct]
    return len(hot) < SIGNAL.min_hot_candidates


def _has_volume_spike(opportunity: Opportunity) -> bool:
    indicator = primary_indicator(opportunity)
    return (
        indicator.volume_spike >= SIGNAL.min_volume_spike
        or opportunity.component_scores.get("volume", 0) >= 70
    )


def _has_ma_alignment(opportunity: Opportunity) -> bool:
    for tf in ("15m", "5m"):
        ind = opportunity.indicators.get(tf)
        if ind and ind.ma_alignment_score >= 55:
            return True
    return bool(opportunity.component_scores.get("ma_alignment", 0) >= 60)


def _meets_push_liquidity(opportunity: Opportunity) -> bool:
    return (
        opportunity.quote_volume >= SIGNAL.push_min_quote_volume_24h
        or opportunity.quote_volume_4h >= SIGNAL.push_min_quote_volume_4h
    )


def _has_funding_rate_anomaly(opportunity: Opportunity) -> bool:
    return (
        opportunity.funding_rate is not None
        and abs(opportunity.funding_rate) >= SIGNAL.push_funding_rate_abs_threshold
    )


def _has_sustained_momentum(opportunity: Opportunity) -> bool:
    for tf in ("15m", "5m"):
        ind = opportunity.indicators.get(tf)
        if ind and ind.sustained_momentum_score >= 50 and ind.consecutive_bull >= 3:
            return True
    return False


def build_signal_tags(opportunity: Opportunity) -> List[str]:
    tags = []
    indicator = primary_indicator(opportunity)
    extension_pct = opportunity.context.ema_extension_pct
    st = spike_tier(opportunity)

    if st >= 1:
        tags.append(f"SPIKE_T{st}")
    if st >= 2:
        tags.append("SPIKE_ALERT")

    if opportunity.component_scores.get("trend", 0) >= 70:
        tags.append("TREND_ALIGNED")
    if _has_ma_alignment(opportunity):
        tags.append("MA_ALIGNED")
    if _has_sustained_momentum(opportunity):
        tags.append("SUSTAINED_RUN")
    if _has_volume_spike(opportunity):
        tags.append("VOLUME_SPIKE")

    ot = oi_spike_tier(opportunity.open_interest_change_pct)
    if ot >= 3:
        tags.append("OI_3X")
    elif ot >= 2:
        tags.append("OI_2X")
    elif ot >= 1:
        tags.append("OI_RISING")
    elif opportunity.open_interest_change_pct is not None and opportunity.open_interest_change_pct >= SIGNAL.min_oi_change_pct:
        tags.append("OI_EXPANSION")

    if opportunity.component_scores.get("breakout", 0) >= 60 or indicator.high_breakout_pct > 0:
        tags.append("BREAKOUT")

    if opportunity.funding_rate is not None and opportunity.funding_rate <= -SIGNAL.push_funding_rate_abs_threshold:
        tags.append("NEG_FUNDING")
    elif opportunity.funding_rate is not None and opportunity.funding_rate >= SIGNAL.push_funding_rate_abs_threshold:
        tags.append("POS_FUNDING")

    if _has_funding_rate_anomaly(opportunity):
        tags.append("FUNDING_ANOMALY")

    if extension_pct > SIGNAL.max_extension_pct:
        tags.append("OVEREXTENDED")

    return tags


def build_blocked_reasons(opportunity: Opportunity, tags: List[str], market_cold: bool) -> List[str]:
    st = spike_tier(opportunity)
    if st >= 2:
        return []  # T2 以上的异动不拦截

    reasons = []
    positive_tags = [t for t in tags if t not in ("OVEREXTENDED",)]

    if market_cold:
        reasons.append("市场热度偏低，信号可靠性下降")

    if SIGNAL.ma_alignment_required and not _has_ma_alignment(opportunity):
        reasons.append("均线尚未形成多头排列，趋势确认度不足")

    if SIGNAL.require_volume_spike and not _has_volume_spike(opportunity):
        primary = primary_indicator(opportunity)
        reasons.append(f"放量程度不足 ({primary.volume_spike:.1f}x)，资金关注度有限")

    if "OVEREXTENDED" in tags:
        reasons.append(f"价格偏离 EMA20 过远 ({opportunity.context.ema_extension_pct:.1f}%)，追高需谨慎")

    if len(positive_tags) < SIGNAL.min_signal_tags:
        reasons.append(f"有效信号标签不足 ({len(positive_tags)}/{SIGNAL.min_signal_tags})，需更多确认")

    if SIGNAL.require_open_interest and opportunity.open_interest_change_pct is None:
        reasons.append("缺少持仓量数据，无法验证资金流向")

    return reasons


def assess_opportunity(opportunity: Opportunity, market_cold: bool = False) -> SignalAssessment:
    tags = build_signal_tags(opportunity)
    blocked_reasons = build_blocked_reasons(opportunity, tags, market_cold)
    st = spike_tier(opportunity)
    ot = oi_spike_tier(opportunity.open_interest_change_pct)
    spike_alert_active = st >= 2

    # ── 分数调整 ──
    score = opportunity.score
    if st >= 3:
        score += 9.0
    elif st >= 2:
        score += 6.0
    elif st >= 1:
        score += 3.0

    if "TREND_ALIGNED" in tags:
        score += 2.0
    if "MA_ALIGNED" in tags:
        score += 4.0
    if "SUSTAINED_RUN" in tags:
        score += 3.0
    if "VOLUME_SPIKE" in tags:
        score += 2.0
    if "OI_3X" in tags:
        score += 6.0
    elif "OI_2X" in tags:
        score += 4.0
    elif "OI_RISING" in tags:
        score += 2.5
    elif "OI_EXPANSION" in tags:
        score += 1.5
    if "BREAKOUT" in tags:
        score += 2.0
    if "NEG_FUNDING" in tags:
        score += 2.0
    if "POS_FUNDING" in tags:
        score += 1.5
    if "FUNDING_ANOMALY" in tags:
        score += 2.0
    if "OVEREXTENDED" in tags:
        score -= 6.0
    if market_cold:
        score -= SIGNAL.cold_market_score_penalty

    score = max(0.0, min(100.0, score))
    opportunity.score = round(score, 2)
    opportunity.grade = grade_for_score(opportunity.score)

    tag_text = ", ".join(tags[:6]) if tags else "NONE"
    opportunity.reasons = [f"信号标签: {tag_text}"] + opportunity.reasons
    if spike_alert_active:
        opportunity.reasons.insert(1, spike_alert_reason(opportunity))

    trigger_reason = spike_alert_reason(opportunity) if spike_alert_active else " / ".join(tags[:4]) if tags else "NO_STRONG_SIGNAL"

    # ── 预警等级 ──
    if st >= 3 or (ot >= 2 and st >= 1):
        alert_level = "URGENT"
    elif _has_funding_rate_anomaly(opportunity) and (st >= 1 or ot >= 1 or _has_volume_spike(opportunity)):
        alert_level = "HIGH"
    elif st >= 2:
        alert_level = "HIGH"
    elif st >= 1 and _has_ma_alignment(opportunity):
        alert_level = "HIGH"
    elif opportunity.score >= SCORE.s_grade_score:
        alert_level = "HIGH" if len(blocked_reasons) == 0 else "NORMAL"
    elif opportunity.score >= SCORE.a_grade_score:
        alert_level = "NORMAL" if len(blocked_reasons) <= 1 else "WATCH"
    elif opportunity.score >= SCORE.b_grade_score:
        alert_level = "WATCH" if len(blocked_reasons) <= 1 else "SUPPRESS"
    else:
        alert_level = "SUPPRESS"

    return SignalAssessment(
        symbol=opportunity.symbol,
        tags=tags,
        blocked_reasons=blocked_reasons,
        trigger_reason=trigger_reason,
        market_cold=market_cold,
        is_spike_alert=spike_alert_active,
        spike_tier=st,
        alert_level=alert_level,
    )


def qualified_for_push(opportunity: Opportunity, assessment: SignalAssessment) -> bool:
    if not _meets_push_liquidity(opportunity):
        return False
    if _has_funding_rate_anomaly(opportunity):
        return True
    if assessment.is_spike_alert or assessment.spike_tier >= 2:
        return True
    if opportunity.score < SIGNAL.min_push_score:
        return False

    grade = opportunity.grade
    blocked_count = len(assessment.blocked_reasons)

    if grade == "S":
        return True
    if grade == "A":
        return blocked_count <= 1
    if grade == "B":
        return blocked_count == 0
    return False


def grade_at_least(grade: str, minimum: str) -> bool:
    return GRADE_RANK.get(grade, 0) >= GRADE_RANK.get(minimum, 0)
