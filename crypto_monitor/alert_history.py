# -*- coding: utf-8 -*-
"""
预警历史持久化 —— SQLite 存储，支持查询、统计、回溯。

每一条预警都完整记录：
  - 币种、时间、评分、等级
  - 形态类型、信号标签、预警级别
  - 价格、涨幅、成交额、OI 变化
  - 关键指标 (MA对齐、持续动量、RSI)
  - 是否被推送、推送内容摘要

用途:
  1. 回顾历史预警，看哪些信号最终走出来了
  2. 统计各形态的胜率
  3. 避免短时间内对同一币种重复推送
  4. 生成预警日报
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import DATA_DIR

DB_PATH = DATA_DIR / "alert_history.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建表（幂等）。"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            created_at TEXT NOT NULL,
            alert_type TEXT NOT NULL DEFAULT 'full_scan',
            score REAL,
            grade TEXT,
            alert_level TEXT,
            pattern TEXT,
            tags TEXT,
            price REAL,
            change_5m REAL,
            change_15m REAL,
            change_24h REAL,
            quote_volume REAL,
            oi_change_pct REAL,
            funding_rate REAL,
            ma_alignment_5m REAL,
            ma_alignment_15m REAL,
            sustained_momentum_5m REAL,
            consecutive_bull_5m INTEGER,
            volume_spike_5m REAL,
            volume_spike_15m REAL,
            rsi_5m REAL,
            rsi_15m REAL,
            confidence REAL,
            pushed INTEGER DEFAULT 0,
            push_summary TEXT,
            raw_reasons TEXT
        );

        CREATE TABLE IF NOT EXISTS pump_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            created_at TEXT NOT NULL,
            pattern TEXT,
            confidence REAL,
            price_level INTEGER,
            oi_level INTEGER,
            alert_priority TEXT,
            tags TEXT,
            change_5m REAL,
            change_15m REAL,
            ma_score_5m REAL,
            ma_score_15m REAL,
            vol_spike_5m REAL,
            vol_spike_15m REAL,
            rsi_5m REAL,
            consecutive_bull_5m INTEGER,
            oi_change_pct REAL,
            pushed INTEGER DEFAULT 0,
            reasons TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol);
        CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);
        CREATE INDEX IF NOT EXISTS idx_alerts_level ON alerts(alert_level);
        CREATE INDEX IF NOT EXISTS idx_alerts_pattern ON alerts(pattern);
        CREATE INDEX IF NOT EXISTS idx_pump_symbol ON pump_alerts(symbol);
        CREATE INDEX IF NOT EXISTS idx_pump_created ON pump_alerts(created_at);
        CREATE INDEX IF NOT EXISTS idx_pump_pattern ON pump_alerts(pattern);

        CREATE TABLE IF NOT EXISTS alert_stats (
            symbol TEXT PRIMARY KEY,
            total_alerts INTEGER DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT,
            max_score REAL DEFAULT 0,
            max_grade TEXT,
            best_pattern TEXT,
            peak_price REAL,
            peak_oi_change REAL
        );

        CREATE TABLE IF NOT EXISTS daily_summary (
            date TEXT PRIMARY KEY,
            total_alerts INTEGER DEFAULT 0,
            total_pump_alerts INTEGER DEFAULT 0,
            urgent_count INTEGER DEFAULT 0,
            high_count INTEGER DEFAULT 0,
            top_symbols TEXT,
            top_patterns TEXT,
            market_heat TEXT
        );
    """)
    conn.commit()
    conn.close()


# ── 写入 ──────────────────────────────────────────────────

