# -*- coding: utf-8 -*-

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from crypto_monitor.config import ASR_STRATEGY, ENABLE_CONSOLE, ENABLE_DEBUG, RUN_ONCE, PUMP, RAPID, SCAN, SIGNAL, SNAPSHOT_DIR, STATE_DIR
    from crypto_monitor.asr_strategy_monitor import AsrBtcV17StrategyMonitor
    from crypto_monitor.exchange_client import BinanceFuturesClient
    from crypto_monitor.indicators import build_indicator_map
    from crypto_monitor.levels import build_context
    from crypto_monitor.models import Candle, Opportunity, SymbolMarket
    from crypto_monitor.notifier import TelegramNotifier
    from crypto_monitor.pump_detector import PATTERN_NONE, evaluate_pump, PumpSignal
    from crypto_monitor.scoring import build_opportunity
    from crypto_monitor.signal_engine import (
        SignalAssessment, assess_opportunity, is_cold_market,
        market_allowed, qualified_for_push,
    )
    from crypto_monitor.digest_notifier import DigestNotifier
    from crypto_monitor.alert_history import (
        get_alert_stats,
        get_recent_alerts,
        get_symbol_history,
        log_full_alert,
        log_pump_alert,
    )
    from crypto_monitor.divergence_detector import comprehensive_divergence_check, DivergenceSignal
    from crypto_monitor.webhook_notifier import get_webhook
    from crypto_monitor.market_structure import analyze_structure
    from crypto_monitor.anomaly_detector import scan_anomalies
    from crypto_monitor.runtime_status import RuntimeStatusStore
else:
    from .config import ASR_STRATEGY, ENABLE_CONSOLE, ENABLE_DEBUG, RUN_ONCE, PUMP, RAPID, SCAN, SIGNAL, SNAPSHOT_DIR, STATE_DIR
    from .asr_strategy_monitor import AsrBtcV17StrategyMonitor
    from .exchange_client import BinanceFuturesClient
    from .indicators import build_indicator_map
    from .levels import build_context
    from .models import Candle, Opportunity, SymbolMarket
    from .notifier import TelegramNotifier
    from .pump_detector import PATTERN_NONE, evaluate_pump, PumpSignal
    from .scoring import build_opportunity
    from .signal_engine import (
        SignalAssessment, assess_opportunity, is_cold_market,
        market_allowed, qualified_for_push,
    )
    from .digest_notifier import DigestNotifier
    from .alert_history import (
        get_alert_stats,
        get_recent_alerts,
        get_symbol_history,
        log_full_alert,
        log_pump_alert,
    )
    from .divergence_detector import comprehensive_divergence_check, DivergenceSignal
    from .webhook_notifier import get_webhook
    from .market_structure import analyze_structure
    from .anomaly_detector import scan_anomalies
    from .runtime_status import RuntimeStatusStore


