"""每日 Parquet 匯出：QuestDB → 本地 data/ 目錄。

使用方式：
    python -m storage.export                        # 匯出今日
    python -m storage.export --date 2025-04-14      # 指定日期
    python -m storage.export --symbols MNQ          # 只匯出 MNQ
"""
from __future__ import annotations

import argparse
import io
import logging
import pathlib
from datetime import date

log = logging.getLogger("ibkr.export")


def export_day(
    table: str,
    symbol: str,
    export_date: str | date,
    out_dir: str = "data",
    questdb_url: str = "http://localhost:9000",
) -> pathlib.Path:
    """從 QuestDB 匯出單日資料為 Parquet。

    回傳寫出的 Path。
    """
    try:
        import pyarrow.csv as pacsv
        import pyarrow.parquet as pq
        import requests
    except ImportError as e:
        raise RuntimeError(f"缺少依賴套件：{e}\npip install pyarrow requests") from e

    d = str(export_date)
    query = (
        f"SELECT * FROM {table} "
        f"WHERE symbol='{symbol}' AND ts::date = '{d}' "
        f"ORDER BY ts"
    )
    resp = requests.get(f"{questdb_url}/exp", params={"query": query}, timeout=30)
    resp.raise_for_status()

    if not resp.content.strip():
        log.warning("%s / %s / %s：無資料", table, symbol, d)
        return pathlib.Path(out_dir)

    tbl = pacsv.read_csv(io.BytesIO(resp.content))
    year = d[:4]
    out = (
        pathlib.Path(out_dir)
        / table
        / f"symbol={symbol}"
        / f"year={year}"
        / f"{d}.parquet"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, str(out), compression="zstd", use_dictionary=True)
    log.info("匯出 %d 筆 → %s", len(tbl), out)
    print(f"✅  {table}/{symbol}/{d}  {len(tbl):,} 筆 → {out}")
    return out


def export_all(
    symbols: list[str],
    export_date: str | date,
    out_dir: str = "data",
    questdb_url: str = "http://localhost:9000",
) -> None:
    for sym in symbols:
        for table in ("bars_1s", "cdv_5s"):
            try:
                export_day(table, sym, export_date, out_dir, questdb_url)
            except Exception as exc:
                log.error("匯出失敗 %s/%s/%s: %s", table, sym, export_date, exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="每日 Parquet 匯出")
    p.add_argument("--date",    default=str(date.today()), help="YYYY-MM-DD")
    p.add_argument("--symbols", default="MNQ,QQQ",         help="逗號分隔")
    p.add_argument("--out-dir", default="data",            help="輸出根目錄")
    p.add_argument("--db-url",  default="http://localhost:9000")
    args = p.parse_args()

    export_all(
        symbols=[s.strip() for s in args.symbols.split(",")],
        export_date=args.date,
        out_dir=args.out_dir,
        questdb_url=args.db_url,
    )