def log_full_alert(
    symbol: str,
    score: float,
    grade: str,
    alert_level: str,
    tags: List[str],
    price: float,
    change_24h: float,
    quote_volume: float,
    oi_change_pct: Optional[float],
    funding_rate: Optional[float],
    indicators: Dict,
    reasons: List[str],
    pushed: bool = False,
    push_summary: str = "",
    alert_type: str = "full_scan",
    pattern: str = "",
    confidence: float = 0.0,
) -> int:
    conn = _get_conn()
    i5 = indicators.get("5m")
    i15 = indicators.get("15m")
    row = {
        "symbol": symbol,
        "created_at": _now_iso(),
        "alert_type": alert_type,
        "score": score,
        "grade": grade,
        "alert_level": alert_level,
        "pattern": pattern,
        "tags": ",".join(tags) if tags else "",
        "price": price,
        "change_5m": i5.change_pct if i5 else None,
        "change_15m": i15.change_pct if i15 else None,
        "change_24h": change_24h,
        "quote_volume": quote_volume,
        "oi_change_pct": oi_change_pct,
        "funding_rate": funding_rate,
        "ma_alignment_5m": i5.ma_alignment_score if i5 else None,
        "ma_alignment_15m": i15.ma_alignment_score if i15 else None,
        "sustained_momentum_5m": i5.sustained_momentum_score if i5 else None,
        "consecutive_bull_5m": i5.consecutive_bull if i5 else None,
        "volume_spike_5m": i5.volume_spike if i5 else None,
        "volume_spike_15m": i15.volume_spike if i15 else None,
        "rsi_5m": i5.rsi14 if i5 else None,
        "rsi_15m": i15.rsi14 if i15 else None,
        "confidence": confidence,
        "pushed": 1 if pushed else 0,
        "push_summary": push_summary,
        "raw_reasons": json.dumps(reasons, ensure_ascii=False) if reasons else "",
    }
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO alerts ({cols}) VALUES ({placeholders})", list(row.values()))

    # 更新统计
    conn.execute("""
        INSERT INTO alert_stats (symbol, total_alerts, first_seen, last_seen, max_score, max_grade, best_pattern, peak_price, peak_oi_change)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            total_alerts = total_alerts + 1,
            last_seen = excluded.last_seen,
            max_score = MAX(max_score, excluded.max_score),
            max_grade = CASE WHEN
                CASE excluded.max_grade WHEN 'S' THEN 5 WHEN 'A' THEN 4 WHEN 'B' THEN 3 WHEN 'C' THEN 2 ELSE 1 END
                > CASE max_grade WHEN 'S' THEN 5 WHEN 'A' THEN 4 WHEN 'B' THEN 3 WHEN 'C' THEN 2 ELSE 1 END
                THEN excluded.max_grade ELSE max_grade END,
            best_pattern = CASE WHEN excluded.max_score > max_score THEN excluded.best_pattern ELSE best_pattern END,
            peak_price = MAX(peak_price, excluded.peak_price),
            peak_oi_change = MAX(COALESCE(peak_oi_change, 0), COALESCE(excluded.peak_oi_change, 0))
    """, [
        symbol, _now_iso(), _now_iso(), score, grade, pattern or "",
        price, oi_change_pct or 0,
    ])

    conn.commit()
    rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return rowid


def log_pump_alert(
    symbol: str,
    pattern: str,
    confidence: float,
    price_level: int,
    oi_level: int,
    alert_priority: str,
    tags: List[str],
    change_5m: float,
    change_15m: float,
    ma_score_5m: float,
    ma_score_15m: float,
    vol_spike_5m: float,
    vol_spike_15m: float,
    rsi_5m: float,
    consecutive_bull_5m: int,
    oi_change_pct: Optional[float],
    reasons: List[str],
    pushed: bool = False,
) -> int:
    conn = _get_conn()
    row = {
        "symbol": symbol,
        "created_at": _now_iso(),
        "pattern": pattern,
        "confidence": confidence,
        "price_level": price_level,
        "oi_level": oi_level,
        "alert_priority": alert_priority,
        "tags": ",".join(tags) if tags else "",
        "change_5m": change_5m,
        "change_15m": change_15m,
        "ma_score_5m": ma_score_5m,
        "ma_score_15m": ma_score_15m,
        "vol_spike_5m": vol_spike_5m,
        "vol_spike_15m": vol_spike_15m,
        "rsi_5m": rsi_5m,
        "consecutive_bull_5m": consecutive_bull_5m,
        "oi_change_pct": oi_change_pct,
        "pushed": 1 if pushed else 0,
        "reasons": json.dumps(reasons, ensure_ascii=False) if reasons else "",
    }
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO pump_alerts ({cols}) VALUES ({placeholders})", list(row.values()))
    conn.commit()
    rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return rowid


