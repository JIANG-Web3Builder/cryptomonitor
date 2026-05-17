# -*- coding: utf-8 -*-

import hashlib
import html
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

import requests

from .config import PROXY, SIGNAL, STATE_DIR, TELEGRAM
from .models import Opportunity, PushState

if TYPE_CHECKING:
    from .pump_detector import PumpSignal
    from .signal_engine import SignalAssessment


def redact_secret(value: str) -> str:
    if not value or not TELEGRAM.bot_token:
        return value
    return value.replace(TELEGRAM.bot_token, "<TELEGRAM_BOT_TOKEN>")


MIN_REPUSH_INTERVAL_S = 120         # 同一币种两次推送最小间隔
PUMP_CONFIDENCE_DELTA_RETHRESH = 12

ALERT_LEVEL_RANK = {"SUPPRESS": 0, "WATCH": 1, "NORMAL": 2, "HIGH": 3, "URGENT": 4}
GRADE_RANK = {"D": 0, "C": 1, "B": 2, "A": 3, "S": 4}


def _today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _bucket(value: float, size: float, minimum: int = 0) -> int:
    if size <= 0:
        return int(value)
    return max(minimum, int(value // size))


class TelegramNotifier:
    MAX_PUSHES_PER_CYCLE = 8

    def __init__(self):
        self.state_file = STATE_DIR / "push_state.json"
        self.chat_state_file = STATE_DIR / "telegram_chat.json"
        self.pump_state_file = STATE_DIR / "pump_push_state.json"
        self.states: Dict[str, PushState] = self._load_states()
        self.pump_states: Dict[str, dict] = self._load_pump_states()
        self.chat_id = TELEGRAM.chat_id or self._load_chat_id()
        self._last_send_at = 0.0
        self._send_count_in_window = 0
        self._rate_window_start = 0.0
        self._seen_in_session: set = set()
        self._signal_freq: Dict[str, int] = {}

    # ── 持久化 ──
    def _load_states(self) -> Dict[str, PushState]:
        if not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return {key: PushState(**value) for key, value in data.items()}
        except Exception:
            return {}

    def _save_states(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {key: value.__dict__ for key, value in self.states.items()}
        self.state_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_pump_states(self) -> Dict[str, dict]:
        if not self.pump_state_file.exists():
            return {}
        try:
            return json.loads(self.pump_state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_pump_states(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.pump_state_file.write_text(json.dumps(self.pump_states, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_chat_id(self) -> str:
        if not self.chat_state_file.exists():
            return ""
        try:
            data = json.loads(self.chat_state_file.read_text(encoding="utf-8"))
            return str(data.get("chat_id") or "")
        except Exception:
            return ""

    def _save_chat_id(self, chat_id: str):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.chat_state_file.write_text(json.dumps({"chat_id": chat_id}, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── rate limiting ──
    def _check_rate_limit(self) -> bool:
        now = time.time()
        if now - self._rate_window_start > 1.0:
            self._rate_window_start = now
            self._send_count_in_window = 0
        if self._send_count_in_window >= 3:
            return False
        return True

    # ── chat_id ──
    def _resolve_chat_id(self) -> str:
        if self.chat_id:
            return self.chat_id
        if not TELEGRAM.auto_resolve_chat_id or not TELEGRAM.bot_token:
            return ""
        url = f"https://api.telegram.org/bot{TELEGRAM.bot_token}/getUpdates"
        try:
            response = requests.get(url, timeout=TELEGRAM.timeout_seconds, proxies=PROXY.requests_proxies)
            if response.status_code != 200:
                print(f"Telegram 获取 chat_id 失败: HTTP {response.status_code}")
                return ""
            payload = response.json()
            updates = payload.get("result") or []
            if not updates:
                print("Telegram 暂无聊天记录，请先给机器人发送 /start 或任意消息。")
                return ""
            for update in reversed(updates):
                message = update.get("message") or update.get("channel_post") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                if chat_id is not None:
                    self.chat_id = str(chat_id)
                    self._save_chat_id(self.chat_id)
                    return self.chat_id
        except Exception as exc:
            print(f"Telegram 自动获取 chat_id 失败: {redact_secret(str(exc))}")
        return ""

    def check_bot(self) -> bool:
        if not TELEGRAM.enabled:
            print("Telegram 已关闭。")
            return False
        if not TELEGRAM.bot_token:
            print("Telegram Bot Token 未配置。")
            return False
        url = f"https://api.telegram.org/bot{TELEGRAM.bot_token}/getMe"
        try:
            response = requests.get(url, timeout=TELEGRAM.timeout_seconds, proxies=PROXY.requests_proxies)
            if response.status_code != 200:
                print(f"Telegram Bot 连通性检查失败: HTTP {response.status_code}")
                return False
            payload = response.json()
            if not payload.get("ok"):
                print(f"Telegram Bot 连通性检查失败")
                return False
            result = payload.get("result") or {}
            username = result.get("username") or "unknown"
            print(f"Telegram Bot 连通性正常: @{username}")
            return True
        except Exception as exc:
            print(f"Telegram Bot 连通性检查异常: {redact_secret(str(exc))}")
            return False

    def wait_for_chat_id(self, timeout_seconds: int = 60, poll_seconds: int = 3) -> str:
        deadline = time.time() + timeout_seconds
        while time.time() <= deadline:
            chat_id = self._resolve_chat_id()
            if chat_id:
                print("Telegram chat_id 已识别并缓存。")
                return chat_id
            remaining = max(0, int(deadline - time.time()))
            print(f"等待 Telegram 消息中，请给机器人发送 /start 或任意消息，剩余 {remaining}s。")
            time.sleep(poll_seconds)
        print("等待 Telegram chat_id 超时。")
        return ""

    # ── 签名 ──
    def _build_opportunity_signature(self, opportunity: Opportunity) -> str:
        i5 = opportunity.indicators.get("5m")
        i15 = opportunity.indicators.get("15m")
        volume_spike = 1.0
        for tf in ("15m", "5m"):
            ind = opportunity.indicators.get(tf)
            if ind:
                volume_spike = ind.volume_spike
                break
        payload = {
            "symbol": opportunity.symbol,
            "grade": opportunity.grade,
            "score_bucket": _bucket(opportunity.score, 5),
            "change_24h_bucket": _bucket(opportunity.change_24h_pct, 5),
            "change_15m_bucket": _bucket(i15.change_pct if i15 else 0.0, 3),
            "change_5m_bucket": _bucket(i5.change_pct if i5 else 0.0, 2),
            "funding_bucket": None if opportunity.funding_rate is None else _bucket(opportunity.funding_rate * 10000, 5),
            "oi_bucket": None if opportunity.open_interest_change_pct is None else _bucket(opportunity.open_interest_change_pct, 25),
            "volume_spike_bucket": _bucket(volume_spike, 1),
            "ma_bucket": _bucket(i15.ma_alignment_score if i15 else (i5.ma_alignment_score if i5 else 0.0), 10),
            "reason_keys": opportunity.reasons[:3],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _normalize_state(self, state: PushState) -> PushState:
        today = _today_key()
        if state.daily_push_date != today:
            state.daily_push_date = today
            state.daily_push_count = 0
        return state

    def _is_stronger_signal(self, opportunity: Opportunity, assessment: Optional["SignalAssessment"], state: PushState) -> bool:
        if not state.last_signature:
            return True

        score_delta = opportunity.score - state.last_score
        current_grade_rank = GRADE_RANK.get(opportunity.grade, 0)
        last_grade_rank = GRADE_RANK.get(state.last_grade, 0)
        current_alert_rank = ALERT_LEVEL_RANK.get(assessment.alert_level if assessment else "NORMAL", 0)
        last_alert_rank = ALERT_LEVEL_RANK.get(state.last_alert_level, 0)
        current_spike_tier = getattr(assessment, "spike_tier", 0) if assessment else 0

        return any([
            score_delta >= SIGNAL.repush_min_score_delta,
            current_grade_rank > last_grade_rank,
            current_alert_rank > last_alert_rank,
            current_spike_tier > state.last_spike_tier,
        ])

    def get_today_pushed_summary(self) -> List[Dict[str, object]]:
        today = _today_key()
        rows = []
        for symbol, raw_state in self.states.items():
            state = self._normalize_state(raw_state)
            if state.daily_push_date != today or state.daily_push_count <= 0:
                continue
            rows.append({
                "symbol": symbol,
                "count": state.daily_push_count,
                "grade": state.last_grade,
                "alert_level": state.last_alert_level,
                "score": round(state.last_score, 2),
                "last_push_at": state.last_push_at,
            })
        rows.sort(key=lambda item: (-int(item["count"]), -float(item["last_push_at"])))
        return rows


    def should_push(self, opportunity: Opportunity, assessment: Optional["SignalAssessment"] = None) -> bool:
        state = self._normalize_state(self.states.get(opportunity.symbol, PushState()))
        signature = self._build_opportunity_signature(opportunity)
        if state.last_signature == signature:
            return False
        if state.daily_push_count >= SIGNAL.max_symbol_pushes_per_day:
            return False
        if not self._is_stronger_signal(opportunity, assessment, state):
            return False
        now = time.time()
        if state.last_push_at and (now - state.last_push_at) < MIN_REPUSH_INTERVAL_S:
            return False
        return True

    def should_push_pump(self, symbol: str, confidence: float) -> bool:
        state = self.pump_states.get(symbol, {})
        now = time.time()
        last_time = state.get("last_push_at", 0)
        last_conf = state.get("last_confidence", 0)
        if not last_time:
            return True
        if (now - last_time) < MIN_REPUSH_INTERVAL_S:
            if confidence - last_conf < PUMP_CONFIDENCE_DELTA_RETHRESH:
                return False
        return True

    def mark_pushed(self, opportunity: Opportunity, assessment: Optional["SignalAssessment"] = None, signature: Optional[str] = None):
        state = self._normalize_state(self.states.get(opportunity.symbol, PushState()))
        state.last_push_at = time.time()
        state.last_score = opportunity.score
        state.last_grade = opportunity.grade
        state.last_alert_level = assessment.alert_level if assessment else state.last_alert_level
        state.last_spike_tier = getattr(assessment, "spike_tier", 0) if assessment else state.last_spike_tier
        state.repeat_count += 1
        state.daily_push_count += 1
        state.last_signature = signature or self._build_opportunity_signature(opportunity)
        self.states[opportunity.symbol] = state
        self._save_states()

    def mark_pump_pushed(self, symbol: str, confidence: float):
        self.pump_states[symbol] = {
            "last_push_at": time.time(),
            "last_confidence": confidence,
        }
        self._save_pump_states()

    # ── 全量扫描推送 ──
    def send_opportunities(self, opportunities: Iterable[Opportunity], assessments: Optional[Dict[str, "SignalAssessment"]] = None) -> Dict[str, int]:
        stats = {"new": 0, "updated": 0, "unchanged": 0, "sent": 0, "skipped": 0, "sent_symbols": []}
        sent_in_cycle = 0

        sorted_opps = sorted(
            opportunities,
            key=lambda o: (
                1 if assessments and getattr(assessments.get(o.symbol), "is_spike_alert", False) else 0,
                o.score,
            ),
            reverse=True,
        )
        for item in sorted_opps:
            state = self._normalize_state(self.states.get(item.symbol, PushState()))
            self.states[item.symbol] = state
            assessment = assessments.get(item.symbol) if assessments else None
            if not self.should_push(item, assessment=assessment):
                stats["unchanged"] += 1
                continue

            self._signal_freq[item.symbol] = self._signal_freq.get(item.symbol, 0) + 1
            freq = self._signal_freq[item.symbol]

            is_new = item.symbol not in self._seen_in_session
            if is_new:
                self._seen_in_session.add(item.symbol)

            signature = self._build_opportunity_signature(item)
            is_first_push = not state.last_signature

            if sent_in_cycle >= self.MAX_PUSHES_PER_CYCLE:
                if not (assessment and getattr(assessment, "is_spike_alert", False)):
                    stats["skipped"] += 1
                    continue

            if assessment and getattr(assessment, "is_spike_alert", False):
                status_label = "⚡异动预警" if is_first_push else "⚡异动更新"
            elif is_new:
                status_label = "🆕新信号"
            elif is_first_push:
                status_label = "新信号"
            else:
                status_label = f"更新({freq}次)"

            message = self.format_opportunity(item, status_label=status_label, assessment=assessment)
            if self.send_message(message):
                if is_first_push or is_new:
                    stats["new"] += 1
                else:
                    stats["updated"] += 1
                stats["sent"] += 1
                stats["sent_symbols"].append(item.symbol)
                sent_in_cycle += 1
                self.mark_pushed(item, assessment=assessment, signature=signature)
                time.sleep(0.35)

        return stats

    def format_opportunity(self, item: Opportunity, status_label: str = "预警信号", assessment: Optional["SignalAssessment"] = None) -> str:
        tag_str = ""
        if assessment and assessment.tags:
            visible_tags = [t for t in assessment.tags if not t.startswith("SPIKE_")][:5]
            if visible_tags:
                tag_str = " ".join(f"#{t}" for t in visible_tags)

        oi_change_str = "N/A"
        if item.open_interest_change_pct is not None:
            oi = item.open_interest_change_pct
            if oi >= 200:
                oi_change_str = f"<b>+{oi:.0f}%</b> (3X+)"
            elif oi >= 100:
                oi_change_str = f"<b>+{oi:.0f}%</b> (2X+)"
            else:
                oi_change_str = f"+{oi:.1f}%"

        funding_str = "N/A"
        if item.funding_rate is not None:
            fr = item.funding_rate * 100
            if item.funding_rate <= -SIGNAL.push_funding_rate_abs_threshold:
                funding_str = f"<b>{fr:.3f}%</b> (逼空)"
            elif item.funding_rate >= SIGNAL.push_funding_rate_abs_threshold:
                funding_str = f"{fr:.3f}% (偏多)"
            else:
                funding_str = f"{fr:.3f}%"

        grade_emoji = {"S": "🏆", "A": "🔥", "B": "📈", "C": "📊"}.get(item.grade, "📊")

        alert_emoji = {"URGENT": "🚨", "HIGH": "🔔", "NORMAL": "📢", "WATCH": "👀"}.get(
            assessment.alert_level if assessment else "NORMAL", ""
        )

        freq = self._signal_freq.get(item.symbol, 0)

        # 均线状态
        i5 = item.indicators.get("5m")
        i15 = item.indicators.get("15m")
        ma_info = ""
        if i15:
            ma_info = f"MA对齐: {i15.ma_alignment_score:.0f}分" + (f" | 连阳: {i15.consecutive_bull}根" if i15.consecutive_bull >= 3 else "")

        msg = (
            f"{alert_emoji} {grade_emoji} <b>{html.escape(item.symbol)}</b> {status_label} | {item.grade}级 {item.score:.0f}分\n"
            f"\n"
            f"💰 现价: <b>{item.current_price:.8g}</b> | 24h: {item.change_24h_pct:+.1f}%\n"
            f"📊 成交额: 24h {item.quote_volume/1_000_000:.1f}M | 4h {item.quote_volume_4h/1_000_000:.1f}M | OI: {oi_change_str}\n"
            f"📐 {ma_info}\n"
            f"💱 费率: {funding_str} | 支撑: {item.context.support:.6g} | 压力: {item.context.resistance:.6g}\n"
            f"📏 ATR: {item.context.atr_pct:.1f}% | EMA偏离: {item.context.ema_extension_pct:+.1f}%"
        )
        if assessment and assessment.trigger_reason:
            msg += f"\n🎯 {html.escape(assessment.trigger_reason)}"
        if tag_str:
            msg += f"\n{tag_str}"
        if freq > 0:
            msg += f"\n🔄 第{freq}次信号"
        return msg

    # ── 拉盘快速预警推送 ──
    def send_pump_alerts(self, pump_signals: List["PumpSignal"]) -> int:
        if not pump_signals:
            return 0

        priority_order = {"URGENT": 0, "HIGH": 1, "NORMAL": 2}
        pump_signals.sort(key=lambda p: (priority_order.get(p.alert_priority, 2), -p.confidence))

        sent = 0
        for ps in pump_signals:
            if not self.should_push_pump(ps.symbol, ps.confidence):
                continue
            message = self.format_pump_alert(ps)
            if self.send_message(message):
                self.mark_pump_pushed(ps.symbol, ps.confidence)
                sent += 1
                time.sleep(0.35)
        return sent

    def format_pump_alert(self, ps: "PumpSignal") -> str:
        pattern_emoji = {
            "MOMENTUM_SURGE": "📈", "OI_EXPLOSION": "💥",
            "BREAKOUT_SPIKE": "🚀", "STRONG_RUN": "🔥",
            "SHORT_SQUEEZE": "🧨",
        }.get(ps.pattern, "⚡")
        pattern_cn = {
            "MOMENTUM_SURGE": "持续动量推动", "OI_EXPLOSION": "OI爆发驱动",
            "BREAKOUT_SPIKE": "突破量价齐升", "STRONG_RUN": "强势连阳",
            "SHORT_SQUEEZE": "逼空行情",
        }.get(ps.pattern, ps.pattern)
        priority_mark = {"URGENT": "🚨", "HIGH": "🔔", "NORMAL": "📢"}.get(ps.alert_priority, "")

        oi_desc = ""
        if ps.oi_level >= 3:
            oi_desc = f" | OI: <b>3X+</b>"
        elif ps.oi_level >= 2:
            oi_desc = f" | OI: <b>2X</b>"
        elif ps.oi_level >= 1:
            oi_desc = f" | OI: +{ps.oi_change_pct:.0f}%"

        price_lvl = {3: "15%+🚨", 2: "10%+", 1: "5%+", 0: ""}.get(ps.price_level, "")

        tags_str = " ".join(f"#{t}" for t in ps.tags[:5]) if ps.tags else ""

        msg = (
            f"{priority_mark} {pattern_emoji} <b>{html.escape(ps.symbol)}</b> {pattern_cn}\n"
            f"\n"
            f"形态: <b>{pattern_cn}</b> | 信心: {ps.confidence:.0f}分{oi_desc}\n"
            f"5m: {ps.change_5m:+.1f}% | 15m: {ps.change_15m:+.1f}% {price_lvl}\n"
            f"量能: 5m {ps.vol_spike_5m:.1f}x | 15m {ps.vol_spike_15m:.1f}x\n"
            f"MA: 5m={ps.ma_score_5m:.0f} 15m={ps.ma_score_15m:.0f} | RSI(5m): {ps.rsi_5m:.0f} | 连阳: {ps.consecutive_bull_5m}根"
        )
        if ps.reasons:
            msg += f"\n📌 {html.escape(' | '.join(ps.reasons[:4]))}"
        if tags_str:
            msg += f"\n{tags_str}"
        return msg

    # ── 发送底层 ──
    def send_message(self, message: str) -> bool:
        if not TELEGRAM.enabled:
            print(message)
            return True
        chat_id = self._resolve_chat_id()
        if not TELEGRAM.bot_token or not chat_id:
            print("Telegram 未配置 Token/Chat ID；消息仅输出到控制台。")
            print(message)
            return False

        while not self._check_rate_limit():
            time.sleep(0.35)

        url = f"https://api.telegram.org/bot{TELEGRAM.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": TELEGRAM.parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=TELEGRAM.timeout_seconds,
                proxies=PROXY.requests_proxies,
            )
            if response.status_code == 200:
                self._send_count_in_window += 1
                return True
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "5"))
                print(f"Telegram 429 限频，等待 {retry_after}s ...")
                time.sleep(retry_after)
                return self.send_message(message)
            print(f"Telegram 推送失败: HTTP {response.status_code}")
        except Exception as exc:
            print(f"Telegram 推送异常: {redact_secret(str(exc))}")
        return False
