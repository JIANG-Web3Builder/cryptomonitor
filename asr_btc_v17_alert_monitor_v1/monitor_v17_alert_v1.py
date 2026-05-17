from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import engine_v17_alert_v1 as engine
from config_channel_v4 import BASE_PARAMS, INITIAL_CAPITAL
from data_loader_channel_v4 import REQUIRED_COLUMNS
from indicators_channel_v4 import compute_indicators
from strategy_versions_channel_v4 import get_version_features

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = ROOT_DIR / "config_alert_v1.json"

ENTRY_ID_CN = {
    "Long1": "通道低位多单第1层",
    "Long2": "通道支撑多单第2层",
    "Long3": "通道下沿深度多单第3层",
    "Short1": "通道高位空单第1层",
    "Short2": "通道压力空单第2层",
    "Short3": "通道上沿深度空单第3层",
    "TrendLong": "向上突破趋势多单",
    "TrendShort": "向下突破趋势空单",
    "RevLong": "趋势空头止盈后的反转多单",
    "RevShort": "趋势多头止盈后的反转空单",
}

EXIT_REASON_CN = {
    "TP L1": "多单第1层止盈",
    "TP L2": "多单第2层止盈",
    "TP L3": "多单第3层止盈",
    "TP S1": "空单第1层止盈",
    "TP S2": "空单第2层止盈",
    "TP S3": "空单第3层止盈",
    "Exit L1": "多单第1层止损/保本退出",
    "Exit L2": "多单第2层止损/保本退出",
    "Exit L3": "多单第3层止损/保本退出",
    "Exit S1": "空单第1层止损/保本退出",
    "Exit S2": "空单第2层止损/保本退出",
    "Exit S3": "空单第3层止损/保本退出",
    "Breakout Flip Up": "向上突破翻多，旧仓全部平掉",
    "Breakout Flip Down": "向下突破翻空，旧仓全部平掉",
    "Breakout Stop": "突破趋势回到通道内，趋势单退出",
    "Reverse TP Channel": "反转单到达通道中线止盈",
    "Reverse Stop": "反转单触发止损",
    "NoProgress L1": "多单第1层无进展退出",
    "NoProgress L2": "多单第2层无进展退出",
    "NoProgress L3": "多单第3层无进展退出",
    "NoProgress S1": "空单第1层无进展退出",
    "NoProgress S2": "空单第2层无进展退出",
    "NoProgress S3": "空单第3层无进展退出",
}

SIDE_CN = {"long": "做多", "short": "做空"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT_DIR / path


def load_market_data(config: dict[str, Any]) -> pd.DataFrame:
    data_path = resolve_path(config["data_csv_path"])
    df = pd.read_csv(data_path)
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"行情文件缺少必要字段: {missing}")
    df = df[REQUIRED_COLUMNS].copy()
    df["open_time"] = pd.to_datetime(df["open_time"], utc=False)
    df = df.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    history_start = pd.Timestamp(config.get("history_start_time", "2024-01-01 00:00:00"))
    df = df[df["open_time"] >= history_start].reset_index(drop=True)
    if config.get("only_closed_latest_bar", True) and len(df) > 1:
        latest_time = df["open_time"].iloc[-1]
        expected_next = latest_time + pd.Timedelta(minutes=int(config.get("timeframe_minutes", 15)))
        if pd.Timestamp.utcnow().tz_localize(None) < expected_next:
            df = df.iloc[:-1].copy()
    max_bars = int(config.get("max_bars", 12000))
    if max_bars > 0 and len(df) > max_bars:
        df = df.tail(max_bars).reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["bar_index"] = range(len(df))
    return df


