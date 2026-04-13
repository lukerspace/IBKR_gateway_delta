"""QuestDB ILP TCP 寫入器。

非阻塞設計：
  - fmt_1s / fmt_5s_cdv  → 在 asyncio 事件迴圈中格式化成 ILP 字串
  - db_writer_task       → 協程，每 0.5s 批次 flush，斷線自動重連
"""
from __future__ import annotations

import asyncio
import logging
import socket

log = logging.getLogger("ibkr.storage")


# ── ILP 格式化 ────────────────────────────────────────────────────────────────

def fmt_1s(sym: str, bar: dict) -> str:
    """1s bar → bars_1s ILP 行。

    bar 欄位：ts（Unix 秒 int）, open, high, low, close, vol, vwap
    """
    ts_ns = bar["ts"] * 1_000_000_000
    return (
        f"bars_1s,symbol={sym} "
        f"open={bar['open']},high={bar['high']},low={bar['low']},"
        f"close={bar['close']},volume={bar['vol']},vwap={bar.get('vwap', 0.0)} "
        f"{ts_ns}"
    )


def fmt_5s_cdv(sym: str, bar: dict, vwap_day: float = 0.0) -> str:
    """5s CDV bar → cdv_5s ILP 行。

    bar 欄位：ts（Unix 秒 int）, open, high, low, close, net（= delta）
    """
    ts_ns = bar["ts"] * 1_000_000_000
    return (
        f"cdv_5s,symbol={sym} "
        f"open={bar['open']},high={bar['high']},low={bar['low']},"
        f"close={bar['close']},delta={bar['net']},vwap_day={vwap_day} "
        f"{ts_ns}"
    )


# ── 非同步寫入協程 ─────────────────────────────────────────────────────────────

async def db_writer_task(
    queue: asyncio.Queue,
    host: str = "127.0.0.1",
    port: int = 9009,
) -> None:
    """消費 queue，每 0.5s 批次寫入 QuestDB（ILP TCP）。

    斷線後下一輪自動重連；QuestDB 未啟動時靜默等待，不影響 terminal 主迴圈。
    """
    sock: socket.socket | None = None

    while True:
        await asyncio.sleep(0.5)

        # 批次取出所有待寫資料
        rows: list[str] = []
        while True:
            try:
                rows.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not rows:
            continue

        try:
            if sock is None:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3.0)
                s.connect((host, port))
                s.settimeout(None)
                sock = s
                log.info("QuestDB 已連線 %s:%s", host, port)

            payload = "\n".join(rows) + "\n"
            sock.sendall(payload.encode())
            log.debug("寫入 %d 筆", len(rows))

        except Exception as exc:
            log.warning("QuestDB 寫入失敗（%s），下次重連", exc)
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None
            # 失敗的資料不補寫（DEDUP UPSERT 保證重連後補傳冪等）
