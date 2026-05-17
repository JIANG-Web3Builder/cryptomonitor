# -*- coding: utf-8 -*-
"""
市场摘要推送 —— 每15分钟发送，含市场结构、Top机会、拉盘汇总、背离警告。

v3.0: 新增市场结构分析 + 背离检测汇总 + Discord webhook
"""

import html
import time
from datetime import datetime
from typing import Dict, List, Optional

from .config import SIGNAL
from .models import Opportunity
from .notifier import TelegramNotifier


class DigestNotifier:
    def __init__(self, notifier: TelegramNotifier, webhook=None):
        self.notifier = notifier
        self.webhook = webhook
        self._last_digest_at = 0.0
        self._last_pushed_list_digest_at = 0.0
        self._pump_alerts_since_last: Dict[str, str] = {}
        self._anomalies_since_last: List = []

    def record_pump_alert(self, symbol: str, pattern: str = ""):
        if symbol not in self._pump_alerts_since_last:
            self._pump_alerts_since_last[symbol] = pattern

    def record_anomaly(self, anomaly):
        """记录异常信号，摘要时展示。"""
        if len(self._anomalies_since_last) < 20:
            self._anomalies_since_last.append(anomaly)

    def should_send_digest(self) -> bool:
        now = time.time()
        if now - self._last_digest_at >= 900:
            self._last_digest_at = now
            return True
        return False

    def should_send_pushed_list_digest(self) -> bool:
        now = time.time()
        if now - self._last_pushed_list_digest_at >= SIGNAL.pushed_list_digest_interval_seconds:
            self._last_pushed_list_digest_at = now
            return True
        return False

    def send_pushed_list_digest(self) -> int:
        if not SIGNAL.digest_enabled:
            return 0
        if not self.should_send_pushed_list_digest():
            return 0

        pushed_rows = self.notifier.get_today_pushed_summary()
        if not pushed_rows:
            return 0

        message = self._format_pushed_list_digest(pushed_rows)
        sent = self.notifier.send_message(message)

        if self.webhook:
            try:
                self.webhook.send_simple(
                    content=message.replace("<b>", "**").replace("</b>", "**"),
                    title="今日已推送列表",
                )
            except Exception:
                pass

        return 1 if sent else 0

    def send_digest(
        self,
        opportunities: List[Opportunity],
        market_cold: bool = False,
        push_stats: Dict[str, int] = None,
        market_structure=None,
        divergence_warnings: Dict[str, List] = None,
    ) -> int:
        if not SIGNAL.digest_enabled:
            return 0
        if not self.should_send_digest():
            return 0
        if not opportunities:
            return 0

        message = self._format_digest(
            opportunities, market_cold, push_stats or {},
            market_structure, divergence_warnings,
        )

        # Telegram
        sent = self.notifier.send_message(message)

        # Discord webhook
        if self.webhook:
            try:
                self.webhook.send_simple(
                    content=message.replace("<b>", "**").replace("</b>", "**")
                             .replace("🏆", "").replace("🧊", "").replace("🔥", "")
                             .replace("⚡", "").replace("💥", "").replace("🚀", "")
                             .replace("📈", "").replace("📊", ""),
                    title="市场摘要 Digest",
                )
            except Exception:
                pass

        self._pump_alerts_since_last = {}
        self._anomalies_since_last = []
        return 1 if sent else 0

    def _format_digest(
        self,
        opportunities: List[Opportunity],
        market_cold: bool,
        push_stats: Dict[str, int],
        market_structure=None,
        divergence_warnings: Dict[str, List] = None,
    ) -> str:
        now = datetime.now().strftime("%H:%M")
        heat = "🧊 偏冷" if market_cold else "🔥 活跃"

        s_count = sum(1 for o in opportunities if o.grade == "S")
        a_count = sum(1 for o in opportunities if o.grade == "A")
        b_count = sum(1 for o in opportunities if o.grade == "B")

        lines = [
            f"📊 <b>市场摘要</b> {now} · {heat}",
        ]

        # ── 市场结构 ──
        if market_structure:
            lines.append(f"📐 市场: {html.escape(market_structure.summary)}")
            if market_structure.hot_sectors:
                lines.append(f"🔥 热点板块: {', '.join(html.escape(s) for s in market_structure.hot_sectors[:3])}")

        lines.append("")
        lines.append(
            f"本轮: S级{s_count} A级{a_count} B级{b_count} | "
            f"推送: 新{push_stats.get('new', 0)} 更新{push_stats.get('updated', 0)}"
        )

        # ── Top 机会 ──
        top_n = min(SIGNAL.digest_top_n, len(opportunities))
        if top_n > 0:
            lines.append("")
            lines.append("<b>🏆 Top 机会</b>")
            for i, o in enumerate(opportunities[:top_n], 1):
                oi = ""
                if o.open_interest_change_pct is not None:
                    oi_val = o.open_interest_change_pct
                    if oi_val >= 200:
                        oi = f" OI:3X+"
                    elif oi_val >= 100:
                        oi = f" OI:2X+"
                    elif oi_val >= 50:
                        oi = f" OI:+{oi_val:.0f}%"
                    else:
                        oi = f" OI:{oi_val:+.1f}%"

                # MA 对齐图标
                i15 = o.indicators.get("15m")
                ma_icon = " ◈" if (i15 and i15.ma_alignment_score >= 70) else ""

                lines.append(
                    f"  {i}. {html.escape(o.symbol):<14} {o.grade}级 {o.score:>5.0f}分  "
                    f"24h:{o.change_24h_pct:+.1f}%{oi}{ma_icon}"
                )

        # ── 拉盘预警汇总 ──
        if self._pump_alerts_since_last:
            pattern_short = {
                "MOMENTUM_SURGE": "动量",
                "OI_EXPLOSION": "OI爆发",
                "BREAKOUT_SPIKE": "突破",
                "STRONG_RUN": "连阳",
                "SHORT_SQUEEZE": "逼空",
            }
            pump_items = []
            for symbol, pat in list(self._pump_alerts_since_last.items())[:10]:
                tag = pattern_short.get(pat, "")
                pump_items.append(f"{html.escape(symbol)}{'['+tag+']' if tag else ''}")
            pump_list = "、".join(pump_items)
            lines.append("")
            lines.append(f"⚡ 近15min拉盘预警: {pump_list}")

        # ── 背离警告 ──
        if divergence_warnings:
            div_lines = []
            for symbol, divs in list(divergence_warnings.items())[:5]:
                warn_divs = [d for d in divs if d.is_warning]
                if warn_divs:
                    best = warn_divs[0]
                    div_lines.append(f"  {html.escape(symbol)}: {html.escape(best.description[:60])}")
            if div_lines:
                lines.append("")
                lines.append("<b>⚠️ 背离警告</b>")
                lines.extend(div_lines[:3])

        # ── 市场异常 ──
        if self._anomalies_since_last:
            anomaly_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}
            anom_lines = []
            seen = set()
            for a in self._anomalies_since_last:
                if a.symbol not in seen:
                    seen.add(a.symbol)
                    icon = anomaly_icon.get(a.severity, "⚪")
                    anom_lines.append(f"  {icon} {html.escape(a.symbol)}: {html.escape(a.description[:70])}")
            if anom_lines:
                lines.append("")
                lines.append("<b>🔍 市场异常</b>")
                lines.extend(anom_lines[:5])

        return "\n".join(lines)

    def _format_pushed_list_digest(self, pushed_rows: List[Dict[str, object]]) -> str:
        now = datetime.now().strftime("%H:%M")
        total_pushes = sum(int(row.get("count", 0)) for row in pushed_rows)
        lines = [
            f"🧾 <b>今日已推送列表</b> {now}",
            f"已推送币种: {len(pushed_rows)} | 总推送次数: {total_pushes}",
            "",
        ]

        for index, row in enumerate(pushed_rows[:15], 1):
            lines.append(
                f"  {index}. {html.escape(str(row.get('symbol', ''))):<14} "
                f"{html.escape(str(row.get('grade', '')))}级 "
                f"{html.escape(str(row.get('alert_level', '')))} "
                f"score={float(row.get('score', 0.0)):.0f} "
                f"count={int(row.get('count', 0))}"
            )

        if len(pushed_rows) > 15:
            lines.append("")
            lines.append(f"... 其余 {len(pushed_rows) - 15} 个币种未展开")

        return "\n".join(lines)
