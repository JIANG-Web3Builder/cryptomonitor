from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from config_channel_v4 import BASE_PARAMS, COMMISSION_PCT, INITIAL_CAPITAL, MINTICK, SLIPPAGE_TICKS
from strategy_versions_channel_v4 import get_version_features


@dataclass
class PositionLot:
    entry_id: str
    side: str
    qty: float
    entry_price: float
    entry_time: pd.Timestamp
    entry_bar: int
    level: int
    trend_mode_at_entry: int
    be_flags_at_entry: str
    breakout_context: str
    reverse_context: str
    mfe_abs: float = 0.0
    mae_abs: float = 0.0


@dataclass
class BacktestState:
    version: str
    realized_equity: float = INITIAL_CAPITAL
    nLong: int = 0
    nShort: int = 0
    lastEntryBar: int = 0
    entryPx1: float | None = None
    entryPx2: float | None = None
    entryPx3: float | None = None
    entryBar1: int = 0
    entryBar2: int = 0
    entryBar3: int = 0
    be1: bool = False
    be2: bool = False
    be3: bool = False
    trendMode: int = 0
    breakoutInsideBars: int = 0
    breakoutAboveBars: int = 0
    breakoutBelowBars: int = 0
    breakoutEntryBar: int | None = None
    lastBreakoutBar: int = -10000
    reverseStopPx: float | None = None
    reverseTpPx: float | None = None
    outsideLongBars: int = 0
    outsideShortBars: int = 0
    long1BeLockBar: int | None = None
    long2BeLockBar: int | None = None
    long3BeLockBar: int | None = None
    short1BeLockBar: int | None = None
    short2BeLockBar: int | None = None
    short3BeLockBar: int | None = None
    open_positions: dict[str, PositionLot] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    trade_seq: int = 0

    def position_size(self) -> float:
        return sum(pos.qty if pos.side == "long" else -pos.qty for pos in self.open_positions.values())

    def is_long(self) -> bool:
        return self.position_size() > 0

    def is_short(self) -> bool:
        return self.position_size() < 0

    def is_flat(self) -> bool:
        return abs(self.position_size()) < 1e-12

    def position_avg_price(self) -> float | None:
        if not self.open_positions:
            return None
        qty_sum = sum(pos.qty for pos in self.open_positions.values())
        if qty_sum <= 0:
            return None
        return sum(pos.qty * pos.entry_price for pos in self.open_positions.values()) / qty_sum

    def current_equity(self, close_price: float) -> float:
        unrealized = 0.0
        for pos in self.open_positions.values():
            if pos.side == "long":
                unrealized += (close_price - pos.entry_price) * pos.qty
            else:
                unrealized += (pos.entry_price - close_price) * pos.qty
        return self.realized_equity + unrealized

    def be_flags_string(self) -> str:
        return f"{int(self.be1)}|{int(self.be2)}|{int(self.be3)}"

    def set_level_entry_fields(self, level: int, price: float, bar_index: int) -> None:
        if level == 1:
            self.entryPx1 = price
            self.entryBar1 = bar_index
            self.be1 = False
        elif level == 2:
            self.entryPx2 = price
            self.entryBar2 = bar_index
            self.be2 = False
        elif level == 3:
            self.entryPx3 = price
            self.entryBar3 = bar_index
            self.be3 = False

    def clear_level_fields(self, level: int) -> None:
        if level == 1:
            self.entryPx1 = None
            self.be1 = False
        elif level == 2:
            self.entryPx2 = None
            self.be2 = False
        elif level == 3:
            self.entryPx3 = None
            self.be3 = False


def _is_nan(value: Any) -> bool:
    return value is None or (isinstance(value, float) and np.isnan(value))


def _value_or_none(value: Any) -> float | None:
    if _is_nan(value):
        return None
    return float(value)


def _slip_price(price: float, side: str, action: str, order_kind: str) -> float:
    slip = SLIPPAGE_TICKS * MINTICK
    if order_kind == "limit":
        return price
    if side == "long" and action == "entry":
        return price + slip
    if side == "short" and action == "entry":
        return price - slip
    if side == "long" and action == "exit":
        return price - slip
    return price + slip


def _infer_path(row: pd.Series) -> list[float]:
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])
    if abs(h - o) < abs(o - l):
        return [o, h, l, c]
    return [o, l, h, c]


def _between(level: float, start: float, end: float) -> bool:
    return min(start, end) <= level <= max(start, end)


def _first_exit_hit(side: str, stop: float | None, limit: float | None, row: pd.Series) -> tuple[str, float] | None:
    o = float(row["open"])
    if side == "long":
        if stop is not None and o <= stop:
            return "stop", o
        if limit is not None and o >= limit:
            return "limit", o
    else:
        if stop is not None and o >= stop:
            return "stop", o
        if limit is not None and o <= limit:
            return "limit", o

    path = _infer_path(row)
    for start, end in zip(path, path[1:]):
        candidates: list[tuple[str, float, float]] = []
        if stop is not None and _between(stop, start, end):
            candidates.append(("stop", abs(stop - start), stop))
        if limit is not None and _between(limit, start, end):
            candidates.append(("limit", abs(limit - start), limit))
        if candidates:
            candidates.sort(key=lambda item: item[1])
            return candidates[0][0], candidates[0][2]
    return None