# ── 查询 ──────────────────────────────────────────────────

def get_recent_alerts(minutes: int = 60, min_level: str = "NORMAL") -> List[Dict]:
    """获取最近 N 分钟内的预警（去重）。"""
    conn = _get_conn()
    cutoff = datetime.now(timezone.utc).isoformat()
    # 简单起见，用时间戳比较
    rows = conn.execute("""
        SELECT DISTINCT symbol, MAX(score) as score, MAX(grade) as grade,
               MAX(alert_level) as alert_level, MAX(pattern) as pattern,
               MAX(price) as price, MAX(change_24h) as change_24h,
               MAX(oi_change_pct) as oi_change_pct, MAX(created_at) as created_at
        FROM alerts
        WHERE created_at >= datetime('now', ? || ' minutes')
          AND alert_level IN (?, ?, ?, ?)
        GROUP BY symbol
        ORDER BY score DESC
    """, [str(-minutes), "URGENT", "HIGH", "NORMAL", "WATCH"]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_symbol_history(symbol: str, limit: int = 20) -> List[Dict]:
    """某币种的预警历史。"""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT * FROM alerts WHERE symbol = ? ORDER BY created_at DESC LIMIT ?
    """, [symbol, limit]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_alert_stats(min_hours: int = 24) -> Dict:
    """全局预警统计。"""
    conn = _get_conn()
    stats = {}
    row = conn.execute("""
        SELECT COUNT(*) as total, COUNT(DISTINCT symbol) as symbols,
               SUM(CASE WHEN pushed=1 THEN 1 ELSE 0 END) as pushed_count
        FROM alerts WHERE created_at >= datetime('now', ? || ' hours')
    """, [str(-min_hours)]).fetchone()
    stats.update(dict(row))

    # 按预警级别
    levels = conn.execute("""
        SELECT alert_level, COUNT(*) as cnt FROM alerts
        WHERE created_at >= datetime('now', ? || ' hours')
        GROUP BY alert_level ORDER BY cnt DESC
    """, [str(-min_hours)]).fetchall()
    stats["by_level"] = {r["alert_level"]: r["cnt"] for r in levels}

    # 按形态
    patterns = conn.execute("""
        SELECT pattern, COUNT(*) as cnt FROM alerts
        WHERE created_at >= datetime('now', ? || ' hours') AND pattern != ''
        GROUP BY pattern ORDER BY cnt DESC
    """, [str(-min_hours)]).fetchall()
    stats["by_pattern"] = {r["pattern"]: r["cnt"] for r in patterns}

    # Top 币种
    top = conn.execute("""
        SELECT symbol, MAX(score) as max_score, MAX(grade) as max_grade, COUNT(*) as cnt
        FROM alerts
        WHERE created_at >= datetime('now', ? || ' hours')
        GROUP BY symbol ORDER BY max_score DESC LIMIT 10
    """, [str(-min_hours)]).fetchall()
    stats["top_symbols"] = [dict(r) for r in top]

    conn.close()
    return stats


def should_suppress_recent(symbol: str, minutes: int = 15, min_level: str = "HIGH") -> bool:
    """检查某币种最近是否已有高级别预警（抑制重复推送）。"""
    conn = _get_conn()
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM alerts
        WHERE symbol = ?
          AND created_at >= datetime('now', ? || ' minutes')
          AND alert_level IN ('URGENT', 'HIGH')
    """, [symbol, str(-minutes)]).fetchone()
    conn.close()
    return row["cnt"] > 0 if row else False


# ── 初始化 ────────────────────────────────────────────────
init_db()
