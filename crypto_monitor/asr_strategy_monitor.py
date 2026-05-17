# -*- coding: utf-8 -*-

import csv
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ASR_STRATEGY, DATA_DIR, ENABLE_CONSOLE, ENABLE_DEBUG
from .notifier import TelegramNotifier, redact_secret


class AsrBtcV17StrategyMonitor:
    def __init__(self, notifier: TelegramNotifier, client):
        self.notifier = notifier
        self.client = client
        self.root_dir = Path(__file__).resolve().parent.parent
        self.strategy_dir = self.root_dir / ASR_STRATEGY.strategy_dir
        self.runtime_dir = DATA_DIR / "asr_strategy"
        self.data_csv_path = self.runtime_dir / "BTCUSDT_15m.csv"
        self.config_path = self.runtime_dir / "config_alert_v1.json"
        self._strategy_module = None

    def run_once(self) -> int:
        if not ASR_STRATEGY.enabled:
            return 0
        try:
            self._prepare_runtime_files()
            module = self._load_strategy_module()
            return self._run_strategy_scan(module)
        except Exception as exc:
            print(f"[ASR策略] 扫描异常: {redact_secret(str(exc))}")
            if ENABLE_DEBUG:
                import traceback
                traceback.print_exc()
            return 0

    def _load_strategy_module(self):
        if self._strategy_module is not None:
            return self._strategy_module
        if not self.strategy_dir.exists():
            raise FileNotFoundError(f"策略目录不存在: {self.strategy_dir}")
        strategy_path = str(self.strategy_dir)
        if strategy_path not in sys.path:
            sys.path.insert(0, strategy_path)
        self._strategy_module = importlib.import_module("monitor_v17_alert_v1")
        return self._strategy_module

    def _prepare_runtime_files(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._refresh_market_data_csv()
        if not self.config_path.exists():
            source_config = self.strategy_dir / "config_alert_v1.json"
            config = json.loads(source_config.read_text(encoding="utf-8"))
        else:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["symbol"] = ASR_STRATEGY.symbol
        config["version"] = ASR_STRATEGY.version
        config["timeframe_minutes"] = ASR_STRATEGY.timeframe_minutes
        config["data_csv_path"] = str(self.data_csv_path)
        config["state_file"] = str(self.runtime_dir / "state_v17_alert_v1.json")
        config["log_file"] = str(self.runtime_dir / "alerts_sent_v17_alert_v1.csv")
        config["max_bars"] = ASR_STRATEGY.max_bars
        config["only_closed_latest_bar"] = True
        config["send_start_mode"] = ASR_STRATEGY.send_start_mode
        config["alert"] = {"enabled": False, "dry_run": True}
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def _refresh_market_data_csv(self) -> None:
        symbol = ASR_STRATEGY.ccxt_symbol
        limit = max(100, min(int(ASR_STRATEGY.fetch_limit), 1500))
        rows = self.client._with_retry(
            f"获取 {ASR_STRATEGY.symbol} {ASR_STRATEGY.timeframe_minutes}m 策略K线",
            lambda: self.client.exchange.fetch_ohlcv(symbol, timeframe=f"{ASR_STRATEGY.timeframe_minutes}m", limit=limit),
            [],
        )
        if not rows:
            raise RuntimeError("未获取到策略K线数据")
        with self.data_csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["open_time", "open", "high", "low", "close", "volume"])
            for row in rows:
                open_time = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).replace(tzinfo=None)
                writer.writerow([
                    open_time.strftime("%Y-%m-%d %H:%M:%S"),
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                ])

    def _run_strategy_scan(self, module) -> int:
        config = module.load_json(self.config_path)
        state_path = module.resolve_path(config.get("state_file", "state_v17_alert_v1.json"))
        log_path = module.resolve_path(config.get("log_file", "alerts_sent_v17_alert_v1.csv"))
        state = module.load_state(state_path)
        data = module.load_market_data(config)
        features = module.get_version_features(config.get("version", ASR_STRATEGY.version))
        indicator_data = module.compute_indicators(data, {"channel_mode": features.channel_mode})
        events, _, _ = module.capture_v17_events(indicator_data.copy(), config.get("version", ASR_STRATEGY.version))
        pending = module.select_pending_events(events, config, state)
        sent_keys = list(state.get("sent_keys", []))
        success_count = 0
        for event in pending:
            message = module.format_event_message(event, config)
            ok = self.notifier.send_message(message)
            module.append_log(
                log_path,
                {
                    "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ok": ok,
                    "status": "telegram" if ok else "telegram_failed",
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
                event["symbol"] = config.get("symbol", ASR_STRATEGY.symbol)
                sent_keys.append(module.event_key(event))
        state["sent_keys"] = sent_keys[-5000:]
        state["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_seen_event_time"] = events[-1]["time"] if events else ""
        state["last_data_time"] = data["open_time"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S") if not data.empty else ""
        state["initialized"] = True
        module.save_json(state_path, state)
        if ENABLE_CONSOLE:
            print(f"[ASR策略] 事件数={len(events)}，待发送={len(pending)}，成功={success_count}，最新K线={state['last_data_time']}")
        return success_count