def _get_stop(entry_price: float, is_buy: bool, is_be: bool, cur_ofs: float, params: dict) -> float:
    if is_be:
        return entry_price * (1.0 + params["be_buffer"]) if is_buy else entry_price * (1.0 - params["be_buffer"])
    return entry_price - params["stop_mult"] * cur_ofs if is_buy else entry_price + params["stop_mult"] * cur_ofs


def _level_from_entry_id(entry_id: str) -> int:
    if entry_id.endswith("1"):
        return 1
    if entry_id.endswith("2"):
        return 2
    if entry_id.endswith("3"):
        return 3
    return 0


def _close_position(state: BacktestState, entry_id: str, row: pd.Series, fill_price: float, exit_reason: str, exit_id: str, order_kind: str) -> bool:
    pos = state.open_positions.get(entry_id)
    if pos is None:
        return False

    side = pos.side
    actual_fill = _slip_price(fill_price, side, "exit", order_kind)
    pnl_abs = (actual_fill - pos.entry_price) * pos.qty if side == "long" else (pos.entry_price - actual_fill) * pos.qty
    exit_notional = actual_fill * pos.qty
    exit_commission = exit_notional * COMMISSION_PCT
    state.realized_equity += pnl_abs - exit_commission
    state.trade_seq += 1
    state.trades.append(
        {
            "version": state.version,
            "trade_id": state.trade_seq,
            "side": side,
            "entry_id": pos.entry_id,
            "exit_id": exit_id,
            "entry_time": pos.entry_time,
            "exit_time": row["open_time"],
            "entry_price": pos.entry_price,
            "exit_price": actual_fill,
            "qty": pos.qty,
            "bars_held": int(row["bar_index"] - pos.entry_bar),
            "pnl_abs": pnl_abs - exit_commission,
            "pnl_pct": (actual_fill / pos.entry_price - 1.0) * 100.0 if side == "long" else (pos.entry_price / actual_fill - 1.0) * 100.0,
            "mfe_abs": pos.mfe_abs,
            "mae_abs": pos.mae_abs,
            "mfe_pct": pos.mfe_abs / pos.entry_price * 100.0 if pos.entry_price else 0.0,
            "mae_pct": pos.mae_abs / pos.entry_price * 100.0 if pos.entry_price else 0.0,
            "exit_reason": exit_reason,
            "level_snapshot": pos.level,
            "trend_mode_at_entry": pos.trend_mode_at_entry,
            "be_flags_at_entry": pos.be_flags_at_entry,
            "be_flags_at_exit": state.be_flags_string(),
            "breakout_context": pos.breakout_context,
            "reverse_context": pos.reverse_context,
        }
    )

    if state.version == "v10":
        if exit_id == "Exit L1" and state.be1:
            state.long1BeLockBar = int(row["bar_index"])
        if exit_id == "Exit L2" and state.be2:
            state.long2BeLockBar = int(row["bar_index"])
        if exit_id == "Exit L3" and state.be3:
            state.long3BeLockBar = int(row["bar_index"])
        if exit_id == "Exit S1" and state.be1:
            state.short1BeLockBar = int(row["bar_index"])
        if exit_id == "Exit S2" and state.be2:
            state.short2BeLockBar = int(row["bar_index"])
        if exit_id == "Exit S3" and state.be3:
            state.short3BeLockBar = int(row["bar_index"])

    if pos.level in (1, 2, 3):
        state.clear_level_fields(pos.level)

    del state.open_positions[entry_id]
    return True


def _close_all_positions(state: BacktestState, row: pd.Series, reason: str, exit_id: str = "CloseAll") -> None:
    for entry_id in list(state.open_positions.keys()):
        _close_position(state, entry_id, row, float(row["close"]), reason, exit_id, "market")


def _enter_position(state: BacktestState, entry_id: str, side: str, qty: float, row: pd.Series, level: int, breakout_context: str = "", reverse_context: str = "") -> None:
    if qty <= 0:
        return

    if side == "long" and state.is_short():
        _close_all_positions(state, row, f"Reverse to {entry_id}", "ReverseClose")
    if side == "short" and state.is_long():
        _close_all_positions(state, row, f"Reverse to {entry_id}", "ReverseClose")

    fill_price = _slip_price(float(row["close"]), side, "entry", "market")
    entry_notional = fill_price * qty
    entry_commission = entry_notional * COMMISSION_PCT
    state.realized_equity -= entry_commission
    pos = PositionLot(
        entry_id=entry_id,
        side=side,
        qty=qty,
        entry_price=fill_price,
        entry_time=row["open_time"],
        entry_bar=int(row["bar_index"]),
        level=level,
        trend_mode_at_entry=state.trendMode,
        be_flags_at_entry=state.be_flags_string(),
        breakout_context=breakout_context,
        reverse_context=reverse_context,
    )
    state.open_positions[entry_id] = pos