class CryptoAlertMonitor:
    """全市场加密货币异动预警机器人。"""

    def __init__(self):
        self.client = BinanceFuturesClient()
        self.notifier = TelegramNotifier()
        self.webhook = get_webhook()
        self.digest = DigestNotifier(self.notifier, self.webhook)
        self.asr_strategy = AsrBtcV17StrategyMonitor(self.notifier, self.client)
        self.status = RuntimeStatusStore()
        self.previous_open_interest: Dict[str, float] = self._load_open_interest_state()
        self.oi_history: Dict[str, List[Tuple[float, float]]] = {}
        self.last_assessments: Dict[str, SignalAssessment] = {}
        self.market_cold = False
        self.market_structure = None
        self._full_scan_count = 0

    @staticmethod
    def _result_count(result: Any) -> int:
        if result is None:
            return 0
        if isinstance(result, int):
            return result
        if isinstance(result, (list, tuple, set, dict)):
            return len(result)
        return 1

    def _run_component(self, name: str, func, *args, **kwargs):
        started_at = time.perf_counter()
        self.status.heartbeat(force=True)
        self.status.start_component(name)
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            self.status.finish_component(
                name,
                success=False,
                duration_seconds=time.perf_counter() - started_at,
                error=str(exc),
            )
            self.status.heartbeat(force=True)
            raise
        self.status.finish_component(
            name,
            success=True,
            duration_seconds=time.perf_counter() - started_at,
            result_count=self._result_count(result),
        )
        self.status.heartbeat(force=True)
        return result

    def _recent_quote_volume(self, candles: Optional[List[dict]], bars: int) -> float:
        if not candles:
            return 0.0
        total = 0.0
        for candle in candles[-bars:]:
            total += float(candle.get("close", 0.0)) * float(candle.get("volume", 0.0))
        return total

    def _recent_quote_volume_from_candles(self, candles: Optional[List[Candle]], bars: int) -> float:
        if not candles:
            return 0.0
        total = 0.0
        for candle in candles[-bars:]:
            total += float(candle.close) * float(candle.volume)
        return total

    def _passes_rapid_liquidity_filter(self, candles_5m: Optional[List[dict]], candles_15m: Optional[List[dict]]) -> Tuple[bool, float]:
        threshold = float(RAPID.min_4h_quote_volume_usdt)
        if threshold <= 0:
            return True, 0.0
        quote_volume_4h = self._recent_quote_volume(candles_15m, 16)
        if quote_volume_4h <= 0:
            quote_volume_4h = self._recent_quote_volume(candles_5m, 48)
        return quote_volume_4h >= threshold, quote_volume_4h

    # ── 状态持久化 ──
    def _load_open_interest_state(self) -> Dict[str, float]:
        file_path = STATE_DIR / "open_interest.json"
        if not file_path.exists():
            return {}
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return {key: float(value) for key, value in data.items()}
        except Exception:
            return {}

    def _save_open_interest_state(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        file_path = STATE_DIR / "open_interest.json"
        file_path.write_text(
            json.dumps(self.previous_open_interest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── 快速扫描（拉盘预警） ──
    def rapid_scan(self) -> List[PumpSignal]:
        if not RAPID.enabled:
            return []

        markets = self.client.fetch_candidate_tickers_rapid()
        if not markets:
            return []

        symbols = [m.symbol for m in markets]
        if ENABLE_CONSOLE:
            print(f"[快扫] {len(symbols)} 候选币种，并行获取 5m/15m K线 ...")

        candles_map = self.client.fetch_rapid_candles_batch(symbols)

        pump_signals = []
        for market in markets:
            candles = candles_map.get(market.symbol)
            if not candles:
                continue

            candles_5m = candles.get("5m")
            candles_15m = candles.get("15m")
            if not candles_5m and not candles_15m:
                continue

            liquidity_ok, quote_volume_4h = self._passes_rapid_liquidity_filter(candles_5m, candles_15m)
            if not liquidity_ok:
                if ENABLE_DEBUG:
                    print(f"[快扫] 跳过 {market.symbol}: 近4小时成交额 {quote_volume_4h / 1_000_000:.2f}M < {RAPID.min_4h_quote_volume_usdt / 1_000_000:.2f}M")
                continue

            oi_change = None
            prev_oi = self.previous_open_interest.get(market.symbol)
            if prev_oi and prev_oi > 0:
                current_oi = self.client.fetch_open_interest(market.symbol)
                if current_oi and current_oi > 0:
                    oi_change = (current_oi - prev_oi) / prev_oi * 100.0
                    self.previous_open_interest[market.symbol] = current_oi

            ps = evaluate_pump(
                symbol=market.symbol,
                candles_5m=candles_5m,
                candles_15m=candles_15m,
                change_24h_pct=market.percentage,
                open_interest_change_pct=oi_change,
            )

            if ps.pattern != PATTERN_NONE and ps.confidence >= RAPID.pump_min_confidence:
                pump_signals.append(ps)
                self.digest.record_pump_alert(ps.symbol, ps.pattern)

            # 异常检测（轻量级，复用已获取的 K 线）
            anomalies = scan_anomalies(
                market.symbol, candles_5m, candles_15m,
                quote_volume_24h=market.quote_volume,
                oi_change_pct=oi_change,
            )
            for anom in anomalies:
                if anom.severity in ("CRITICAL", "HIGH"):
                    self.digest.record_anomaly(anom)

        if pump_signals:
            sent = self.notifier.send_pump_alerts(pump_signals)

            # Webhook + 历史记录
            for ps in pump_signals:
                try:
                    self.webhook.send_pump_alert(ps)
                except Exception:
                    pass
                try:
                    log_pump_alert(
                        symbol=ps.symbol, pattern=ps.pattern,
                        confidence=ps.confidence, price_level=ps.price_level,
                        oi_level=ps.oi_level, alert_priority=ps.alert_priority,
                        tags=ps.tags, change_5m=ps.change_5m, change_15m=ps.change_15m,
                        ma_score_5m=ps.ma_score_5m, ma_score_15m=ps.ma_score_15m,
                        vol_spike_5m=ps.vol_spike_5m, vol_spike_15m=ps.vol_spike_15m,
                        rsi_5m=ps.rsi_5m, consecutive_bull_5m=ps.consecutive_bull_5m,
                        oi_change_pct=ps.oi_change_pct, reasons=ps.reasons, pushed=True,
                    )
                except Exception:
                    pass

            if ENABLE_CONSOLE:
                patterns = {}
                for p in pump_signals:
                    patterns[p.pattern] = patterns.get(p.pattern, 0) + 1
                pattern_info = " ".join(f"{k}={v}" for k, v in patterns.items())
                print(f"[快扫] 拉盘预警 {len(pump_signals)} 条，推送 {sent} 条 ({pattern_info})")

        return pump_signals

    # ── 全量扫描 ──
    def scan_once(self) -> List[Opportunity]:
        markets = self.client.fetch_candidate_markets()
        if not markets:
            print("本轮未获取到 Binance 候选市场，可能是网络、代理或 Binance 访问限制导致。")
            self.last_assessments = {}
            self.market_cold = True
            return []

        markets = [m for m in markets if market_allowed(m)]
        self.market_cold = is_cold_market(markets)

        opportunities = []
        assessments = {}
        divergence_warnings: Dict[str, List[DivergenceSignal]] = {}
        indicator_map = {}
        oi_changes = {}

        for index, market in enumerate(markets, start=1):
            try:
                analysis = self.analyze_market(market)
                if analysis:
                    opportunity, candles_by_tf = analysis
                    indicator_map[market.symbol] = opportunity.indicators
                    if opportunity.open_interest_change_pct is not None:
                        oi_changes[market.symbol] = opportunity.open_interest_change_pct
                    divs = comprehensive_divergence_check(
                        market.symbol, candles_by_tf,
                        opportunity.indicators,
                        opportunity.open_interest_change_pct,
                    )
                    if divs:
                        divergence_warnings[market.symbol] = divs

                    assessment = assess_opportunity(opportunity, self.market_cold)
                    opportunities.append(opportunity)
                    assessments[opportunity.symbol] = assessment

                    if ENABLE_DEBUG:
                        blocked = "BLOCKED" if assessment.blocked_reasons else "OK"
                        div_info = f" DIV={len(divs)}" if divs else ""
                        print(f"  {index}/{len(markets)} {market.symbol} "
                              f"score={opportunity.score} grade={opportunity.grade} {blocked}{div_info}")
            except Exception as exc:
                if ENABLE_DEBUG:
                    print(f"  分析 {market.symbol} 失败: {exc}")

        self.market_structure = analyze_structure(
            markets,
            indicators_map=indicator_map or None,
            oi_changes=oi_changes or None,
        )

        if ENABLE_CONSOLE:
            state = "偏冷" if self.market_cold else "活跃"
            print(f"\n[全扫] {len(markets)} 候选 | 热度: {state} | {self.market_structure.summary}")

        opportunities.sort(key=lambda item: item.score, reverse=True)
        self.last_assessments = assessments
        self._divergence_warnings = divergence_warnings

        if SCAN.top_results and SCAN.top_results > 0:
            return opportunities[: SCAN.top_results]
        return opportunities

    def analyze_market(self, market: SymbolMarket) -> Optional[Tuple[Opportunity, Dict[str, List[Candle]]]]:
        candles_by_timeframe = self.client.fetch_ohlcv_map_parallel(market.symbol)
        if len(candles_by_timeframe) < 2:
            return None
        indicators = build_indicator_map(candles_by_timeframe)
        if not indicators:
            return None
        context = build_context(candles_by_timeframe, indicators)
        funding_rate = self.client.fetch_funding_rate(market.symbol)
        open_interest, open_interest_change_pct = self._open_interest_with_change(market.symbol)
        quote_volume_4h = self._recent_quote_volume_from_candles(candles_by_timeframe.get("15m"), 16)
        if quote_volume_4h <= 0:
            quote_volume_4h = self._recent_quote_volume_from_candles(candles_by_timeframe.get("5m"), 48)
        opportunity = build_opportunity(
            market=market,
            indicators=indicators,
            context=context,
            funding_rate=funding_rate,
            open_interest=open_interest,
            open_interest_change_pct=open_interest_change_pct,
            quote_volume_4h=quote_volume_4h,
        )
        return opportunity, candles_by_timeframe

    def _open_interest_with_change(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        current = self.client.fetch_open_interest(symbol)
        if current is None:
            return None, None

        now = time.time()
        if symbol not in self.oi_history:
            self.oi_history[symbol] = []
        self.oi_history[symbol].append((now, current))
        self.oi_history[symbol] = [
            (t, v) for t, v in self.oi_history[symbol] if now - t <= 14400
        ]

        stored_oi = self.previous_open_interest.get(symbol)
        self.previous_open_interest[symbol] = current

        if stored_oi and stored_oi > 0 and len(self.oi_history[symbol]) <= 1:
            change = (current - stored_oi) / stored_oi * 100.0
            return current, change

        oi_history = self.oi_history[symbol]
        best_change = None
        for t, v in oi_history:
            age = now - t
            if 1800 <= age <= 7200:
                change = (current - v) / v * 100.0 if v > 0 else 0.0
                if best_change is None or abs(change) > abs(best_change):
                    best_change = change

        if best_change is not None:
            return current, best_change

        if stored_oi is not None and stored_oi > 0:
            change = (current - stored_oi) / stored_oi * 100.0
            return current, change

        return current, None

    def handle_results(self, opportunities: List[Opportunity]):
        self._save_open_interest_state()
        self._save_snapshot(opportunities)

        if ENABLE_CONSOLE:
            self._print_top(opportunities)

        qualified = []
        for item in opportunities:
            assessment = self.last_assessments.get(
                item.symbol, SignalAssessment(item.symbol)
            )
            if qualified_for_push(item, assessment):
                qualified.append((item, assessment))

        prio_order = {"URGENT": 0, "HIGH": 1, "NORMAL": 2, "WATCH": 3}
        qualified.sort(
            key=lambda x: (
                prio_order.get(x[1].alert_level, 4),
                -x[0].score,
            )
        )

        items_only = [q[0] for q in qualified]

        # ── 推送 Telegram ──
        push_stats = self.notifier.send_opportunities(items_only, self.last_assessments)
        sent_symbols = set(push_stats.get("sent_symbols", []))

        # ── 推送 Discord/Webhook ──
        for item, assessment in qualified:
            if item.symbol not in sent_symbols:
                continue
            try:
                self.webhook.send_full_alert(item, assessment)
            except Exception:
                pass

        # ── 记录到 SQLite 历史 ──
        for item, assessment in qualified:
            try:
                log_full_alert(
                    symbol=item.symbol,
                    score=item.score,
                    grade=item.grade,
                    alert_level=assessment.alert_level,
                    tags=assessment.tags,
                    price=item.current_price,
                    change_24h=item.change_24h_pct,
                    quote_volume=item.quote_volume,
                    oi_change_pct=item.open_interest_change_pct,
                    funding_rate=item.funding_rate,
                    indicators=item.indicators,
                    reasons=item.reasons,
                    pushed=item.symbol in sent_symbols,
                    push_summary=assessment.trigger_reason,
                )
            except Exception:
                pass

        if ENABLE_CONSOLE:
            print(
                f"Telegram: 新{push_stats['new']} 更新{push_stats['updated']} "
                f"未变{push_stats['unchanged']} 实发{push_stats['sent']}"
            )

        # ── 摘要（含市场结构+背离警告） ──
        div_warnings = getattr(self, '_divergence_warnings', {})
        self.digest.send_digest(
            items_only, self.market_cold, push_stats,
            market_structure=self.market_structure,
            divergence_warnings=div_warnings,
        )
        self.digest.send_pushed_list_digest()

    def _cleanup_snapshots(self, keep: int = 50):
        """保留最近 keep 个快照文件，删除更早的。"""
        files = sorted(SNAPSHOT_DIR.glob("opportunities_*.json"))
        if len(files) <= keep:
            return
        for f in files[:-keep]:
            try:
                f.unlink()
            except Exception:
                pass
        if ENABLE_DEBUG and len(files) > keep:
            print(f"[清理] 删除 {len(files) - keep} 个旧快照，保留 {keep} 个")

    def _save_snapshot(self, opportunities: List[Opportunity]):
        if not SCAN.snapshot_enabled:
            return
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        file_path = SNAPSHOT_DIR / f"opportunities_{stamp}.json"
        payload = [item.to_dict() for item in opportunities]
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _print_top(self, opportunities: List[Opportunity]):
        if not opportunities:
            print("本轮未发现符合条件的机会。")
            return
        print("扫描结果:")
        for item in opportunities:
            assessment = self.last_assessments.get(item.symbol)
            oi_change = (
                "N/A" if item.open_interest_change_pct is None
                else f"{item.open_interest_change_pct:.1f}%"
            )
            tags = ",".join(assessment.tags[:5]) if assessment else "N/A"
            filtered = "" if assessment and not assessment.blocked_reasons else ""
            i15 = item.indicators.get("15m")
            ma_str = f" MA={i15.ma_alignment_score:.0f}" if i15 and i15.ma_alignment_score >= 50 else ""
            con_str = f" CB={i15.consecutive_bull}" if i15 and i15.consecutive_bull >= 3 else ""
            print(
                f"  {item.grade} {item.symbol:<18} score={item.score:>5.2f} "
                f"24h={item.change_24h_pct:>6.2f}% vol={item.quote_volume / 1_000_000:>8.2f}M "
                f"OI={oi_change:>8}{ma_str}{con_str} tags={tags}{filtered}"
            )

    def _print_startup(self):
        print("=" * 60)
        print("🛡️  加密货币全市场异动预警机器人 v3.0")
        top_text = "ALL" if not SCAN.top_results or SCAN.top_results <= 0 else str(SCAN.top_results)
        print(f"全量扫描: {SCAN.interval_seconds}s | 候选: {SCAN.max_symbols_per_scan or 'ALL'} | Top: {top_text}")
        if RAPID.enabled:
            rapid_candidates = "ALL" if not RAPID.max_symbols or RAPID.max_symbols <= 0 else str(RAPID.max_symbols)
            print(f"快速预警: {RAPID.interval_seconds}s | 候选: {rapid_candidates} | 周期: 5m/15m")
            print(f"  流动性过滤: 近4小时成交额 ≥ {RAPID.min_4h_quote_volume_usdt / 1_000_000:.1f}M USDT")
            print(f"  价格档位: T1≥{PUMP.price_surge_5m_t1}% T2≥{PUMP.price_surge_5m_t2}% T3≥{PUMP.price_surge_5m_t3}%")
            print(f"  OI档位: T1≥{PUMP.oi_surge_t1}% T2≥{PUMP.oi_surge_t2}% T3≥{PUMP.oi_surge_t3}%")
            print(f"  形态要求: MA对齐≥{PUMP.ma_alignment_min}线 | 连阳≥{PUMP.min_consecutive_bull_5m}根(5m)")
        print(f"推送阈值: {SIGNAL.min_push_score} | 最少标签: {SIGNAL.min_signal_tags}")
        print(f"摘要: {'开启' if SIGNAL.digest_enabled else '关闭'} | 异动预警: {'开启' if SIGNAL.spike_alert_enabled else '关闭'}")
        print(f"Webhook: {'Discord' if self.webhook.discord_enabled else ''} {'HTTP' if self.webhook.generic_enabled else ''}".replace('  ',' ').strip() or "Webhook: 未配置")
        if ASR_STRATEGY.enabled:
            print(f"ASR策略: 开启 | {ASR_STRATEGY.symbol} {ASR_STRATEGY.version} | 周期: {ASR_STRATEGY.timeframe_minutes}m | 扫描: {ASR_STRATEGY.interval_seconds}s")
        print(f"运行状态: {self.status.format_console_summary()}")
        print(f"新增: 背离检测 | 市场结构分析 | 预警历史(SQLite) | 板块热度")
        print("=" * 60)

    # ── 主循环 ──
    def run(self):
        self.status.set_mode("monitor")
        self.status.heartbeat(force=True)
        self._print_startup()
        last_full_scan = 0.0
        last_rapid_scan = 0.0
        last_asr_strategy_scan = 0.0

        while True:
            now = time.time()
            self.status.heartbeat()
            try:
                if ASR_STRATEGY.enabled and (now - last_asr_strategy_scan) >= ASR_STRATEGY.interval_seconds:
                    self._run_component("asr_strategy", self.asr_strategy.run_once)
                    last_asr_strategy_scan = time.time()

                if RAPID.enabled and (now - last_rapid_scan) >= RAPID.interval_seconds:
                    self._run_component("rapid_scan", self.rapid_scan)
                    last_rapid_scan = now

                if (now - last_full_scan) >= SCAN.interval_seconds:
                    opportunities = self._run_component("full_scan", self.scan_once)
                    self.handle_results(opportunities)
                    last_full_scan = time.time()
                    self._full_scan_count += 1
                    # 每 10 轮全量扫描清理一次旧快照
                    if self._full_scan_count % 10 == 0:
                        self._cleanup_snapshots(keep=50)

            except KeyboardInterrupt:
                print("\n监控已停止。")
                break
            except Exception as exc:
                print(f"[ERROR] 扫描异常: {exc}")
                if ENABLE_DEBUG:
                    import traceback
                    traceback.print_exc()

            next_full = last_full_scan + SCAN.interval_seconds - time.time()
            next_rapid = last_rapid_scan + RAPID.interval_seconds - time.time()
            next_asr = last_asr_strategy_scan + ASR_STRATEGY.interval_seconds - time.time()
            waits = [max(3, next_full)]
            if RAPID.enabled:
                waits.append(max(3, next_rapid))
            if ASR_STRATEGY.enabled:
                waits.append(max(3, next_asr))
            sleep_for = min(waits)
            time.sleep(min(sleep_for, 10))

    def run_once(self):
        self.status.set_mode("once")
        self.status.heartbeat(force=True)
        self._print_startup()
        if ASR_STRATEGY.enabled:
            self._run_component("asr_strategy", self.asr_strategy.run_once)
        opportunities = self._run_component("full_scan", self.scan_once)
        self.handle_results(opportunities)
        return opportunities


def _print_json(payload: Any):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _load_runtime_status_snapshot() -> Dict[str, Any]:
    file_path = STATE_DIR / "runtime_status.json"
    if not file_path.exists():
        store = RuntimeStatusStore(file_path=file_path)
        return {
            "available": True,
            "file": str(file_path),
            "status": store.snapshot(),
        }
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "available": False,
            "file": str(file_path),
            "error": str(exc),
        }
    return {
        "available": True,
        "file": str(file_path),
        "status": payload,
    }


def _show_recent_alerts(minutes: int):
    _print_json({
        "minutes": minutes,
        "items": get_recent_alerts(minutes=minutes),
    })


def _show_symbol_history(symbol: str, limit: int):
    _print_json({
        "symbol": symbol,
        "limit": limit,
        "items": get_symbol_history(symbol=symbol, limit=limit),
    })


def _show_alert_stats(hours: int):
    _print_json({
        "hours": hours,
        "stats": get_alert_stats(min_hours=hours),
    })


def main():
    parser = argparse.ArgumentParser(description="Binance USDT 永续全市场异动预警机器人 v3.0")
    parser.add_argument("--once", action="store_true", help="只扫描一轮后退出")
    parser.add_argument("--rapid-only", action="store_true", help="只运行快速拉盘预警")
    parser.add_argument("--no-rapid", action="store_true", help="禁用快速拉盘预警")
    parser.add_argument("--test-telegram", action="store_true", help="发送一条 Telegram 测试消息后退出")
    parser.add_argument("--wait-telegram-chat", action="store_true", help="等待用户给机器人发消息并自动缓存 chat_id")
    parser.add_argument("--status", action="store_true", help="输出运行状态 JSON 后退出")
    parser.add_argument("--recent-alerts", action="store_true", help="输出最近预警列表后退出")
    parser.add_argument("--recent-alerts-minutes", type=int, default=60, help="最近预警查询时间窗口（分钟）")
    parser.add_argument("--history-symbol", default="", help="输出指定币种预警历史后退出")
    parser.add_argument("--history-limit", type=int, default=20, help="币种历史输出条数")
    parser.add_argument("--alert-stats-hours", type=int, default=0, help="输出最近 N 小时预警统计后退出")
    args = parser.parse_args()

    if args.status:
        _print_json(_load_runtime_status_snapshot())
        return
    if args.recent_alerts:
        _show_recent_alerts(minutes=max(1, args.recent_alerts_minutes))
        return
    if args.history_symbol:
        _show_symbol_history(symbol=args.history_symbol.upper(), limit=max(1, args.history_limit))
        return
    if args.alert_stats_hours > 0:
        _show_alert_stats(hours=max(1, args.alert_stats_hours))
        return

    monitor = CryptoAlertMonitor()

    if args.wait_telegram_chat:
        monitor.status.set_mode("telegram_setup")
        monitor.notifier.check_bot()
        chat_id = monitor.notifier.wait_for_chat_id()
        if chat_id:
            ok = monitor.notifier.send_message(
                "Crypto Monitor v3.0: chat_id 已识别。全市场异动预警已就绪。"
            )
            print(f"telegram_test_sent={ok}")
        return

    if args.test_telegram:
        monitor.status.set_mode("telegram_test")
        monitor.notifier.check_bot()
        ok = monitor.notifier.send_message(
            "Crypto Monitor v3.0 测试: 全市场异动预警链路已接入。"
        )
        print(f"telegram_test_sent={ok}")
        return

    if args.no_rapid:
        import crypto_monitor.config as _cfg
        disabled_rapid = _cfg.RAPID.__class__(
            enabled=False,
            interval_seconds=_cfg.RAPID.interval_seconds,
            timeframes=_cfg.RAPID.timeframes,
            min_quote_volume_usdt=_cfg.RAPID.min_quote_volume_usdt,
            min_4h_quote_volume_usdt=_cfg.RAPID.min_4h_quote_volume_usdt,
            max_symbols=_cfg.RAPID.max_symbols,
            min_24h_change_pct=_cfg.RAPID.min_24h_change_pct,
            pump_min_confidence=_cfg.RAPID.pump_min_confidence,
        )
        _cfg.RAPID = disabled_rapid
        globals()["RAPID"] = disabled_rapid

    if args.rapid_only:
        monitor.status.set_mode("rapid_only")
        monitor.status.heartbeat(force=True)
        monitor._print_startup()
        print("仅运行快速拉盘预警模式。")
        last_rapid = 0.0
        last_asr_strategy_scan = 0.0
        while True:
            try:
                now = time.time()
                monitor.status.heartbeat()
                if ASR_STRATEGY.enabled and (now - last_asr_strategy_scan) >= ASR_STRATEGY.interval_seconds:
                    monitor._run_component("asr_strategy", monitor.asr_strategy.run_once)
                    last_asr_strategy_scan = time.time()
                if (now - last_rapid) >= RAPID.interval_seconds:
                    monitor._run_component("rapid_scan", monitor.rapid_scan)
                    last_rapid = now
                waits = [max(1, RAPID.interval_seconds - (time.time() - last_rapid))]
                if ASR_STRATEGY.enabled:
                    waits.append(max(1, ASR_STRATEGY.interval_seconds - (time.time() - last_asr_strategy_scan)))
                sleep_for = min(waits)
                time.sleep(min(sleep_for, 5))
            except KeyboardInterrupt:
                print("\n监控已停止。")
                break
            except Exception as exc:
                print(f"[ERROR] 快速扫描异常: {exc}")
        return

    if args.once or RUN_ONCE:
        monitor.run_once()
        return

    monitor.run()


if __name__ == "__main__":
    main()
