# -*- coding: utf-8 -*-
"""
Webhook 通知器 —— Discord + 通用 HTTP Webhook 支持。

Discord 采用 Embed 格式化，按预警等级变色:
  - URGENT: 红色 0xFF0000
  - HIGH: 橙色 0xFF8C00
  - NORMAL: 蓝色 0x3498DB
  - WATCH: 灰色 0x95A5A6

支持多个 webhook URL，可同时推送 Telegram + Discord + 自定义服务。
"""

import html
import json
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

import requests

from .config import PROXY, env_bool

if TYPE_CHECKING:
    from .pump_detector import PumpSignal
    from .models import Opportunity
    from .signal_engine import SignalAssessment


# ── 配置 ──────────────────────────────────────────────────

DISCORD_ENABLED = env_bool("DISCORD_ENABLED", False)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_AVATAR_URL = os.getenv("DISCORD_AVATAR_URL", "")
DISCORD_USERNAME = os.getenv("DISCORD_USERNAME", "Crypto Alert Monitor")

GENERIC_WEBHOOK_ENABLED = env_bool("GENERIC_WEBHOOK_ENABLED", False)
GENERIC_WEBHOOK_URL = os.getenv("GENERIC_WEBHOOK_URL", "")

ALERT_COLORS = {
    "URGENT": 0xFF0000,
    "HIGH": 0xFF8C00,
    "NORMAL": 0x3498DB,
    "WATCH": 0x95A5A6,
    "SUPPRESS": 0x808080,
}

PATTERN_COLORS = {
    "SHORT_SQUEEZE": 0xFF4500,
    "OI_EXPLOSION": 0xFF1493,
    "BREAKOUT_SPIKE": 0x00CED1,
    "MOMENTUM_SURGE": 0x32CD32,
    "STRONG_RUN": 0x1E90FF,
}


def _post_webhook(url: str, payload: dict, timeout: int = 10) -> bool:
    try:
        resp = requests.post(url, json=payload, timeout=timeout, proxies=PROXY.requests_proxies)
        return resp.status_code in (200, 204)
    except Exception:
        return False


# ── Discord Embed ─────────────────────────────────────────

def _discord_full_alert(
    item: "Opportunity",
    assessment: Optional["SignalAssessment"] = None,
) -> dict:
    color = ALERT_COLORS.get(assessment.alert_level if assessment else "NORMAL", 0x3498DB)
    level_emoji = {"URGENT": "🚨", "HIGH": "🔔", "NORMAL": "📢", "WATCH": "👀"}.get(
        assessment.alert_level if assessment else "NORMAL", ""
    )

    oi_str = "N/A"
    if item.open_interest_change_pct is not None:
        oi = item.open_interest_change_pct
        if oi >= 200:
            oi_str = f"+{oi:.0f}% (3X+)"
        elif oi >= 100:
            oi_str = f"+{oi:.0f}% (2X+)"
        else:
            oi_str = f"+{oi:.1f}%"

    funding_str = "N/A"
    if item.funding_rate is not None:
        funding_str = f"{item.funding_rate*100:.3f}%"

    i5 = item.indicators.get("5m")
    i15 = item.indicators.get("15m")

    fields = [
        {"name": "💰 价格", "value": f"**{item.current_price:.8g}** | 24h: {item.change_24h_pct:+.1f}%", "inline": True},
        {"name": "📊 成交额", "value": f"24h {item.quote_volume/1_000_000:.1f}M | 4h {item.quote_volume_4h/1_000_000:.1f}M", "inline": True},
        {"name": "📈 OI变化", "value": oi_str, "inline": True},
    ]

    if i15:
        fields.append({"name": "📐 MA对齐(15m)", "value": f"{i15.ma_alignment_score:.0f}分", "inline": True})
        fields.append({"name": "🔥 连阳(15m)", "value": f"{i15.consecutive_bull}根", "inline": True})
        fields.append({"name": "📏 ATR", "value": f"{item.context.atr_pct:.1f}%", "inline": True})

    fields.append({"name": "💱 费率", "value": funding_str, "inline": True})
    fields.append({"name": "🛡️ 支撑", "value": f"{item.context.support:.6g}", "inline": True})
    fields.append({"name": "⚔️ 压力", "value": f"{item.context.resistance:.6g}", "inline": True})

    if assessment and assessment.tags:
        tags_visible = [t for t in assessment.tags if not t.startswith("SPIKE_")][:5]
        if tags_visible:
            fields.append({"name": "🏷️ 标签", "value": " ".join(f"`{t}`" for t in tags_visible), "inline": False})

    embed = {
        "title": f"{level_emoji} {item.symbol} | {item.grade}级 {item.score:.0f}分",
        "description": item.reasons[0] if item.reasons else "",
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Crypto Alert Monitor v3.0 | {item.grade}级预警"},
    }
    return embed


