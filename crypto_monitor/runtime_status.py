# -*- coding: utf-8 -*-

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import STATE_DIR


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeStatusStore:
    def __init__(self, file_path: Optional[Path] = None):
        self.file_path = file_path or (STATE_DIR / "runtime_status.json")
        self._last_heartbeat_ts = 0.0
        self.data = self._load()
        self._ensure_defaults()
        self.save()

    def _component_template(self) -> Dict[str, Any]:
        return {
            "status": "idle",
            "runs": 0,
            "successes": 0,
            "failures": 0,
            "last_started_at": "",
            "last_finished_at": "",
            "last_success_at": "",
            "last_duration_seconds": 0.0,
            "last_result_count": 0,
            "last_error": "",
        }

    def _load(self) -> Dict[str, Any]:
        if not self.file_path.exists():
            return {}
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _ensure_defaults(self):
        self.data.setdefault("process_started_at", _now_iso())
        self.data.setdefault("updated_at", "")
        self.data.setdefault("last_heartbeat_at", "")
        self.data.setdefault("mode", "monitor")
        self.data.setdefault("components", {})
        components = self.data["components"]
        for name in ("full_scan", "rapid_scan", "asr_strategy"):
            if not isinstance(components.get(name), dict):
                components[name] = self._component_template()
                continue
            item = components[name]
            for key, value in self._component_template().items():
                item.setdefault(key, value)

    def save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = _now_iso()
        self.file_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_mode(self, mode: str):
        self.data["mode"] = mode
        self.save()

    def heartbeat(self, force: bool = False, min_interval_seconds: int = 15):
        now = time.time()
        if not force and self._last_heartbeat_ts and (now - self._last_heartbeat_ts) < min_interval_seconds:
            return
        self._last_heartbeat_ts = now
        self.data["last_heartbeat_at"] = _now_iso()
        self.save()

    def start_component(self, name: str):
        component = self.data["components"].setdefault(name, self._component_template())
        component["runs"] += 1
        component["status"] = "running"
        component["last_started_at"] = _now_iso()
        component["last_error"] = ""
        self.save()

    def finish_component(
        self,
        name: str,
        success: bool,
        duration_seconds: float,
        result_count: Optional[int] = None,
        error: str = "",
    ):
        component = self.data["components"].setdefault(name, self._component_template())
        component["last_finished_at"] = _now_iso()
        component["last_duration_seconds"] = round(float(duration_seconds), 3)
        if result_count is not None:
            component["last_result_count"] = int(result_count)
        if success:
            component["successes"] += 1
            component["status"] = "ok"
            component["last_success_at"] = component["last_finished_at"]
            component["last_error"] = ""
        else:
            component["failures"] += 1
            component["status"] = "error"
            component["last_error"] = str(error)[:500]
        self.save()

    def snapshot(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self.data, ensure_ascii=False))

    def format_console_summary(self) -> str:
        labels = {
            "full_scan": "全扫",
            "rapid_scan": "快扫",
            "asr_strategy": "ASR",
        }
        chunks = []
        for key in ("full_scan", "rapid_scan", "asr_strategy"):
            item = self.data["components"].get(key, {})
            last_success_at = item.get("last_success_at") or "-"
            chunks.append(
                f"{labels[key]}={item.get('status', 'idle')} ok:{item.get('successes', 0)} fail:{item.get('failures', 0)} last:{last_success_at} dur:{item.get('last_duration_seconds', 0.0):.1f}s cnt:{item.get('last_result_count', 0)}"
            )
        return " | ".join(chunks)
