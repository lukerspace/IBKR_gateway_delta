"""IBKR Gateway 主程式 — QQQ tick + MNQ 5s bar 即時訂閱。

使用方式:
    python main.py              # tick + bar 同時訂閱
    python main.py --tick-only  # 只訂閱 tick
    python main.py --bar-only   # 只訂閱 5s bar
    python main.py --port 4001  # 真實帳戶
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client    import connect, disconnect
from contracts import mnq_front, qqq
from quotes    import cancel_bar, cancel_tick, subscribe_bar, subscribe_tick


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="IBKR Gateway 即時報價")
    p.add_argument("--host",      default="127.0.0.1")
    p.add_argument("--port",      type=int, default=4002, help="4002=模擬 / 4001=真實")
    p.add_argument("--client-id", type=int, default=1)
    p.add_argument("--tick-only", action="store_true")
    p.add_argument("--bar-only",  action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    ib = connect(args.host, args.port, args.client_id)

    # ── 建立合約 ──────────────────────────────────────────────────────────────
    qqq_c = qqq()
    mnq_c = mnq_front(ib)          # 自動取 MNQ 近月合約

    ib.qualifyContracts(qqq_c)     # 驗證 QQQ 合約細節

    tickers = []
    bar_lists = []

    # ── 訂閱 ─────────────────────────────────────────────────────────────────
    if not args.bar_only:
        tickers.append(subscribe_tick(ib, qqq_c))
        tickers.append(subscribe_tick(ib, mnq_c))

    if not args.tick_only:
        bar_lists.append(subscribe_bar(ib, qqq_c))
        bar_lists.append(subscribe_bar(ib, mnq_c))

    print("\n⚡ 開始接收報價，按 Ctrl+C 結束\n")

    try:
        ib.run()                   # 事件迴圈，持續接收推播
    except KeyboardInterrupt:
        print("\n🛑 使用者中斷")
    finally:
        for t in tickers:
            cancel_tick(ib, t)
        for b in bar_lists:
            cancel_bar(ib, b)
        disconnect()


if __name__ == "__main__":
    main()