def _discord_pump_alert(ps: "PumpSignal") -> dict:
    color = PATTERN_COLORS.get(ps.pattern, 0xFFA500)
    pattern_cn = {
        "MOMENTUM_SURGE": "持续动量推动", "OI_EXPLOSION": "OI爆发驱动",
        "BREAKOUT_SPIKE": "突破量价齐升", "STRONG_RUN": "强势连阳",
        "SHORT_SQUEEZE": "逼空行情",
    }.get(ps.pattern, ps.pattern)
    level_emoji = {"URGENT": "🚨", "HIGH": "🔔", "NORMAL": "📢"}.get(ps.alert_priority, "")

    oi_str = "N/A"
    if ps.oi_change_pct is not None:
        oi_str = f"+{ps.oi_change_pct:.0f}%"
        if ps.oi_level >= 3:
            oi_str += " (3X+)"
        elif ps.oi_level >= 2:
            oi_str += " (2X+)"

    fields = [
        {"name": "🎯 形态", "value": f"**{pattern_cn}** | 信心 {ps.confidence:.0f}分", "inline": False},
        {"name": "📈 5m 涨幅", "value": f"{ps.change_5m:+.1f}%", "inline": True},
        {"name": "📈 15m 涨幅", "value": f"{ps.change_15m:+.1f}%", "inline": True},
        {"name": "💥 OI", "value": oi_str, "inline": True},
        {"name": "📊 量能(5m)", "value": f"{ps.vol_spike_5m:.1f}x", "inline": True},
        {"name": "📊 量能(15m)", "value": f"{ps.vol_spike_15m:.1f}x", "inline": True},
        {"name": "📐 MA(5m/15m)", "value": f"{ps.ma_score_5m:.0f}/{ps.ma_score_15m:.0f}", "inline": True},
        {"name": "🔥 RSI(5m)", "value": f"{ps.rsi_5m:.0f}", "inline": True},
        {"name": "🕯️ 连阳", "value": f"{ps.consecutive_bull_5m}根", "inline": True},
    ]

    if ps.reasons:
        fields.append({"name": "📌 原因", "value": " | ".join(ps.reasons[:3]), "inline": False})

    embed = {
        "title": f"{level_emoji} {ps.symbol} {pattern_cn}",
        "description": f"快速预警 | 信心: {ps.confidence:.0f}分",
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Crypto Alert Monitor v3.0 | 快速预警"},
    }
    return embed


# ── 公共接口 ──────────────────────────────────────────────

class WebhookNotifier:
    def __init__(self):
        self.discord_enabled = DISCORD_ENABLED and bool(DISCORD_WEBHOOK_URL)
        self.generic_enabled = GENERIC_WEBHOOK_ENABLED and bool(GENERIC_WEBHOOK_URL)
        self._last_send: Dict[str, float] = {}  # symbol -> last send time
        self._min_interval = 60  # 同一币种 webhook 最小间隔

    def _can_send(self, symbol: str) -> bool:
        now = time.time()
        last = self._last_send.get(symbol, 0)
        if now - last < self._min_interval:
            return False
        self._last_send[symbol] = now
        return True

    def send_full_alert(self, item: "Opportunity", assessment: Optional["SignalAssessment"] = None) -> int:
        """发送全量扫描预警到 Discord/Webhook。返回发送成功数。"""
        if not self._can_send(item.symbol):
            return 0
        sent = 0
        if self.discord_enabled:
            embed = _discord_full_alert(item, assessment)
            payload = {
                "username": DISCORD_USERNAME,
                "embeds": [embed],
            }
            if DISCORD_AVATAR_URL:
                payload["avatar_url"] = DISCORD_AVATAR_URL
            if _post_webhook(DISCORD_WEBHOOK_URL, payload):
                sent += 1

        if self.generic_enabled:
            gen_payload = {
                "type": "full_alert",
                "symbol": item.symbol,
                "score": item.score,
                "grade": item.grade,
                "price": item.current_price,
                "alert_level": assessment.alert_level if assessment else "NORMAL",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if _post_webhook(GENERIC_WEBHOOK_URL, gen_payload):
                sent += 1

        return sent

    def send_pump_alert(self, ps: "PumpSignal") -> int:
        """发送拉盘快速预警到 Discord/Webhook。"""
        if not self._can_send(ps.symbol):
            return 0
        sent = 0
        if self.discord_enabled:
            embed = _discord_pump_alert(ps)
            payload = {
                "username": DISCORD_USERNAME,
                "embeds": [embed],
            }
            if DISCORD_AVATAR_URL:
                payload["avatar_url"] = DISCORD_AVATAR_URL
            if _post_webhook(DISCORD_WEBHOOK_URL, payload):
                sent += 1

        if self.generic_enabled:
            gen_payload = {
                "type": "pump_alert",
                "symbol": ps.symbol,
                "pattern": ps.pattern,
                "confidence": ps.confidence,
                "alert_priority": ps.alert_priority,
                "change_5m": ps.change_5m,
                "change_15m": ps.change_15m,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if _post_webhook(GENERIC_WEBHOOK_URL, gen_payload):
                sent += 1

        return sent

    def send_simple(self, content: str, title: str = "") -> bool:
        """发送简洁文本消息。"""
        if self.discord_enabled:
            embed = {
                "title": title or "Crypto Alert",
                "description": content,
                "color": 0x3498DB,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            payload = {"username": DISCORD_USERNAME, "embeds": [embed]}
            if DISCORD_AVATAR_URL:
                payload["avatar_url"] = DISCORD_AVATAR_URL
            return _post_webhook(DISCORD_WEBHOOK_URL, payload)
        return False


# ── 单例 ──────────────────────────────────────────────────
_webhook_instance: Optional[WebhookNotifier] = None


def get_webhook() -> WebhookNotifier:
    global _webhook_instance
    if _webhook_instance is None:
        _webhook_instance = WebhookNotifier()
    return _webhook_instance
