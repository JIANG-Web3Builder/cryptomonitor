from __future__ import annotations

import math

import numpy as np
import pandas as pd

from config_channel_v4 import BASE_PARAMS, MINTICK, TIMEFRAME_MINUTES


def pine_rma(series: pd.Series, length: int) -> pd.Series:
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False, min_periods=length).mean()


def pine_rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = pine_rma(gain, length)
    avg_loss = pine_rma(loss, length)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(avg_gain != 0, 0.0).where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return rsi


def pine_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> tuple[pd.Series, pd.Series]:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = pine_rma(tr, length)
    return tr, atr


def compute_indicators(df: pd.DataFrame, base_params: dict | None = None) -> pd.DataFrame:
    params = dict(BASE_PARAMS)
    if base_params:
        params.update(base_params)

    out = df.copy()
    current_tf = TIMEFRAME_MINUTES
    tf_multiplier = params["base_tf_minutes"] / current_tf

    hl_length = max(1, round(params["hl_length_input"] * tf_multiplier))
    atr_length = max(1, round(params["atr_length_input"] * tf_multiplier))
    vov_length = max(1, round(params["vov_length_input"] * tf_multiplier))
    smooth_factor = max(1, round(params["smooth_factor_input"] * tf_multiplier))
    time_stop_bars = max(1, round(params["time_stop_bars_in"] * tf_multiplier))
    trend_ma = max(10, round(params["trend_ma_input"] * tf_multiplier))
    cooldown_bars = max(0, round(params["cooldown_bars_in"] * tf_multiplier))

    out["hl2"] = (out["high"] + out["low"]) / 2.0
    out["typicalPrice"] = (out["high"] + out["low"] + out["close"]) / 3.0
    rolling_volume = out["volume"].rolling(hl_length, min_periods=hl_length).sum()
    rolling_vp = (out["typicalPrice"] * out["volume"]).rolling(hl_length, min_periods=hl_length).sum()
    out["volumeWeightedMidLine"] = rolling_vp / rolling_volume.replace(0.0, np.nan)
    if params.get("channel_mode") == "volume_weighted_mid":
        out["midLine"] = out["volumeWeightedMidLine"]
    else:
        out["midLine"] = out["hl2"].rolling(hl_length, min_periods=hl_length).mean()
    out["tr_"], out["atr_"] = pine_atr(out["high"], out["low"], out["close"], atr_length)
    out["roc_close"] = out["close"].pct_change(1) * 100.0
    out["rocVol"] = out["roc_close"].abs().rolling(20, min_periods=20).std() / out["close"] * 10000.0
    out["compVol"] = out["tr_"] * 0.4 + out["atr_"] * 0.4 + out["rocVol"] * 0.2
    out["vovDenom"] = out["compVol"].rolling(vov_length, min_periods=vov_length).mean()
    out["vov"] = out["compVol"].rolling(vov_length, min_periods=vov_length).std() / out["vovDenom"]
    out["vovSafe"] = out["vov"].where((~out["vov"].isna()) & (out["vov"] <= 10.0), 0.0)
    base_offset = out["midLine"] * params["base_width_pct"] / 100.0 / 2.0
    if params.get("channel_mode") == "swing_range":
        out["swingHigh"] = out["high"].rolling(hl_length, min_periods=hl_length).max()
        out["swingLow"] = out["low"].rolling(hl_length, min_periods=hl_length).min()
        out["smoothRes"] = out["swingHigh"].ewm(span=smooth_factor, adjust=False, min_periods=smooth_factor).mean()
        out["smoothSup"] = out["swingLow"].ewm(span=smooth_factor, adjust=False, min_periods=smooth_factor).mean()
        out["smoothMid"] = (out["smoothRes"] + out["smoothSup"]) / 2.0
        out["dynamicOffset"] = (out["smoothRes"] - out["smoothSup"]).abs() / 2.0
        out["atrPct"] = out["atr_"] / out["close"]
        out["atrPctBase"] = np.nan
        out["atrRegime"] = np.nan
        out["atrRegimeSafe"] = 1.0
    elif params.get("channel_mode") == "atr_regime":
        out["atrPct"] = out["atr_"] / out["close"]
        out["atrPctBase"] = out["atrPct"].rolling(vov_length, min_periods=vov_length).median()
        out["atrRegime"] = (out["atrPct"] / out["atrPctBase"]).replace([np.inf, -np.inf], np.nan)
        out["atrRegimeSafe"] = out["atrRegime"].clip(lower=0.5, upper=2.0).fillna(1.0)
        out["dynamicOffset"] = base_offset * (out["atrRegimeSafe"] ** 0.5) * (1.0 + out["vovSafe"] * params["adjust_factor"])
        out["smoothRes"] = (out["midLine"] + out["dynamicOffset"]).ewm(span=smooth_factor, adjust=False, min_periods=smooth_factor).mean()
        out["smoothSup"] = (out["midLine"] - out["dynamicOffset"]).ewm(span=smooth_factor, adjust=False, min_periods=smooth_factor).mean()
        out["smoothMid"] = out["midLine"].ewm(span=smooth_factor, adjust=False, min_periods=smooth_factor).mean()
        out["swingHigh"] = np.nan
        out["swingLow"] = np.nan
    else:
        out["atrPct"] = out["atr_"] / out["close"]
        out["atrPctBase"] = np.nan
        out["atrRegime"] = np.nan
        out["atrRegimeSafe"] = 1.0
        out["dynamicOffset"] = base_offset * (1.0 + out["vovSafe"] * params["adjust_factor"])
        out["smoothRes"] = (out["midLine"] + out["dynamicOffset"]).ewm(span=smooth_factor, adjust=False, min_periods=smooth_factor).mean()
        out["smoothSup"] = (out["midLine"] - out["dynamicOffset"]).ewm(span=smooth_factor, adjust=False, min_periods=smooth_factor).mean()
        out["smoothMid"] = out["midLine"].ewm(span=smooth_factor, adjust=False, min_periods=smooth_factor).mean()
        out["swingHigh"] = np.nan
        out["swingLow"] = np.nan
    if params.get("channel_mode") == "volume_mid_layers":
        out["volumeSmoothMid"] = out["volumeWeightedMidLine"].ewm(span=smooth_factor, adjust=False, min_periods=smooth_factor).mean()
        out["internalMid"] = np.minimum(np.maximum(out["volumeSmoothMid"], out["smoothSup"]), out["smoothRes"])
    else:
        out["volumeSmoothMid"] = np.nan
        out["internalMid"] = out["smoothMid"]
    out["midHigh"] = (out["internalMid"] + out["smoothRes"]) / 2.0
    out["midLow"] = (out["internalMid"] + out["smoothSup"]) / 2.0
    out["superbuy"] = out["smoothMid"] + out["dynamicOffset"] * 1.618
    out["supersell"] = out["smoothMid"] - out["dynamicOffset"] * 1.618
    out["uperLine"] = out["smoothRes"] + out["dynamicOffset"] * 0.25
    out["downerLine"] = out["smoothSup"] - out["dynamicOffset"] * 0.25

    diff = (out["smoothRes"] - out["smoothSup"]).abs()
    diff = diff.fillna(MINTICK * 2.0)
    out["channelWidth"] = np.maximum(np.maximum(diff, MINTICK * 2.0), MINTICK * 2.0)
    out["zoneOffset"] = np.maximum(MINTICK, out["channelWidth"] * params["zone_channel_pct"])
    out["maLine"] = out["close"].rolling(trend_ma, min_periods=trend_ma).mean()
    out["rsiVal"] = pine_rsi(out["close"], params["rsi_period"])
    out["uptrend"] = out["close"] > out["maLine"]
    out["downtrend"] = out["close"] < out["maLine"]
    out["reverseLongStopRef"] = out["high"].rolling(params["reverse_stop_lookback"], min_periods=params["reverse_stop_lookback"]).max().shift(1)
    out["reverseShortStopRef"] = out["low"].rolling(params["reverse_stop_lookback"], min_periods=params["reverse_stop_lookback"]).min().shift(1)
    out["reverseStopBuffer"] = np.maximum(MINTICK, out["atr_"] * 0.1)
    out["reverseShortStopPxRef"] = np.where(
        out["reverseLongStopRef"].isna(),
        np.nan,
        np.maximum(out["high"], out["reverseLongStopRef"]) + out["reverseStopBuffer"],
    )
    out["reverseLongStopPxRef"] = np.where(
        out["reverseShortStopRef"].isna(),
        np.nan,
        np.minimum(out["low"], out["reverseShortStopRef"]) - out["reverseStopBuffer"],
    )

    out["tfMultiplier"] = tf_multiplier
    out["hlLength"] = hl_length
    out["atrLength"] = atr_length
    out["vovLength"] = vov_length
    out["smoothFactor"] = smooth_factor
    out["timeStopBars"] = time_stop_bars
    out["trendMA"] = trend_ma
    out["cooldownBars"] = cooldown_bars
    return out


def nz(value: float | None, fallback: float) -> float:
    if value is None:
        return fallback
    if isinstance(value, float) and math.isnan(value):
        return fallback
    return float(value)
