# -*- coding: utf-8 -*-

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import ccxt

from .config import ENABLE_DEBUG, PROXY, SCAN, RAPID
from .models import Candle, SymbolMarket
from .notifier import redact_secret


class BinanceFuturesClient:
    def __init__(self):
        config = {
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {"defaultType": "swap"},
        }
        if PROXY.ccxt_proxies:
            config["proxies"] = PROXY.ccxt_proxies
        self.exchange = ccxt.binanceusdm(config)
        self._markets_loaded_at = 0.0
        self._markets = {}
        self.retry_attempts = 3
        self.retry_delay_seconds = 2.0

    def _with_retry(self, label: str, func, default=None):
        last_error = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return func()
            except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable, ccxt.DDoSProtection) as exc:
                last_error = exc
                if ENABLE_DEBUG:
                    print(f"{label} 网络异常，第 {attempt}/{self.retry_attempts} 次: {redact_secret(str(exc))}")
                if attempt < self.retry_attempts:
                    time.sleep(self.retry_delay_seconds * attempt)
            except Exception as exc:
                last_error = exc
                break
        print(f"{label} 失败，已跳过本项: {redact_secret(str(last_error))}")
        return default

    def load_markets(self, force: bool = False):
        now = time.time()
        if not force and self._markets and now - self._markets_loaded_at <= SCAN.market_refresh_seconds:
            return self._markets
        markets = self._with_retry("加载 Binance USDT-M 市场", lambda: self.exchange.load_markets(True), {})
        if markets:
            self._markets = markets
            self._markets_loaded_at = now
        return self._markets

    def fetch_candidate_markets(self) -> List[SymbolMarket]:
        markets = self.load_markets()
        if not markets:
            return []
        tickers = self._with_retry("获取全市场 ticker", self.exchange.fetch_tickers, {})
        if not tickers:
            return []
        candidates = []
        for symbol, market in markets.items():
            if not self._is_usdt_perp(market):
                continue
            ticker = tickers.get(symbol, {})
            item = self._market_from_ticker(symbol, market, ticker)
            if item and self._passes_market_filter(item):
                candidates.append(item)
        candidates.sort(key=lambda item: (item.percentage, item.quote_volume), reverse=True)
        if SCAN.max_symbols_per_scan and SCAN.max_symbols_per_scan > 0:
            return candidates[: SCAN.max_symbols_per_scan]
        return candidates

    def _is_usdt_perp(self, market: Dict) -> bool:
        if not market.get("active", False):
            return False
        if market.get("quote") != "USDT":
            return False
        if market.get("base") in SCAN.excluded_bases:
            return False
        return bool(market.get("swap") or market.get("future") or ":USDT" in market.get("symbol", ""))

    def _market_from_ticker(self, symbol: str, market: Dict, ticker: Dict) -> Optional[SymbolMarket]:
        last = float(ticker.get("last") or ticker.get("close") or 0.0)
        quote_volume = float(ticker.get("quoteVolume") or 0.0)
        percentage = float(ticker.get("percentage") or 0.0)
        high = float(ticker.get("high") or 0.0)
        low = float(ticker.get("low") or 0.0)
        if last <= 0:
            return None
        return SymbolMarket(
            symbol=symbol,
            market_id=str(market.get("id") or symbol.replace("/", "").replace(":USDT", "")),
            base=str(market.get("base") or symbol.split("/")[0]),
            quote=str(market.get("quote") or "USDT"),
            active=bool(market.get("active", True)),
            last=last,
            quote_volume=quote_volume,
            percentage=percentage,
            high=high,
            low=low,
            oi_24h_change_pct=None,  # Binance ticker 不提供 OI 变化率，需单独获取
        )

    def _passes_market_filter(self, market: SymbolMarket) -> bool:
        if market.last < SCAN.min_price:
            return False
        if market.quote_volume < SCAN.min_quote_volume_usdt:
            return False
        if market.percentage < SCAN.min_24h_change_pct:
            return False
        if market.percentage > SCAN.max_24h_change_pct:
            return False
        return True

    def fetch_ohlcv_map(self, symbol: str) -> Dict[str, List[Candle]]:
        result = {}
        for timeframe in SCAN.timeframes:
            rows = self._with_retry(
                f"获取 {symbol} {timeframe} K线",
                lambda symbol=symbol, timeframe=timeframe: self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=SCAN.ohlcv_limit),
                [],
            )
            candles = [Candle.from_ohlcv(row) for row in rows]
            if candles:
                result[timeframe] = candles
        return result

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        funding = self._with_retry(f"获取 {symbol} 资金费率", lambda: self.exchange.fetch_funding_rate(symbol), None)
        if not funding:
            return None
        value = funding.get("fundingRate")
        return float(value) if value is not None else None

    def fetch_open_interest(self, symbol: str) -> Optional[float]:
        data = self._with_retry(f"获取 {symbol} 持仓量", lambda: self.exchange.fetch_open_interest(symbol), None)
        if data:
            value = data.get("openInterestAmount") or data.get("openInterestValue") or data.get("openInterest")
            if value is not None:
                return float(value)
        try:
            market = self.exchange.market(symbol)
            market_id = market.get("id") or symbol.replace("/", "").replace(":USDT", "")
            data = self._with_retry(
                f"获取 {symbol} 原始持仓量",
                lambda market_id=market_id: self.exchange.fapiPublicGetOpenInterest({"symbol": market_id}),
                None,
            )
            if data and data.get("openInterest") is not None:
                return float(data["openInterest"])
        except Exception as exc:
            if ENABLE_DEBUG:
                print(f"{symbol} 持仓量 fallback 失败: {redact_secret(str(exc))}")
            return None

    # ── 快速扫描接口 ──────────────────────────────────────

    def fetch_rapid_candles(self, symbol: str) -> Dict[str, List[Dict]]:
        """获取 5m/15m 短期 K 线（返回原始 dict，轻量级）。"""
        result = {}
        for timeframe in RAPID.timeframes:
            rows = self._with_retry(
                f"快速获取 {symbol} {timeframe} K线",
                lambda symbol=symbol, timeframe=timeframe: self.exchange.fetch_ohlcv(
                    symbol, timeframe=timeframe, limit=60
                ),
                [],
            )
            if rows:
                result[timeframe] = [
                    {"timestamp": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                     "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
                    for r in rows
                ]
        return result

    def fetch_rapid_candles_batch(self, symbols: List[str], max_workers: int = 8) -> Dict[str, Dict[str, List[Dict]]]:
        """并行拉取多个币种的快速 K 线。"""
        result = {}
        if not symbols:
            return result
        with ThreadPoolExecutor(max_workers=min(max_workers, len(symbols))) as executor:
            futures = {executor.submit(self.fetch_rapid_candles, s): s for s in symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    data = future.result()
                    if data:
                        result[symbol] = data
                except Exception as exc:
                    if ENABLE_DEBUG:
                        print(f"并行获取 {symbol} 快速K线失败: {redact_secret(str(exc))}")
        return result

    def fetch_candidate_tickers_rapid(self) -> List[SymbolMarket]:
        """获取快速扫描候选币种（简化版，只看涨跌幅 + 成交额 + 最新价）。"""
        markets = self.load_markets()
        if not markets:
            return []
        tickers = self._with_retry("快速获取 ticker", self.exchange.fetch_tickers, {})
        if not tickers:
            return []
        candidates = []
        for symbol, market in markets.items():
            if not self._is_usdt_perp(market):
                continue
            ticker = tickers.get(symbol, {})
            item = self._market_from_ticker(symbol, market, ticker)
            if item and item.quote_volume >= RAPID.min_quote_volume_usdt and item.percentage >= RAPID.min_24h_change_pct:
                candidates.append(item)
        # 按 (涨跌幅 * 0.6 + 成交额归一化 * 0.4) 排序，优先找放量上涨的
        if candidates:
            max_vol = max(c.quote_volume for c in candidates) if candidates else 1
            candidates.sort(
                key=lambda c: c.percentage * 0.6 + (c.quote_volume / max(1, max_vol)) * 40,
                reverse=True,
            )
        if RAPID.max_symbols and RAPID.max_symbols > 0:
            return candidates[: RAPID.max_symbols]
        return candidates

    def fetch_ohlcv_map_parallel(self, symbol: str, max_workers: int = 4) -> Dict[str, List[Candle]]:
        """并行获取多个时间框架的 K 线。"""
        result = {}
        timeframes = list(SCAN.timeframes)

        def _fetch_one(tf: str):
            rows = self._with_retry(
                f"获取 {symbol} {tf} K线",
                lambda s=symbol, t=tf: self.exchange.fetch_ohlcv(s, timeframe=t, limit=SCAN.ohlcv_limit),
                [],
            )
            return tf, [Candle.from_ohlcv(row) for row in rows]

        with ThreadPoolExecutor(max_workers=min(max_workers, len(timeframes))) as executor:
            futures = [executor.submit(_fetch_one, tf) for tf in timeframes]
            for future in as_completed(futures):
                try:
                    tf, candles = future.result()
                    if candles:
                        result[tf] = candles
                except Exception as exc:
                    if ENABLE_DEBUG:
                        print(f"并行获取 {symbol} K线失败: {redact_secret(str(exc))}")
        return result