def _update_mfe_mae(state: BacktestState, row: pd.Series) -> None:
    high = float(row["high"])
    low = float(row["low"])
    for pos in state.open_positions.values():
        if pos.side == "long":
            pos.mfe_abs = max(pos.mfe_abs, high - pos.entry_price)
            pos.mae_abs = min(pos.mae_abs, low - pos.entry_price)
        else:
            pos.mfe_abs = max(pos.mfe_abs, pos.entry_price - low)
            pos.mae_abs = min(pos.mae_abs, pos.entry_price - high)


def _tp_targets(features, row: pd.Series) -> dict[str, float]:
    zone = float(row["zoneOffset"])
    tp_zone = max(MINTICK, zone * (features.tp_offset_multiplier or 0.0))
    mid_high = float(row["midHigh"])
    mid_low = float(row["midLow"])
    smooth_res = float(row["smoothRes"])
    smooth_sup = float(row["smoothSup"])

    if features.tp_mode == "v7_original":
        return {
            "Long1": mid_high,
            "Long2": smooth_res,
            "Long3": smooth_res,
            "Short1": mid_low,
            "Short2": smooth_sup,
            "Short3": smooth_sup,
        }
    if features.tp_mode == "v8_partial":
        return {
            "Long1": mid_high - zone,
            "Long2": smooth_res,
            "Long3": smooth_res,
            "Short1": mid_low + zone,
            "Short2": smooth_sup,
            "Short3": smooth_sup,
        }
    if features.tp_mode == "v9_unified":
        return {
            "Long1": mid_high - tp_zone,
            "Long2": mid_high - tp_zone,
            "Long3": mid_high - tp_zone,
            "Short1": mid_low + tp_zone,
            "Short2": mid_low + tp_zone,
            "Short3": mid_low + tp_zone,
        }
    return {
        "Long1": mid_high - tp_zone,
        "Long2": smooth_res - tp_zone,
        "Long3": smooth_res - tp_zone,
        "Short1": mid_low + tp_zone,
        "Short2": mid_low + tp_zone,
        "Short3": mid_low + tp_zone,
    }


