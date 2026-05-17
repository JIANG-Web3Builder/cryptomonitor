# -*- coding: utf-8 -*-

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_ohlcv(cls, row):
        return cls(
            timestamp=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )


@dataclass
class SymbolMarket:
    symbol: str
    market_id: str
    base: str
    quote: str
    active: bool
    last: float
    quote_volume: float
    percentage: float
    high: float
    low: float
    oi_24h_change_pct: Optional[float] = None


@dataclass
class IndicatorSnapshot:
    timeframe: str
    close: float
    change_pct: float
    ema20: float
    ema50: float
    rsi14: float
    atr14: float
    atr_pct: float
    volume_spike: float
    trend_slope_pct: float
    high_breakout_pct: float
    low_distance_pct: float
    ema7: float = 0.0
    ma_alignment_score: float = 0.0
    sustained_momentum_score: float = 0.0
    volume_profile_score: float = 0.0
    consecutive_bull: int = 0
    bull_volume_ratio: float = 1.0


@dataclass
class ContextInfo:
    """轻量级市场上下文，仅供预警参考。"""
    support: float          # 最近支撑位
    resistance: float        # 最近压力位
    atr_pct: float           # ATR波动率百分比
    ema20: float             # 关键均线
    ema_extension_pct: float # 价格偏离 EMA20 的程度


@dataclass
class Opportunity:
    symbol: str
    market_id: str
    base: str
    score: float
    grade: str
    current_price: float
    quote_volume: float
    quote_volume_4h: float
    change_24h_pct: float
    funding_rate: Optional[float]
    open_interest: Optional[float]
    open_interest_change_pct: Optional[float]
    indicators: Dict[str, IndicatorSnapshot]
    context: ContextInfo
    component_scores: Dict[str, float]
    reasons: List[str]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        data = asdict(self)
        data["created_at"] = self.created_at
        return data


@dataclass
class PushState:
    last_push_at: float = 0.0
    last_score: float = 0.0
    last_grade: str = "D"
    last_alert_level: str = "NORMAL"
    last_spike_tier: int = 0
    repeat_count: int = 0
    daily_push_date: str = ""
    daily_push_count: int = 0
    last_signature: str = ""
