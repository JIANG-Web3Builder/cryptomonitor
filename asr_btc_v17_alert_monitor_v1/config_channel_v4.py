from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(r"D:\workspace\20260325\asr_btc_channel_research_v1")
OUTPUT_DIR = BASE_DIR / "output_channel_v4"
DATA_DIR = Path(r"D:\workspace\20260325\data")
DATA_FILE_15M = DATA_DIR / "BTCUSDT_15m.csv"
START_DATE = "2024-01-01 00:00:00"
END_DATE = "2026-05-01 23:59:59"
INITIAL_CAPITAL = 10000.0
COMMISSION_PCT = 0.036 / 100.0
SLIPPAGE_TICKS = 3.0
MINTICK = 0.01
SYMBOL = "BTCUSDT"
TIMEFRAME_MINUTES = 15

@dataclass(frozen=True)
class StrategyFeatures:
    version: str
    tp_mode: str
    tp_offset_multiplier: float | None
    long1_tp_uses_zone_offset: bool
    short1_tp_uses_zone_offset: bool
    reverse_breakeven_on_channel: bool
    reverse_lock_bars: int
    be_reentry_lock_bars: int | None
    deep_entry_overshoot_pct: float | None
    deep_entry_reclaim: bool
    mfe_zone_breakeven: bool
    level0_mfe_zone_breakeven: bool
    l1_reclaim_entry: bool
    l1_knife_half_size: bool
    no_progress_time_stop: bool
    channel_mode: str

BASE_PARAMS = {
    "base_tf_minutes": 60,
    "hl_length_input": 75,
    "atr_length_input": 20,
    "vov_length_input": 100,
    "smooth_factor_input": 12,
    "base_width_pct": 8.0,
    "adjust_factor": 0.5,
    "zone_channel_pct": 8.0 / 100.0,
    "stop_mult": 2.5,
    "be_buffer": 0.3 / 100.0,
    "time_stop_bars_in": 24,
    "outside_stop_bars": 3,
    "trend_ma_input": 200,
    "enable_ma_entry_filter": False,
    "rsi_period": 14,
    "rsi_long": 40.0,
    "rsi_short": 60.0,
    "cooldown_bars_in": 3,
    "enable_short": True,
    "breakout_15m": True,
    "breakout_rsi_high": 78.0,
    "breakout_rsi_low": 22.0,
    "breakout_back_bars": 2,
    "breakout_confirm_bars": 2,
    "breakout_min_hold_bars": 8,
    "breakout_cooldown_bars": 12,
    "reverse_stop_lookback": 30,
    "alloc_pct1": 20.0,
    "alloc_pct2": 40.0,
    "alloc_pct3": 40.0,
}

FEATURES_BY_VERSION = {
    "v10": StrategyFeatures("v10", "v9_unified", 0.6, False, False, True, 136, 136, None, False, False, False, False, False, False, "baseline"),
    "v11": StrategyFeatures("v11", "v11_hybrid", 0.5, False, False, True, 136, None, None, False, False, False, False, False, False, "baseline"),
    "v12": StrategyFeatures("v12", "v11_hybrid", 0.5, False, False, True, 136, None, 0.35, True, False, False, False, False, False, "baseline"),
    "v13": StrategyFeatures("v13", "v11_hybrid", 0.5, False, False, True, 136, None, 0.35, True, True, False, False, False, False, "baseline"),
    "v14": StrategyFeatures("v14", "v11_hybrid", 0.5, False, False, True, 136, None, 0.35, True, True, True, False, False, False, "baseline"),
    "v15": StrategyFeatures("v15", "v11_hybrid", 0.5, False, False, True, 136, None, 0.35, True, True, True, True, False, False, "baseline"),
    "v16": StrategyFeatures("v16", "v11_hybrid", 0.5, False, False, True, 136, None, 0.35, True, True, True, False, True, False, "baseline"),
    "v17": StrategyFeatures("v17", "v11_hybrid", 0.5, False, False, True, 136, None, 0.35, True, True, True, False, True, True, "baseline"),
    "v17_ch1": StrategyFeatures("v17_ch1", "v11_hybrid", 0.5, False, False, True, 136, None, 0.35, True, True, True, False, True, True, "atr_regime"),
    "v17_ch2": StrategyFeatures("v17_ch2", "v11_hybrid", 0.5, False, False, True, 136, None, 0.35, True, True, True, False, True, True, "swing_range"),
    "v17_ch3": StrategyFeatures("v17_ch3", "v11_hybrid", 0.5, False, False, True, 136, None, 0.35, True, True, True, False, True, True, "volume_weighted_mid"),
    "v17_ch4": StrategyFeatures("v17_ch4", "v11_hybrid", 0.5, False, False, True, 136, None, 0.35, True, True, True, False, True, True, "volume_mid_layers"),
}

VERSION_ORDER = ["v10", "v11", "v12", "v13", "v14", "v15", "v16", "v17", "v17_ch1", "v17_ch2", "v17_ch3", "v17_ch4"]
