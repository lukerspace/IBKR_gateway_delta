"""每日 Parquet 匯出：DuckDB → data/ 目錄。

使用方式：
    python -m storage.export                        # 匯出今日
    python -m storage.export --date 2025-04-14
    python -m storage.export --symbols MNQ
    python -m storage.export --query               # 查詢最新 10 筆（驗證用）
"""
from __future__ import annotations

import argparse
import logging
import pathlib
from datetime import date

log = logging.getLogger("ibkr.export")

_DB_PATH = pathlib.Path("data/market.duckdb")


def export_day(
    table: str,
    symbol: str,
    export_date: str | date,
    out_dir: str = "data",
    db_path: pathlib.Path = _DB_PATH,
) -> pathlib.Path | None:
    """匯出單日資料為 Parquet，回傳輸出路徑。"""
    import duckdb

    d = str(export_date)
    out = (
        pathlib.Path(out_dir)
        / table
        / f"symbol={symbol}"
        / f"year={d[:4]}"
        / f"{d}.parquet"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path), read_only=True)
    # ts 欄位是 Unix 秒，轉成 DATE 比對
    sql = f"""
        COPY (
            SELECT * FROM {table}
            WHERE symbol = '{symbol}'
              AND epoch_ms(ts * 1000)::DATE = '{d}'::DATE
            ORDER BY ts
        ) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """
    try:
        conn.execute(sql)
        n = conn.execute(
            f"SELECT count(*) FROM '{out}'"
        ).fetchone()[0]
        conn.close()
        if n == 0:
            out.unlink(missing_ok=True)
            log.warning("%s/%s/%s：無資料", table, symbol, d)
            return None
        print(f"✅  {table}/{symbol}/{d}  {n:,} 筆 → {out}")
        return out
    except Exception as exc:
        conn.close()
        log.error("匯出失敗 %s/%s/%s: %s", table, symbol, d, exc)
        return None


def export_all(
    symbols: list[str],
    export_date: str | date,
    out_dir: str = "data",
    db_path: pathlib.Path = _DB_PATH,
) -> None:
    for sym in symbols:
        for table in ("bars_1s", "cdv_5s"):
            export_day(table, sym, export_date, out_dir, db_path)


def query_latest(table: str, n: int = 10, db_path: pathlib.Path = _DB_PATH) -> None:
    """印出最新 n 筆資料（快速驗證用）。"""
    import duckdb
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY ts DESC LIMIT {n}"
        ).fetchall()
        cols = [d[0] for d in conn.description]
        print(f"\n── {table} 最新 {n} 筆 ──")
        print("  ".join(f"{c:>12}" for c in cols))
        for row in rows:
            print("  ".join(f"{str(v):>12}" for v in row))
    except Exception as exc:
        print(f"查詢失敗：{exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="DuckDB → Parquet 匯出 / 驗證查詢")
    p.add_argument("--date",    default=str(date.today()))
    p.add_argument("--symbols", default="MNQ,QQQ")
    p.add_argument("--out-dir", default="data")
    p.add_argument("--db",      default=str(_DB_PATH))
    p.add_argument("--query",   action="store_true", help="只查詢，不匯出")
    args = p.parse_args()

    if args.query:
        for tbl in ("bars_1s", "cdv_5s"):
            query_latest(tbl, db_path=pathlib.Path(args.db))
    else:
        export_all(
            symbols=[s.strip() for s in args.symbols.split(",")],
            export_date=args.date,
            out_dir=args.out_dir,
            db_path=pathlib.Path(args.db),
        )