def capture_v17_events(data: pd.DataFrame, version: str) -> tuple[list[dict[str, Any]], pd.DataFrame, pd.DataFrame]:
    events: list[dict[str, Any]] = []
    original_enter = engine._enter_position
    original_close = engine._close_position

    def wrapped_enter(state, entry_id, side, qty, row, level, breakout_context="", reverse_context=""):
        before = set(state.open_positions.keys())
        original_enter(state, entry_id, side, qty, row, level, breakout_context, reverse_context)
        if entry_id in state.open_positions and entry_id not in before:
            pos = state.open_positions[entry_id]
            events.append(
                {
                    "event_type": "entry",
                    "version": state.version,
                    "symbol": "",
                    "time": pd.Timestamp(pos.entry_time).strftime("%Y-%m-%d %H:%M:%S"),
                    "bar_index": int(pos.entry_bar),
                    "side": pos.side,
                    "entry_id": pos.entry_id,
                    "level": int(pos.level),
                    "price": float(pos.entry_price),
                    "qty": float(pos.qty),
                    "notional": float(pos.entry_price * pos.qty),
                    "trend_mode": int(pos.trend_mode_at_entry),
                    "breakout_context": pos.breakout_context,
                    "reverse_context": pos.reverse_context,
                    "close_price": float(row["close"]),
                    "rsi": float(row["rsiVal"]),
                    "smooth_res": float(row["smoothRes"]),
                    "smooth_sup": float(row["smoothSup"]),
                    "smooth_mid": float(row["smoothMid"]),
                    "mid_high": float(row["midHigh"]),
                    "mid_low": float(row["midLow"]),
                    "zone_offset": float(row["zoneOffset"]),
                    "dynamic_offset": float(row["dynamicOffset"]),
                }
            )

    def wrapped_close(state, entry_id, row, fill_price, exit_reason, exit_id, order_kind):
        pos = state.open_positions.get(entry_id)
        if pos is None:
            return original_close(state, entry_id, row, fill_price, exit_reason, exit_id, order_kind)
        before_count = len(state.trades)
        result = original_close(state, entry_id, row, fill_price, exit_reason, exit_id, order_kind)
        if result and len(state.trades) > before_count:
            trade = state.trades[-1]
            events.append(
                {
                    "event_type": "exit",
                    "version": state.version,
                    "symbol": "",
                    "time": pd.Timestamp(trade["exit_time"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "bar_index": int(row["bar_index"]),
                    "side": str(trade["side"]),
                    "entry_id": str(trade["entry_id"]),
                    "exit_id": str(trade["exit_id"]),
                    "exit_reason": str(trade["exit_reason"]),
                    "level": int(trade["level_snapshot"]),
                    "entry_time": pd.Timestamp(trade["entry_time"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_price": float(trade["entry_price"]),
                    "price": float(trade["exit_price"]),
                    "qty": float(trade["qty"]),
                    "notional": float(trade["exit_price"] * trade["qty"]),
                    "bars_held": int(trade["bars_held"]),
                    "pnl_abs": float(trade["pnl_abs"]),
                    "pnl_pct": float(trade["pnl_pct"]),
                    "mfe_pct": float(trade["mfe_pct"]),
                    "mae_pct": float(trade["mae_pct"]),
                    "trend_mode": int(trade["trend_mode_at_entry"]),
                    "breakout_context": str(trade["breakout_context"]),
                    "reverse_context": str(trade["reverse_context"]),
                    "close_price": float(row["close"]),
                    "rsi": float(row["rsiVal"]),
                    "smooth_res": float(row["smoothRes"]),
                    "smooth_sup": float(row["smoothSup"]),
                    "smooth_mid": float(row["smoothMid"]),
                    "mid_high": float(row["midHigh"]),
                    "mid_low": float(row["midLow"]),
                    "zone_offset": float(row["zoneOffset"]),
                    "dynamic_offset": float(row["dynamicOffset"]),
                }
            )
        return result

    engine._enter_position = wrapped_enter
    engine._close_position = wrapped_close
    try:
        equity_df, trades_df = engine.run_backtest(data, version)
    finally:
        engine._enter_position = original_enter
        engine._close_position = original_close
    return events, equity_df, trades_df


def event_key(event: dict[str, Any]) -> str:
    if event["event_type"] == "entry":
        return f"entry|{event['time']}|{event['entry_id']}|{event['side']}|{event['price']:.2f}"
    return f"exit|{event['time']}|{event['entry_id']}|{event.get('exit_id', '')}|{event['side']}|{event['price']:.2f}"


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return load_json(path)
    return {"sent_keys": [], "last_run_at": "", "last_seen_event_time": "", "initialized": False}


def calculate_size_hint(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    risk = config.get("risk", {})
    equity = float(risk.get("account_equity_usdt", INITIAL_CAPITAL))
    entry_id = str(event.get("entry_id", ""))
    alloc_map = {
        "Long1": "long1_alloc_pct",
        "Long2": "long2_alloc_pct",
        "Long3": "long3_alloc_pct",
        "Short1": "short1_alloc_pct",
        "Short2": "short2_alloc_pct",
        "Short3": "short3_alloc_pct",
        "TrendLong": "trend_alloc_pct",
        "TrendShort": "trend_alloc_pct",
        "RevLong": "reverse_alloc_pct",
        "RevShort": "reverse_alloc_pct",
    }
    alloc = float(risk.get(alloc_map.get(entry_id, "max_total_position_pct"), 0.0))
    notional = equity * alloc
    price = float(event.get("price", 0.0))
    qty = notional / price if price > 0 else 0.0
    return {"equity": equity, "alloc_pct": alloc, "notional": notional, "qty": qty}


def infer_stop_hint(event: dict[str, Any]) -> str:
    side = str(event.get("side", ""))
    entry_id = str(event.get("entry_id", ""))
    dynamic_offset = float(event.get("dynamic_offset", 0.0))
    price = float(event.get("price", 0.0))
    if side == "long":
        stop = price - float(BASE_PARAMS["stop_mult"]) * max(dynamic_offset, 1.0)
        if entry_id == "RevLong":
            stop = float(event.get("smooth_sup", stop))
        return f"参考止损：{stop:.2f}；若后续触发保本条件，按策略移动到入场价上方约 {BASE_PARAMS['be_buffer'] * 100:.2f}%。"
    if side == "short":
        stop = price + float(BASE_PARAMS["stop_mult"]) * max(dynamic_offset, 1.0)
        if entry_id == "RevShort":
            stop = float(event.get("smooth_res", stop))
        return f"参考止损：{stop:.2f}；若后续触发保本条件，按策略移动到入场价下方约 {BASE_PARAMS['be_buffer'] * 100:.2f}%。"
    return "参考止损：请根据当前仓位和交易所风险参数手动复核。"


def infer_target_hint(event: dict[str, Any]) -> str:
    side = str(event.get("side", ""))
    entry_id = str(event.get("entry_id", ""))
    zone_offset = float(event.get("zone_offset", 0.0))
    tp_zone = max(0.01, zone_offset * 0.5)
    if entry_id == "Long1":
        target = float(event.get("mid_high", 0.0)) - tp_zone
        return f"第一目标：midHigh 附近 {target:.2f}。"
    if entry_id in ("Long2", "Long3"):
        target = float(event.get("smooth_res", 0.0)) - tp_zone
        return f"第一目标：smoothRes 附近 {target:.2f}。"
    if entry_id in ("Short1", "Short2", "Short3"):
        target = float(event.get("mid_low", 0.0)) + tp_zone
        return f"第一目标：midLow 附近 {target:.2f}。"
    if entry_id in ("RevLong", "RevShort"):
        return f"第一目标：通道中线 smoothMid 附近 {float(event.get('smooth_mid', 0.0)):.2f}。"
    if entry_id == "TrendLong":
        return f"趋势保护：最少持有 {BASE_PARAMS['breakout_min_hold_bars']} 根15m后，若回到 smoothRes 内需警惕趋势失败；RSI 达 {BASE_PARAMS['breakout_rsi_high']} 附近可能触发反向。"
    if entry_id == "TrendShort":
        return f"趋势保护：最少持有 {BASE_PARAMS['breakout_min_hold_bars']} 根15m后，若回到 smoothSup 内需警惕趋势失败；RSI 达 {BASE_PARAMS['breakout_rsi_low']} 附近可能触发反向。"
    if side:
        return "第一目标：按策略后续出场告警执行。"
    return "第一目标：无。"


def format_event_message(event: dict[str, Any], config: dict[str, Any]) -> str:
    symbol = config.get("symbol", "BTCUSDT")
    version = config.get("version", "v17")
    event_name = "开仓/加仓" if event["event_type"] == "entry" else "平仓/减仓"
    side_text = SIDE_CN.get(str(event.get("side")), str(event.get("side", "")))
    entry_id = str(event.get("entry_id", ""))
    action_text = ENTRY_ID_CN.get(entry_id, entry_id)
    lines = [
        f"【ASR BTC {version} 策略动作提醒】",
        f"品种：{symbol}",
        f"周期：{config.get('timeframe_minutes', 15)}分钟",
        f"时间：{event['time']}",
        f"动作：{event_name}",
        f"方向：{side_text}",
        f"信号：{action_text}",
        f"价格：{float(event.get('price', 0.0)):.2f}",
        f"RSI：{float(event.get('rsi', 0.0)):.2f}",
        f"通道：上轨 {float(event.get('smooth_res', 0.0)):.2f} / 中线 {float(event.get('smooth_mid', 0.0)):.2f} / 下轨 {float(event.get('smooth_sup', 0.0)):.2f}",
    ]
    if event["event_type"] == "entry":
        size_hint = calculate_size_hint(event, config)
        lines.extend(
            [
                f"建议仓位：按配置权益 {size_hint['equity']:.2f} USDT 的 {size_hint['alloc_pct'] * 100:.1f}% 估算，名义仓位约 {size_hint['notional']:.2f} USDT，数量约 {size_hint['qty']:.6f} BTC。",
                infer_stop_hint(event),
                infer_target_hint(event),
                f"杠杆提醒：{config.get('risk', {}).get('leverage_hint', '')}",
            ]
        )
        reminders = config.get("reminders", {}).get("entry", [])
    else:
        reason = str(event.get("exit_reason", ""))
        reason_text = EXIT_REASON_CN.get(reason, reason)
        lines.extend(
            [
                f"出场原因：{reason_text}",
                f"原始入场时间：{event.get('entry_time', '')}",
                f"原始入场价：{float(event.get('entry_price', 0.0)):.2f}",
                f"持仓K线数：{int(event.get('bars_held', 0))}",
                f"本笔估算PnL：{float(event.get('pnl_abs', 0.0)):.2f} USDT / {float(event.get('pnl_pct', 0.0)):.2f}%",
                f"路径统计：最大浮盈 {float(event.get('mfe_pct', 0.0)):.2f}% / 最大浮亏 {float(event.get('mae_pct', 0.0)):.2f}%",
            ]
        )
        reminders = config.get("reminders", {}).get("exit", [])
    lines.append("执行提醒：")
    for item in reminders:
        lines.append(f"- {item}")
    for item in config.get("reminders", {}).get("general", []):
        lines.append(f"- {item}")
    return "\n".join(lines)


def send_alert(message: str, event: dict[str, Any], config: dict[str, Any]) -> tuple[bool, str]:
    alert_cfg = config.get("alert", {})
    if alert_cfg.get("dry_run", True) or not alert_cfg.get("enabled", False):
        print(message)
        print("-" * 80)
        return True, "dry_run"
    webhook_url = str(alert_cfg.get("webhook_url", "")).strip()
    if not webhook_url:
        raise ValueError("alert.enabled=true 时必须配置 alert.webhook_url")
    payload_mode = alert_cfg.get("payload_mode", "json_text")
    if payload_mode == "raw_text":
        body = message.encode("utf-8")
    else:
        text_field = alert_cfg.get("text_field", "text")
        body = json.dumps({text_field: message, "event": event}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        method=alert_cfg.get("method", "POST"),
        headers=alert_cfg.get("headers", {"Content-Type": "application/json"}),
    )
    try:
        with urllib.request.urlopen(request, timeout=float(alert_cfg.get("timeout_seconds", 10))) as response:
            return 200 <= int(response.status) < 300, f"http_{response.status}"
    except urllib.error.URLError as exc:
        return False, str(exc)


def append_log(log_path: Path, row: dict[str, Any]) -> None:
    exists = log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def select_pending_events(events: list[dict[str, Any]], config: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    ignored_exit_ids = set(config.get("ignore_exit_ids", []))
    sent_keys = set(state.get("sent_keys", []))
    filtered: list[dict[str, Any]] = []
    for event in events:
        if event["event_type"] == "exit" and event.get("exit_id") in ignored_exit_ids:
            continue
        event["symbol"] = config.get("symbol", "BTCUSDT")
        key = event_key(event)
        event["event_key"] = key
        if key not in sent_keys:
            filtered.append(event)
    if config.get("send_start_mode") == "skip_history_on_first_run" and not state.get("initialized", False):
        return []
    if config.get("send_start_mode", "new_only") == "latest_only" and filtered:
        return [filtered[-1]]
    return filtered


def run_once(config_path: Path, reset_state: bool = False) -> int:
    config = load_json(config_path)
    version = config.get("version", "v17")
    state_path = resolve_path(config.get("state_file", "state_v17_alert_v1.json"))
    log_path = resolve_path(config.get("log_file", "alerts_sent_v17_alert_v1.csv"))
    state = {"sent_keys": [], "last_run_at": "", "last_seen_event_time": "", "initialized": False} if reset_state else load_state(state_path)
    data = load_market_data(config)
    features = get_version_features(version)
    indicator_data = compute_indicators(data, {"channel_mode": features.channel_mode})
    events, _, _ = capture_v17_events(indicator_data.copy(), version)
    pending = select_pending_events(events, config, state)
    sent_keys = list(state.get("sent_keys", []))
    success_count = 0
    for event in pending:
        message = format_event_message(event, config)
        ok, status = send_alert(message, event, config)
        append_log(
            log_path,
            {
                "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ok": ok,
                "status": status,
                "event_key": event["event_key"],
                "event_type": event["event_type"],
                "event_time": event["time"],
                "entry_id": event.get("entry_id", ""),
                "side": event.get("side", ""),
                "price": f"{float(event.get('price', 0.0)):.2f}",
            },
        )
        if ok:
            sent_keys.append(event["event_key"])
            success_count += 1
    if config.get("send_start_mode") == "skip_history_on_first_run" and not state.get("initialized", False):
        for event in events:
            if event["event_type"] == "exit" and event.get("exit_id") in set(config.get("ignore_exit_ids", [])):
                continue
            event["symbol"] = config.get("symbol", "BTCUSDT")
            sent_keys.append(event_key(event))
    state["sent_keys"] = sent_keys[-5000:]
    state["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["last_seen_event_time"] = events[-1]["time"] if events else ""
    state["last_data_time"] = data["open_time"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S") if not data.empty else ""
    state["initialized"] = True
    save_json(state_path, state)
    print(f"本次扫描事件数={len(events)}，待发送={len(pending)}，成功={success_count}，最新K线={state['last_data_time']}")
    return success_count


def main() -> None:
    parser = argparse.ArgumentParser(description="ASR BTC v17 中文策略动作监控预警程序")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_FILE), help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只运行一次")
    parser.add_argument("--loop", action="store_true", help="持续轮询运行")
    parser.add_argument("--reset-state", action="store_true", help="重置已发送状态后运行")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    if args.loop:
        while True:
            config = load_json(config_path)
            run_once(config_path, reset_state=args.reset_state)
            args.reset_state = False
            time.sleep(float(config.get("poll_seconds", 60)))
    else:
        run_once(config_path, reset_state=args.reset_state)


if __name__ == "__main__":
    main()
