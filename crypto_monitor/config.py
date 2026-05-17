# -*- coding: utf-8 -*-

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
STATE_DIR = DATA_DIR / "state"

if load_dotenv:
    load_dotenv(ROOT_DIR / ".env")


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def env_float(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def env_list(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class ProxyConfig:
    enabled: bool
    http: str
    https: str

    @property
    def requests_proxies(self):
        if not self.enabled:
            return None
        return {"http": self.http, "https": self.https}

    @property
    def ccxt_proxies(self):
        if not self.enabled:
            return None
        return {"http": self.http, "https": self.https}


@dataclass(frozen=True)
class ScanConfig:
    interval_seconds: int
    market_refresh_seconds: int
    timeframes: tuple
    ohlcv_limit: int
    max_symbols_per_scan: int
    top_results: int
    min_quote_volume_usdt: float
    min_24h_change_pct: float
    max_24h_change_pct: float
    min_price: float
    excluded_bases: tuple
    snapshot_enabled: bool


@dataclass(frozen=True)
class ScoreConfig:
    min_push_score: float
    s_grade_score: float
    a_grade_score: float
    b_grade_score: float
    min_volume_spike: float
    min_oi_change_pct: float
    max_funding_rate_warn: float      # 超过此费率发出过热警告


@dataclass(frozen=True)
class SignalConfig:
    min_push_score: float
    digest_enabled: bool
    digest_top_n: int
    digest_min_grade: str
    min_signal_tags: int
    min_hot_candidates: int
    min_hot_change_pct: float
    allow_bases: tuple
    block_bases: tuple
    require_open_interest: bool
    require_volume_spike: bool
    min_volume_spike: float
    min_oi_change_pct: float
    max_extension_pct: float           # EMA偏离超过此值警告超涨
    cold_market_score_penalty: float
    spike_alert_enabled: bool
    spike_alert_5m_t1: float
    spike_alert_5m_t2: float
    spike_alert_5m_t3: float
    spike_alert_15m_t1: float
    spike_alert_15m_t2: float
    spike_alert_15m_t3: float
    spike_alert_oi_t1: float
    spike_alert_oi_t2: float
    spike_alert_oi_t3: float
    ma_alignment_required: bool
    push_min_quote_volume_24h: float
    push_min_quote_volume_4h: float
    push_funding_rate_abs_threshold: float
    max_symbol_pushes_per_day: int
    repush_min_score_delta: float
    pushed_list_digest_interval_seconds: int


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool
    bot_token: str
    chat_id: str
    auto_resolve_chat_id: bool
    parse_mode: str
    timeout_seconds: int
    min_interval_seconds: int
    max_repeats_s: int
    max_repeats_a: int
    max_repeats_b: int


@dataclass(frozen=True)
class AsrStrategyConfig:
    enabled: bool
    strategy_dir: str
    symbol: str
    ccxt_symbol: str
    version: str
    timeframe_minutes: int
    interval_seconds: int
    fetch_limit: int
    max_bars: int
    send_start_mode: str


RUN_ONCE = env_bool("RUN_ONCE", False)


PROXY = ProxyConfig(
    enabled=env_bool("USE_PROXY", False),
    http=os.getenv("HTTP_PROXY_URL", "http://127.0.0.1:7897"),
    https=os.getenv("HTTPS_PROXY_URL", "http://127.0.0.1:7897"),
)

SCAN = ScanConfig(
    interval_seconds=env_int("SCAN_INTERVAL_SECONDS", 300),
    market_refresh_seconds=env_int("MARKET_REFRESH_SECONDS", 3600),
    timeframes=tuple(env_list("SCAN_TIMEFRAMES", ["5m", "15m", "30m", "1h", "4h"])),
    ohlcv_limit=env_int("OHLCV_LIMIT", 120),
    max_symbols_per_scan=env_int("MAX_SYMBOLS_PER_SCAN", 0),
    top_results=env_int("TOP_RESULTS", 0),
    min_quote_volume_usdt=env_float("MIN_QUOTE_VOLUME_USDT", 20_000_000),
    min_24h_change_pct=env_float("MIN_24H_CHANGE_PCT", 0.0),
    max_24h_change_pct=env_float("MAX_24H_CHANGE_PCT", 80.0),
    min_price=env_float("MIN_PRICE", 0.00000001),
    excluded_bases=tuple(item.upper() for item in env_list("EXCLUDED_BASES", ["BTC", "ETH", "USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI"])),
    snapshot_enabled=env_bool("SNAPSHOT_ENABLED", True),
)

SCORE = ScoreConfig(
    min_push_score=env_float("MIN_PUSH_SCORE", 72.0),
    s_grade_score=env_float("S_GRADE_SCORE", 88.0),
    a_grade_score=env_float("A_GRADE_SCORE", 78.0),
    b_grade_score=env_float("B_GRADE_SCORE", 70.0),
    min_volume_spike=env_float("MIN_VOLUME_SPIKE", 2.0),
    min_oi_change_pct=env_float("MIN_OI_CHANGE_PCT", 15.0),
    max_funding_rate_warn=env_float("MAX_FUNDING_RATE_WARN", 0.003),
)

SIGNAL = SignalConfig(
    min_push_score=env_float("SIGNAL_MIN_PUSH_SCORE", 72.0),
    digest_enabled=env_bool("SIGNAL_DIGEST_ENABLED", True),
    digest_top_n=env_int("SIGNAL_DIGEST_TOP_N", 5),
    digest_min_grade=env_list("SIGNAL_DIGEST_MIN_GRADE", ["A"])[0].upper(),
    min_signal_tags=env_int("SIGNAL_MIN_SIGNAL_TAGS", 3),
    min_hot_candidates=env_int("SIGNAL_MIN_HOT_CANDIDATES", 3),
    min_hot_change_pct=env_float("SIGNAL_MIN_HOT_CHANGE_PCT", 8.0),
    allow_bases=tuple(item.upper() for item in env_list("SIGNAL_ALLOW_BASES", [])),
    block_bases=tuple(item.upper() for item in env_list("SIGNAL_BLOCK_BASES", ["BTC", "ETH"])),
    require_open_interest=env_bool("SIGNAL_REQUIRE_OPEN_INTEREST", False),
    require_volume_spike=env_bool("SIGNAL_REQUIRE_VOLUME_SPIKE", True),
    min_volume_spike=env_float("SIGNAL_MIN_VOLUME_SPIKE", 2.0),
    min_oi_change_pct=env_float("SIGNAL_MIN_OI_CHANGE_PCT", 15.0),
    max_extension_pct=env_float("SIGNAL_MAX_EXTENSION_PCT", 12.0),
    cold_market_score_penalty=env_float("SIGNAL_COLD_MARKET_SCORE_PENALTY", 8.0),
    spike_alert_enabled=env_bool("SIGNAL_SPIKE_ALERT_ENABLED", True),
    spike_alert_5m_t1=env_float("SIGNAL_SPIKE_ALERT_5M_T1", 5.0),
    spike_alert_5m_t2=env_float("SIGNAL_SPIKE_ALERT_5M_T2", 10.0),
    spike_alert_5m_t3=env_float("SIGNAL_SPIKE_ALERT_5M_T3", 15.0),
    spike_alert_15m_t1=env_float("SIGNAL_SPIKE_ALERT_15M_T1", 5.0),
    spike_alert_15m_t2=env_float("SIGNAL_SPIKE_ALERT_15M_T2", 10.0),
    spike_alert_15m_t3=env_float("SIGNAL_SPIKE_ALERT_15M_T3", 15.0),
    spike_alert_oi_t1=env_float("SIGNAL_SPIKE_ALERT_OI_T1", 50.0),
    spike_alert_oi_t2=env_float("SIGNAL_SPIKE_ALERT_OI_T2", 100.0),
    spike_alert_oi_t3=env_float("SIGNAL_SPIKE_ALERT_OI_T3", 200.0),
    ma_alignment_required=env_bool("SIGNAL_MA_ALIGNMENT_REQUIRED", True),
    push_min_quote_volume_24h=env_float("SIGNAL_PUSH_MIN_QUOTE_VOLUME_24H", 50_000_000),
    push_min_quote_volume_4h=env_float("SIGNAL_PUSH_MIN_QUOTE_VOLUME_4H", 10_000_000),
    push_funding_rate_abs_threshold=env_float("SIGNAL_PUSH_FUNDING_RATE_ABS_THRESHOLD", 0.001),
    max_symbol_pushes_per_day=env_int("SIGNAL_MAX_SYMBOL_PUSHES_PER_DAY", 3),
    repush_min_score_delta=env_float("SIGNAL_REPUSH_MIN_SCORE_DELTA", 6.0),
    pushed_list_digest_interval_seconds=env_int("SIGNAL_PUSHED_LIST_DIGEST_INTERVAL_SECONDS", 14_400),
)

TELEGRAM = TelegramConfig(
    enabled=env_bool("TELEGRAM_ENABLED", True),
    bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
    chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    auto_resolve_chat_id=env_bool("TELEGRAM_AUTO_RESOLVE_CHAT_ID", True),
    parse_mode=os.getenv("TELEGRAM_PARSE_MODE", "HTML"),
    timeout_seconds=env_int("TELEGRAM_TIMEOUT_SECONDS", 15),
    min_interval_seconds=env_int("TELEGRAM_MIN_INTERVAL_SECONDS", 300),
    max_repeats_s=env_int("TELEGRAM_MAX_REPEATS_S", 6),
    max_repeats_a=env_int("TELEGRAM_MAX_REPEATS_A", 3),
    max_repeats_b=env_int("TELEGRAM_MAX_REPEATS_B", 1),
)


ASR_STRATEGY = AsrStrategyConfig(
    enabled=env_bool("ASR_STRATEGY_ENABLED", True),
    strategy_dir=os.getenv("ASR_STRATEGY_DIR", "asr_btc_v17_alert_monitor_v1"),
    symbol=os.getenv("ASR_STRATEGY_SYMBOL", "BTCUSDT"),
    ccxt_symbol=os.getenv("ASR_STRATEGY_CCXT_SYMBOL", "BTC/USDT:USDT"),
    version=os.getenv("ASR_STRATEGY_VERSION", "v17"),
    timeframe_minutes=env_int("ASR_STRATEGY_TIMEFRAME_MINUTES", 15),
    interval_seconds=env_int("ASR_STRATEGY_INTERVAL_SECONDS", 60),
    fetch_limit=env_int("ASR_STRATEGY_FETCH_LIMIT", 1200),
    max_bars=env_int("ASR_STRATEGY_MAX_BARS", 1200),
    send_start_mode=os.getenv("ASR_STRATEGY_SEND_START_MODE", "skip_history_on_first_run"),
)


SCORE_WEIGHTS = {
    "trend": env_float("WEIGHT_TREND", 22.0),
    "momentum": env_float("WEIGHT_MOMENTUM", 22.0),
    "volume": env_float("WEIGHT_VOLUME", 18.0),
    "open_interest": env_float("WEIGHT_OPEN_INTEREST", 15.0),
    "ma_alignment": env_float("WEIGHT_MA_ALIGNMENT", 14.0),
    "breakout": env_float("WEIGHT_BREAKOUT", 5.0),
    "funding": env_float("WEIGHT_FUNDING", 4.0),
}

@dataclass(frozen=True)
class RapidScanConfig:
    enabled: bool
    interval_seconds: int
    timeframes: tuple
    min_quote_volume_usdt: float
    min_4h_quote_volume_usdt: float
    max_symbols: int
    min_24h_change_pct: float
    pump_min_confidence: float


@dataclass(frozen=True)
class PumpDetectConfig:
    # ── 5m 周期阈值 ──
    vol_spike_5m: float                # 5m 量能倍数下限
    price_surge_5m_t1: float            # 5m 涨幅第一档 5%
    price_surge_5m_t2: float            # 5m 涨幅第二档 10%
    price_surge_5m_t3: float            # 5m 涨幅第三档 15%
    # ── 15m 周期阈值 ──
    vol_spike_15m: float               # 15m 量能倍数下限
    price_surge_15m_t1: float           # 15m 涨幅第一档 5%
    price_surge_15m_t2: float           # 15m 涨幅第二档 10%
    price_surge_15m_t3: float           # 15m 涨幅第三档 15%
    # ── MA 对齐 ──
    ma_alignment_min: int               # 最少几根 MA 对齐 (EMA7>EMA20>EMA50)
    ma_slope_min_pct: float             # MA 斜率最低要求（百分点/bar）
    # ── 持续度 ──
    min_consecutive_bull_5m: int        # 5m 连续阳线最低要求
    min_consecutive_bull_15m: int       # 15m 连续阳线最低要求
    min_bull_volume_ratio: float        # 阳线平均量 / 阴线平均量 最低倍数
    # ── OI 爆发 ──
    oi_surge_t1: float                  # OI 第一档 50%
    oi_surge_t2: float                  # OI 第二档 100% (2X)
    oi_surge_t3: float                  # OI 第三档 200% (3X)
    # ── RSI 界限 ──
    rsi_overbought_5m: float
    rsi_overbought_15m: float
    rsi_momentum_zone_low: float        # 动量区间下沿
    rsi_momentum_zone_high: float       # 动量区间上沿
    # ── 价格加速度 ──
    price_accel_thresh: float           # 加速度阈值
    vol_trend_thresh: float             # 量能趋势（>1 仍在放大）


RAPID = RapidScanConfig(
    enabled=env_bool("RAPID_SCAN_ENABLED", True),
    interval_seconds=env_int("RAPID_SCAN_INTERVAL_SECONDS", 60),
    timeframes=("5m", "15m"),
    min_quote_volume_usdt=env_float("RAPID_MIN_QUOTE_VOLUME_USDT", 8_000_000),
    min_4h_quote_volume_usdt=env_float("RAPID_MIN_4H_QUOTE_VOLUME_USDT", 20_000_000),
    max_symbols=env_int("RAPID_MAX_SYMBOLS", 0),
    min_24h_change_pct=env_float("RAPID_MIN_24H_CHANGE_PCT", 2.0),
    pump_min_confidence=env_float("RAPID_PUMP_MIN_CONFIDENCE", 45.0),
)

PUMP = PumpDetectConfig(
    # 5m
    vol_spike_5m=env_float("PUMP_VOL_SPIKE_5M", 3.0),
    price_surge_5m_t1=env_float("PUMP_PRICE_SURGE_5M_T1", 5.0),
    price_surge_5m_t2=env_float("PUMP_PRICE_SURGE_5M_T2", 10.0),
    price_surge_5m_t3=env_float("PUMP_PRICE_SURGE_5M_T3", 15.0),
    # 15m
    vol_spike_15m=env_float("PUMP_VOL_SPIKE_15M", 2.5),
    price_surge_15m_t1=env_float("PUMP_PRICE_SURGE_15M_T1", 5.0),
    price_surge_15m_t2=env_float("PUMP_PRICE_SURGE_15M_T2", 10.0),
    price_surge_15m_t3=env_float("PUMP_PRICE_SURGE_15M_T3", 15.0),
    # MA 对齐
    ma_alignment_min=env_int("PUMP_MA_ALIGNMENT_MIN", 2),
    ma_slope_min_pct=env_float("PUMP_MA_SLOPE_MIN_PCT", 0.15),
    # 持续度
    min_consecutive_bull_5m=env_int("PUMP_MIN_CONSECUTIVE_BULL_5M", 4),
    min_consecutive_bull_15m=env_int("PUMP_MIN_CONSECUTIVE_BULL_15M", 3),
    min_bull_volume_ratio=env_float("PUMP_MIN_BULL_VOLUME_RATIO", 1.3),
    # OI
    oi_surge_t1=env_float("PUMP_OI_SURGE_T1", 50.0),
    oi_surge_t2=env_float("PUMP_OI_SURGE_T2", 100.0),
    oi_surge_t3=env_float("PUMP_OI_SURGE_T3", 200.0),
    # RSI
    rsi_overbought_5m=env_float("PUMP_RSI_OVERBOUGHT_5M", 88.0),
    rsi_overbought_15m=env_float("PUMP_RSI_OVERBOUGHT_15M", 82.0),
    rsi_momentum_zone_low=env_float("PUMP_RSI_MOMENTUM_ZONE_LOW", 55.0),
    rsi_momentum_zone_high=env_float("PUMP_RSI_MOMENTUM_ZONE_HIGH", 78.0),
    # 加速度
    price_accel_thresh=env_float("PUMP_PRICE_ACCEL_THRESH", 0.5),
    vol_trend_thresh=env_float("PUMP_VOL_TREND_THRESH", 0.8),
)

ENABLE_CONSOLE = env_bool("ENABLE_CONSOLE", True)
ENABLE_DEBUG = env_bool("ENABLE_DEBUG", False)
