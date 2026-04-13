"""DuckDB 本地寫入器（輕量測試版）。

設計：
  - fmt_1s / fmt_5s_cdv → 回傳 dict，put 進 asyncio.Queue
  - db_writer_task       → 每 0.5s 批次寫入 DuckDB（run_in_executor，不阻塞事件迴圈）
  - DuckDB 單檔案，零依賴 server，可直接匯出 Parquet
"""
from __future__ import annotations

import asyncio
import logging
import pathlib

log = logging.getLogger("ibkr.storage")

_DB_PATH = pathlib.Path("data/market.duckdb")


# ── Row 格式化（回傳 dict，供 Queue 傳遞）─────────────────────────────────────

def fmt_1s(sym: str, bar: dict) -> dict:
    return {
        "table":  "bars_1s",
        "ts":     bar["ts"],        # Unix 秒 int
        "symbol": sym,
        "open":   bar["open"],
        "high":   bar["high"],
        "low":    bar["low"],
        "close":  bar["close"],
        "volume": bar["vol"],
        "vwap":   bar.get("vwap", 0.0),
    }


def fmt_5s_cdv(sym: str, bar: dict, vwap_day: float = 0.0) -> dict:
    return {
        "table":    "cdv_5s",
        "ts":       bar["ts"],
        "symbol":   sym,
        "open":     bar["open"],
        "high":     bar["high"],
        "low":      bar["low"],
        "close":    bar["close"],
        "delta":    bar["net"],
        "vwap_day": vwap_day,
    }


# ── Schema 建立 ───────────────────────────────────────────────────────────────

def _ensure_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bars_1s (
            ts      BIGINT,
            symbol  VARCHAR,
            open    DOUBLE,
            high    DOUBLE,
            low     DOUBLE,
            close   DOUBLE,
            volume  DOUBLE,
            vwap    DOUBLE,
            PRIMARY KEY (ts, symbol)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cdv_5s (
            ts        BIGINT,
            symbol    VARCHAR,
            open      DOUBLE,
            high      DOUBLE,
            low       DOUBLE,
            close     DOUBLE,
            delta     DOUBLE,
            vwap_day  DOUBLE,
            PRIMARY KEY (ts, symbol)
        )
    """)


# ── 同步批次寫入（在 executor thread 執行）──────────────────────────────────

def _sync_flush(rows_1s: list[dict], rows_cdv: list[dict], db_path: pathlib.Path) -> int:
    import duckdb
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    written = 0
    if rows_1s:
        conn.executemany(
            "INSERT OR REPLACE INTO bars_1s VALUES (?,?,?,?,?,?,?,?)",
            [[r["ts"], r["symbol"], r["open"], r["high"],
              r["low"], r["close"], r["volume"], r["vwap"]]
             for r in rows_1s],
        )
        written += len(rows_1s)

    if rows_cdv:
        conn.executemany(
            "INSERT OR REPLACE INTO cdv_5s VALUES (?,?,?,?,?,?,?,?)",
            [[r["ts"], r["symbol"], r["open"], r["high"],
              r["low"], r["close"], r["delta"], r["vwap_day"]]
             for r in rows_cdv],
        )
        written += len(rows_cdv)

    conn.close()
    return written


# ── 非同步 writer task ────────────────────────────────────────────────────────

async def db_writer_task(
    queue: asyncio.Queue,
    db_path: str | pathlib.Path = _DB_PATH,
) -> None:
    """消費 queue，每 0.5s 批次寫入 DuckDB。"""
    path = pathlib.Path(db_path)
    loop = asyncio.get_event_loop()

    while True:
        await asyncio.sleep(0.5)

        rows_1s: list[dict] = []
        rows_cdv: list[dict] = []
        while True:
            try:
                r = queue.get_nowait()
                if r["table"] == "bars_1s":
                    rows_1s.append(r)
                else:
                    rows_cdv.append(r)
            except asyncio.QueueEmpty:
                break

        if not rows_1s and not rows_cdv:
            continue

        try:
            n = await loop.run_in_executor(
                None, _sync_flush, rows_1s, rows_cdv, path
            )
            log.debug("DuckDB 寫入 %d 筆（1s:%d cdv:%d）", n, len(rows_1s), len(rows_cdv))
        except Exception as exc:
            log.warning("DuckDB 寫入失敗：%s", exc)