def run_backtest(data: pd.DataFrame, version: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    features = get_version_features(version)
    params = dict(BASE_PARAMS)
    state = BacktestState(version=version)
    equity_rows: list[dict[str, Any]] = []

    for _, row in data.iterrows():
        if any(pd.isna(row[col]) for col in ["smoothRes", "smoothSup", "smoothMid", "midHigh", "midLow", "zoneOffset", "maLine", "rsiVal", "atr_", "dynamicOffset"]):
            equity_rows.append({"open_time": row["open_time"], "equity": state.current_equity(float(row["close"])), "realized_equity": state.realized_equity})
            continue

        _update_mfe_mae(state, row)

        close = float(row["close"])
        open_ = float(row["open"])
        bullish = close > open_
        bearish = close < open_
        cooldown_ok = int(row["bar_index"]) - state.lastEntryBar >= int(row["cooldownBars"])
        is15m = True
        state.breakoutAboveBars = state.breakoutAboveBars + 1 if params["breakout_15m"] and is15m and close > float(row["superbuy"]) else 0
        state.breakoutBelowBars = state.breakoutBelowBars + 1 if params["breakout_15m"] and is15m and close < float(row["supersell"]) else 0
        breakout_ready = int(row["bar_index"]) - state.lastBreakoutBar >= params["breakout_cooldown_bars"]
        breakout_up = params["breakout_15m"] and is15m and state.trendMode == 0 and breakout_ready and state.breakoutAboveBars >= params["breakout_confirm_bars"]
        breakout_down = params["breakout_15m"] and is15m and state.trendMode == 0 and breakout_ready and state.breakoutBelowBars >= params["breakout_confirm_bars"]
        did_breakout_flip = False
        did_breakout_exit = False
        did_fast_exit = False
        reverse_lock_active = state.trendMode in (2, -2) and (int(row["bar_index"]) - state.lastEntryBar) < features.reverse_lock_bars
        reverse_in_channel = close >= float(row["smoothSup"]) and close <= float(row["smoothRes"])
        long1_be_lock = features.be_reentry_lock_bars is not None and state.long1BeLockBar is not None and (int(row["bar_index"]) - state.long1BeLockBar) < features.be_reentry_lock_bars
        long2_be_lock = features.be_reentry_lock_bars is not None and state.long2BeLockBar is not None and (int(row["bar_index"]) - state.long2BeLockBar) < features.be_reentry_lock_bars
        long3_be_lock = features.be_reentry_lock_bars is not None and state.long3BeLockBar is not None and (int(row["bar_index"]) - state.long3BeLockBar) < features.be_reentry_lock_bars
        short1_be_lock = features.be_reentry_lock_bars is not None and state.short1BeLockBar is not None and (int(row["bar_index"]) - state.short1BeLockBar) < features.be_reentry_lock_bars
        short2_be_lock = features.be_reentry_lock_bars is not None and state.short2BeLockBar is not None and (int(row["bar_index"]) - state.short2BeLockBar) < features.be_reentry_lock_bars
        short3_be_lock = features.be_reentry_lock_bars is not None and state.short3BeLockBar is not None and (int(row["bar_index"]) - state.short3BeLockBar) < features.be_reentry_lock_bars

        if state.trendMode == 0 and state.is_long() and close < float(row["downerLine"]):
            state.outsideLongBars += 1
        else:
            state.outsideLongBars = 0
        if state.trendMode == 0 and state.is_short() and close > float(row["uperLine"]):
            state.outsideShortBars += 1
        else:
            state.outsideShortBars = 0

        avg_price = state.position_avg_price()
        if features.reverse_breakeven_on_channel and state.trendMode == 2 and state.is_long() and reverse_in_channel and avg_price is not None:
            base_val = avg_price if state.reverseStopPx is None else state.reverseStopPx
            state.reverseStopPx = max(base_val, avg_price)
        if features.reverse_breakeven_on_channel and state.trendMode == -2 and state.is_short() and reverse_in_channel and avg_price is not None:
            base_val = avg_price if state.reverseStopPx is None else state.reverseStopPx
            state.reverseStopPx = min(base_val, avg_price)

        if breakout_up and state.trendMode != 1:
            _close_all_positions(state, row, "Breakout Flip Up")
            qty = state.current_equity(close) / close
            state.nLong = 0
            state.nShort = 0
            state.entryPx1 = None
            state.entryPx2 = None
            state.entryPx3 = None
            state.be1 = False
            state.be2 = False
            state.be3 = False
            state.trendMode = 1
            state.breakoutInsideBars = 0
            state.breakoutEntryBar = int(row["bar_index"])
            state.lastBreakoutBar = int(row["bar_index"])
            state.reverseStopPx = None
            state.reverseTpPx = None
            state.lastEntryBar = int(row["bar_index"])
            _enter_position(state, "TrendLong", "long", qty, row, 0, breakout_context="breakout_up")
            did_breakout_flip = True

        if breakout_down and state.trendMode != -1:
            _close_all_positions(state, row, "Breakout Flip Down")
            qty = state.current_equity(close) / close
            state.nLong = 0
            state.nShort = 0
            state.entryPx1 = None
            state.entryPx2 = None
            state.entryPx3 = None
            state.be1 = False
            state.be2 = False
            state.be3 = False
            state.trendMode = -1
            state.breakoutInsideBars = 0
            state.breakoutEntryBar = int(row["bar_index"])
            state.lastBreakoutBar = int(row["bar_index"])
            state.reverseStopPx = None
            state.reverseTpPx = None
            state.lastEntryBar = int(row["bar_index"])
            _enter_position(state, "TrendShort", "short", qty, row, 0, breakout_context="breakout_down")
            did_breakout_flip = True

        if not did_breakout_flip and state.trendMode == 1:
            held_long_enough = state.breakoutEntryBar is not None and (int(row["bar_index"]) - state.breakoutEntryBar) >= params["breakout_min_hold_bars"]
            state.breakoutInsideBars = state.breakoutInsideBars + 1 if held_long_enough and close <= float(row["smoothRes"]) else 0
            breakout_long_tp = state.is_long() and float(row["rsiVal"]) >= params["breakout_rsi_high"]
            breakout_long_stop = state.breakoutInsideBars >= params["breakout_back_bars"]
            if breakout_long_tp:
                qty = state.current_equity(close) / close
                state.trendMode = -2
                state.breakoutInsideBars = 0
                state.breakoutEntryBar = None
                state.reverseStopPx = _value_or_none(row["reverseShortStopPxRef"])
                state.reverseTpPx = float(row["smoothMid"])
                state.lastBreakoutBar = int(row["bar_index"])
                state.lastEntryBar = int(row["bar_index"])
                _enter_position(state, "RevShort", "short", qty, row, 0, breakout_context="breakout_long_tp", reverse_context="rev_short")
                did_breakout_exit = True
            elif breakout_long_stop:
                _close_position(state, "TrendLong", row, close, "Breakout Stop", "TrendLongStop", "market")
                state.trendMode = 0
                state.breakoutInsideBars = 0
                state.breakoutEntryBar = None
                state.reverseStopPx = None
                state.reverseTpPx = None
                state.lastBreakoutBar = int(row["bar_index"])
                state.lastEntryBar = int(row["bar_index"])
                did_breakout_exit = True

        if not did_breakout_flip and state.trendMode == -1:
            held_long_enough = state.breakoutEntryBar is not None and (int(row["bar_index"]) - state.breakoutEntryBar) >= params["breakout_min_hold_bars"]
            state.breakoutInsideBars = state.breakoutInsideBars + 1 if held_long_enough and close >= float(row["smoothSup"]) else 0
            breakout_short_tp = state.is_short() and float(row["rsiVal"]) <= params["breakout_rsi_low"]
            breakout_short_stop = state.breakoutInsideBars >= params["breakout_back_bars"]
            if breakout_short_tp:
                qty = state.current_equity(close) / close
                state.trendMode = 2
                state.breakoutInsideBars = 0
                state.breakoutEntryBar = None
                state.reverseStopPx = _value_or_none(row["reverseLongStopPxRef"])
                state.reverseTpPx = float(row["smoothMid"])
                state.lastBreakoutBar = int(row["bar_index"])
                state.lastEntryBar = int(row["bar_index"])
                _enter_position(state, "RevLong", "long", qty, row, 0, breakout_context="breakout_short_tp", reverse_context="rev_long")
                did_breakout_exit = True
            elif breakout_short_stop:
                _close_position(state, "TrendShort", row, close, "Breakout Stop", "TrendShortStop", "market")
                state.trendMode = 0
                state.breakoutInsideBars = 0
                state.breakoutEntryBar = None
                state.reverseStopPx = None
                state.reverseTpPx = None
                state.lastBreakoutBar = int(row["bar_index"])
                state.lastEntryBar = int(row["bar_index"])
                did_breakout_exit = True

        if not did_breakout_flip and not did_breakout_exit and state.trendMode == 2 and state.reverseStopPx is not None and not reverse_lock_active and "RevLong" in state.open_positions:
            state.reverseTpPx = float(row["smoothMid"])
            hit = _first_exit_hit("long", state.reverseStopPx, state.reverseTpPx, row)
            if hit is not None:
                hit_kind, hit_price = hit
                _close_position(state, "RevLong", row, hit_price, "Reverse TP Channel" if hit_kind == "limit" else "Reverse Stop", "Exit RevLong", "limit" if hit_kind == "limit" else "stop")

        if not did_breakout_flip and not did_breakout_exit and state.trendMode == -2 and state.reverseStopPx is not None and not reverse_lock_active and "RevShort" in state.open_positions:
            state.reverseTpPx = float(row["smoothMid"])
            hit = _first_exit_hit("short", state.reverseStopPx, state.reverseTpPx, row)
            if hit is not None:
                hit_kind, hit_price = hit
                _close_position(state, "RevShort", row, hit_price, "Reverse TP Channel" if hit_kind == "limit" else "Reverse Stop", "Exit RevShort", "limit" if hit_kind == "limit" else "stop")

        tp_targets = _tp_targets(features, row)
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit:
            if state.is_long() and state.entryPx1 is not None and close >= tp_targets["Long1"]:
                _close_position(state, "Long1", row, close, "TP L1", "Close L1", "market")
                state.lastEntryBar = int(row["bar_index"])
            if state.is_long() and state.entryPx2 is not None and close >= tp_targets["Long2"]:
                _close_position(state, "Long2", row, close, "TP L2", "Close L2", "market")
                state.lastEntryBar = int(row["bar_index"])
            if state.is_long() and state.entryPx3 is not None and close >= tp_targets["Long3"]:
                _close_position(state, "Long3", row, close, "TP L3", "Close L3", "market")
                state.lastEntryBar = int(row["bar_index"])
            if state.is_short() and state.entryPx1 is not None and close <= tp_targets["Short1"]:
                _close_position(state, "Short1", row, close, "TP S1", "Close S1", "market")
                state.lastEntryBar = int(row["bar_index"])
            if state.is_short() and state.entryPx2 is not None and close <= tp_targets["Short2"]:
                _close_position(state, "Short2", row, close, "TP S2", "Close S2", "market")
                state.lastEntryBar = int(row["bar_index"])
            if state.is_short() and state.entryPx3 is not None and close <= tp_targets["Short3"]:
                _close_position(state, "Short3", row, close, "TP S3", "Close S3", "market")
                state.lastEntryBar = int(row["bar_index"])

        be_snapshot = {1: state.be1, 2: state.be2, 3: state.be3}

        def time_check(ent_bar: int, ep: float, is_buy: bool) -> tuple[bool, bool]:
            bars_held = int(row["bar_index"]) - ent_bar
            profitable = close > ep if is_buy else close < ep
            should_cut = bars_held >= int(row["timeStopBars"]) * 2 and not profitable
            should_be = bars_held >= int(row["timeStopBars"]) and profitable
            return should_cut, should_be

        def no_progress_check(entry_id: str, ent_bar: int, ep: float, is_buy: bool) -> bool:
            if not features.no_progress_time_stop:
                return False
            pos = state.open_positions.get(entry_id)
            if pos is None:
                return False
            bars_held = int(row["bar_index"]) - ent_bar
            profitable = close > ep if is_buy else close < ep
            return bars_held >= int(row["timeStopBars"]) and not profitable and pos.mfe_abs < float(row["zoneOffset"])

        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and state.is_long() and state.entryPx1 is not None:
            cut, set_be = time_check(state.entryBar1, state.entryPx1, True)
            if no_progress_check("Long1", state.entryBar1, state.entryPx1, True):
                _close_position(state, "Long1", row, close, "NoProgress L1", "NoProgress L1", "market")
            elif cut:
                _close_position(state, "Long1", row, close, "TimeStop L1", "TimeStop L1", "market")
            elif set_be and not state.be1:
                state.be1 = True
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and state.is_long() and state.entryPx2 is not None:
            cut, set_be = time_check(state.entryBar2, state.entryPx2, True)
            if no_progress_check("Long2", state.entryBar2, state.entryPx2, True):
                _close_position(state, "Long2", row, close, "NoProgress L2", "NoProgress L2", "market")
            elif cut:
                _close_position(state, "Long2", row, close, "TimeStop L2", "TimeStop L2", "market")
            elif set_be and not state.be2:
                state.be2 = True
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and state.is_long() and state.entryPx3 is not None:
            cut, set_be = time_check(state.entryBar3, state.entryPx3, True)
            if no_progress_check("Long3", state.entryBar3, state.entryPx3, True):
                _close_position(state, "Long3", row, close, "NoProgress L3", "NoProgress L3", "market")
            elif cut:
                _close_position(state, "Long3", row, close, "TimeStop L3", "TimeStop L3", "market")
            elif set_be and not state.be3:
                state.be3 = True
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and state.is_short() and state.entryPx1 is not None:
            cut, set_be = time_check(state.entryBar1, state.entryPx1, False)
            if no_progress_check("Short1", state.entryBar1, state.entryPx1, False):
                _close_position(state, "Short1", row, close, "NoProgress S1", "NoProgress S1", "market")
            elif cut:
                _close_position(state, "Short1", row, close, "TimeStop S1", "TimeStop S1", "market")
            elif set_be and not state.be1:
                state.be1 = True
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and state.is_short() and state.entryPx2 is not None:
            cut, set_be = time_check(state.entryBar2, state.entryPx2, False)
            if no_progress_check("Short2", state.entryBar2, state.entryPx2, False):
                _close_position(state, "Short2", row, close, "NoProgress S2", "NoProgress S2", "market")
            elif cut:
                _close_position(state, "Short2", row, close, "TimeStop S2", "TimeStop S2", "market")
            elif set_be and not state.be2:
                state.be2 = True
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and state.is_short() and state.entryPx3 is not None:
            cut, set_be = time_check(state.entryBar3, state.entryPx3, False)
            if no_progress_check("Short3", state.entryBar3, state.entryPx3, False):
                _close_position(state, "Short3", row, close, "NoProgress S3", "NoProgress S3", "market")
            elif cut:
                _close_position(state, "Short3", row, close, "TimeStop S3", "TimeStop S3", "market")
            elif set_be and not state.be3:
                state.be3 = True

        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and state.is_long():
            for entry_id, level in [("Long1", 1), ("Long2", 2), ("Long3", 3)]:
                ep = getattr(state, f"entryPx{level}")
                if ep is None or entry_id not in state.open_positions:
                    continue
                stop = _get_stop(ep, True, be_snapshot[level], max(float(row["dynamicOffset"]), 1.0), params)
                hit = _first_exit_hit("long", stop, None, row)
                if hit is not None:
                    _close_position(state, entry_id, row, hit[1], f"Exit L{level}", f"Exit L{level}", "stop")
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and state.is_short():
            for entry_id, level in [("Short1", 1), ("Short2", 2), ("Short3", 3)]:
                ep = getattr(state, f"entryPx{level}")
                if ep is None or entry_id not in state.open_positions:
                    continue
                stop = _get_stop(ep, False, be_snapshot[level], max(float(row["dynamicOffset"]), 1.0), params)
                hit = _first_exit_hit("short", stop, None, row)
                if hit is not None:
                    _close_position(state, entry_id, row, hit[1], f"Exit S{level}", f"Exit S{level}", "stop")

        if features.mfe_zone_breakeven and state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit:
            zone_offset = float(row["zoneOffset"])
            for entry_id, level in [("Long1", 1), ("Long2", 2), ("Long3", 3)]:
                pos = state.open_positions.get(entry_id)
                if pos is None:
                    continue
                protect_price = pos.entry_price * (1.0 + params["be_buffer"])
                if pos.mfe_abs >= zone_offset and close <= protect_price:
                    _close_position(state, entry_id, row, close, f"MFEProtect L{level}", f"MFEProtect L{level}", "market")
                    state.lastEntryBar = int(row["bar_index"])
            for entry_id, level in [("Short1", 1), ("Short2", 2), ("Short3", 3)]:
                pos = state.open_positions.get(entry_id)
                if pos is None:
                    continue
                protect_price = pos.entry_price * (1.0 - params["be_buffer"])
                if pos.mfe_abs >= zone_offset and close >= protect_price:
                    _close_position(state, entry_id, row, close, f"MFEProtect S{level}", f"MFEProtect S{level}", "market")
                    state.lastEntryBar = int(row["bar_index"])

        if features.level0_mfe_zone_breakeven and not did_breakout_flip and not did_breakout_exit:
            zone_offset = float(row["zoneOffset"])
            for entry_id in ["TrendLong", "RevLong"]:
                pos = state.open_positions.get(entry_id)
                if pos is None:
                    continue
                bars_held = int(row["bar_index"]) - pos.entry_bar
                protect_price = pos.entry_price * (1.0 + params["be_buffer"])
                can_protect = entry_id == "TrendLong" and bars_held >= params["breakout_min_hold_bars"]
                can_protect = can_protect or (entry_id == "RevLong" and not reverse_lock_active)
                if can_protect and pos.mfe_abs >= zone_offset and close <= protect_price:
                    _close_position(state, entry_id, row, close, f"MFEProtect {entry_id}", f"MFEProtect {entry_id}", "market")
                    state.breakoutInsideBars = 0
                    state.breakoutEntryBar = None
                    state.reverseStopPx = None
                    state.reverseTpPx = None
                    state.lastBreakoutBar = int(row["bar_index"])
                    state.lastEntryBar = int(row["bar_index"])
                    if state.is_flat():
                        state.trendMode = 0
            for entry_id in ["TrendShort", "RevShort"]:
                pos = state.open_positions.get(entry_id)
                if pos is None:
                    continue
                bars_held = int(row["bar_index"]) - pos.entry_bar
                protect_price = pos.entry_price * (1.0 - params["be_buffer"])
                can_protect = entry_id == "TrendShort" and bars_held >= params["breakout_min_hold_bars"]
                can_protect = can_protect or (entry_id == "RevShort" and not reverse_lock_active)
                if can_protect and pos.mfe_abs >= zone_offset and close >= protect_price:
                    _close_position(state, entry_id, row, close, f"MFEProtect {entry_id}", f"MFEProtect {entry_id}", "market")
                    state.breakoutInsideBars = 0
                    state.breakoutEntryBar = None
                    state.reverseStopPx = None
                    state.reverseTpPx = None
                    state.lastBreakoutBar = int(row["bar_index"])
                    state.lastEntryBar = int(row["bar_index"])
                    if state.is_flat():
                        state.trendMode = 0

        outside_long_stop = state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and state.is_long() and state.outsideLongBars >= params["outside_stop_bars"]
        outside_short_stop = state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and state.is_short() and state.outsideShortBars >= params["outside_stop_bars"]
        if outside_long_stop:
            for entry_id in ["Long1", "Long2", "Long3"]:
                _close_position(state, entry_id, row, close, f"OutsideStop {entry_id}", f"Outside {entry_id}", "market")
            state.outsideLongBars = 0
            state.outsideShortBars = 0
            state.lastEntryBar = int(row["bar_index"])
            did_fast_exit = True
        if outside_short_stop:
            for entry_id in ["Short1", "Short2", "Short3"]:
                _close_position(state, entry_id, row, close, f"OutsideStop {entry_id}", f"Outside {entry_id}", "market")
            state.outsideLongBars = 0
            state.outsideShortBars = 0
            state.lastEntryBar = int(row["bar_index"])
            did_fast_exit = True

        if state.is_flat() and not did_breakout_flip:
            if state.trendMode in (2, -2):
                state.lastBreakoutBar = int(row["bar_index"])
                state.lastEntryBar = int(row["bar_index"])
            state.nLong = 0
            state.nShort = 0
            state.be1 = False
            state.be2 = False
            state.be3 = False
            state.entryPx1 = None
            state.entryPx2 = None
            state.entryPx3 = None
            state.breakoutInsideBars = 0
            state.breakoutEntryBar = None
            state.reverseStopPx = None
            state.reverseTpPx = None
            state.outsideLongBars = 0
            state.outsideShortBars = 0
            state.trendMode = 0

        near_mid_low = close <= float(row["midLow"]) + float(row["zoneOffset"])
        near_sup = close <= float(row["smoothSup"]) + float(row["zoneOffset"])
        near_downer = close <= float(row["downerLine"]) + float(row["zoneOffset"])
        near_mid_high = close >= float(row["midHigh"]) - float(row["zoneOffset"])
        near_res = close >= float(row["smoothRes"]) - float(row["zoneOffset"])
        near_uper = close >= float(row["uperLine"]) - float(row["zoneOffset"])

        max_deep_overshoot = float(row["channelWidth"]) * (features.deep_entry_overshoot_pct or 0.0)
        long2_overshoot_ok = close >= float(row["smoothSup"]) - max_deep_overshoot
        long3_overshoot_ok = close >= float(row["downerLine"]) - max_deep_overshoot
        short2_overshoot_ok = close <= float(row["smoothRes"]) + max_deep_overshoot
        short3_overshoot_ok = close <= float(row["uperLine"]) + max_deep_overshoot
        long2_reclaim_ok = bullish and close >= float(row["smoothSup"])
        long3_reclaim_ok = bullish and close >= float(row["downerLine"])
        short2_reclaim_ok = bearish and close <= float(row["smoothRes"])
        short3_reclaim_ok = bearish and close <= float(row["uperLine"])

        long_ma_filter_ok = (not params["enable_ma_entry_filter"]) or bool(row["uptrend"])
        short_ma_filter_ok = (not params["enable_ma_entry_filter"]) or bool(row["downtrend"])
        long1_knife_entry = close < float(row["midLow"])
        short1_knife_entry = close > float(row["midHigh"])
        long1_reclaim_entry_ok = (not features.l1_reclaim_entry) or close >= float(row["midLow"])
        short1_reclaim_entry_ok = (not features.l1_reclaim_entry) or close <= float(row["midHigh"])
        long1_alloc_pct = params["alloc_pct1"] * 0.5 if features.l1_knife_half_size and long1_knife_entry else params["alloc_pct1"]
        short1_alloc_pct = params["alloc_pct1"] * 0.5 if features.l1_knife_half_size and short1_knife_entry else params["alloc_pct1"]

        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and not did_fast_exit and state.is_flat() and cooldown_ok and (not long1_be_lock) and long_ma_filter_ok and near_mid_low and long1_reclaim_entry_ok and bullish and float(row["rsiVal"]) < params["rsi_long"]:
            qty = state.current_equity(close) * (long1_alloc_pct / 100.0) / close
            _enter_position(state, "Long1", "long", qty, row, 1)
            state.nLong = 1
            state.set_level_entry_fields(1, close, int(row["bar_index"]))
            state.lastEntryBar = int(row["bar_index"])
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and not did_fast_exit and state.is_long() and state.nLong == 1 and cooldown_ok and (not long2_be_lock) and near_sup and float(row["rsiVal"]) < params["rsi_long"] + 10 and (not features.deep_entry_reclaim or (long2_overshoot_ok and long2_reclaim_ok)):
            qty = state.current_equity(close) * (params["alloc_pct2"] / 100.0) / close
            _enter_position(state, "Long2", "long", qty, row, 2)
            state.nLong = 2
            state.set_level_entry_fields(2, close, int(row["bar_index"]))
            state.lastEntryBar = int(row["bar_index"])
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and not did_fast_exit and state.is_long() and state.nLong == 2 and cooldown_ok and (not long3_be_lock) and near_downer and float(row["rsiVal"]) < params["rsi_long"] + 10 and (not features.deep_entry_reclaim or (long3_overshoot_ok and long3_reclaim_ok)):
            qty = state.current_equity(close) * (params["alloc_pct3"] / 100.0) / close
            _enter_position(state, "Long3", "long", qty, row, 3)
            state.nLong = 3
            state.set_level_entry_fields(3, close, int(row["bar_index"]))
            state.lastEntryBar = int(row["bar_index"])
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and not did_fast_exit and state.is_flat() and cooldown_ok and (not short1_be_lock) and params["enable_short"] and short_ma_filter_ok and near_mid_high and short1_reclaim_entry_ok and bearish and float(row["rsiVal"]) > params["rsi_short"]:
            qty = state.current_equity(close) * (short1_alloc_pct / 100.0) / close
            _enter_position(state, "Short1", "short", qty, row, 1)
            state.nShort = 1
            state.set_level_entry_fields(1, close, int(row["bar_index"]))
            state.lastEntryBar = int(row["bar_index"])
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and not did_fast_exit and state.is_short() and state.nShort == 1 and cooldown_ok and (not short2_be_lock) and near_res and float(row["rsiVal"]) > params["rsi_short"] - 10 and (not features.deep_entry_reclaim or (short2_overshoot_ok and short2_reclaim_ok)):
            qty = state.current_equity(close) * (params["alloc_pct2"] / 100.0) / close
            _enter_position(state, "Short2", "short", qty, row, 2)
            state.nShort = 2
            state.set_level_entry_fields(2, close, int(row["bar_index"]))
            state.lastEntryBar = int(row["bar_index"])
        if state.trendMode == 0 and not did_breakout_flip and not did_breakout_exit and not did_fast_exit and state.is_short() and state.nShort == 2 and cooldown_ok and (not short3_be_lock) and near_uper and float(row["rsiVal"]) > params["rsi_short"] - 10 and (not features.deep_entry_reclaim or (short3_overshoot_ok and short3_reclaim_ok)):
            qty = state.current_equity(close) * (params["alloc_pct3"] / 100.0) / close
            _enter_position(state, "Short3", "short", qty, row, 3)
            state.nShort = 3
            state.set_level_entry_fields(3, close, int(row["bar_index"]))
            state.lastEntryBar = int(row["bar_index"])

        equity_rows.append(
            {
                "open_time": row["open_time"],
                "equity": state.current_equity(close),
                "realized_equity": state.realized_equity,
            }
        )

    if state.open_positions:
        last_row = data.iloc[-1]
        for entry_id in list(state.open_positions.keys()):
            _close_position(state, entry_id, last_row, float(last_row["close"]), "Force Close End", "ForceClose", "market")
        equity_rows[-1]["equity"] = state.realized_equity
        equity_rows[-1]["realized_equity"] = state.realized_equity

    equity_df = pd.DataFrame(equity_rows)
    trades_df = pd.DataFrame(state.trades)
    return equity_df, trades_df
