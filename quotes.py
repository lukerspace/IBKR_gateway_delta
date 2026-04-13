"""即時報價訂閱：QQQ + MNQ。

支援兩種模式：
  - tick   : 逐筆 bid/ask/last（reqMktData）
  - bar    : 每 5 秒 OHLCV（reqRealTimeBars）
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from ib_insync import IB, BarData, Contract, RealTimeBar, Ticker


# ── Tick 訂閱 ──────────────────────────────────────────────────────────────────

def subscribe_tick(
    ib: IB,
    contract: Contract,
    on_tick: Callable[[Ticker], None] | None = None,
    generic_ticks: str = "",
) -> Ticker:
    """訂閱逐筆報價（bid / ask / last / volume）。

    Args:
        ib:            已連線的 IB 物件
        contract:      合約（Stock 或 Future）
        on_tick:       每次更新時呼叫的 callback，預設印出報價
        generic_ticks: 額外 tick 類型（期貨建議 "233"）

    Returns:
        Ticker 物件（持續更新）
    """
    ticker = ib.reqMktData(contract, generic_ticks, False, False)

    def _default_on_tick(t: Ticker):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        sym = t.contract.localSymbol or t.contract.symbol
        bid = f"{t.bid:.2f}" if t.bid and t.bid > 0 else "—"
        ask = f"{t.ask:.2f}" if t.ask and t.ask > 0 else "—"
        last = f"{t.last:.2f}" if t.last and t.last > 0 else "—"
        print(f"[{ts}] {sym:>10}  bid={bid}  ask={ask}  last={last}  vol={t.volume}")

    cb = on_tick or _default_on_tick
    ticker.updateEvent += cb
    print(f"📡 已訂閱 tick: {contract.symbol}")
    return ticker


# ── Real-time Bar 訂閱 ──────────────────────────────────────────────────────────

def subscribe_bar(
    ib: IB,
    contract: Contract,
    on_bar: Callable[[RealTimeBar, bool], None] | None = None,
) -> object:
    """訂閱 5 秒 real-time bar（TRADES）。

    Args:
        ib:       已連線的 IB 物件
        contract: 合約（Stock 或 Future）
        on_bar:   每條 bar 呼叫的 callback

    Returns:
        RealTimeBarList 物件
    """
    bars = ib.reqRealTimeBars(contract, 5, "TRADES", False)

    def _default_on_bar(bars_: object, has_new: bool):
        if not has_new:
            return
        bar: RealTimeBar = bars_[-1]  # type: ignore
        ts = datetime.fromtimestamp(bar.time).strftime("%H:%M:%S")
        sym = contract.localSymbol or contract.symbol
        print(
            f"[{ts}] {sym:>10}  O={bar.open:.2f}  H={bar.high:.2f}"
            f"  L={bar.low:.2f}  C={bar.close:.2f}  vol={bar.volume}"
        )

    cb = on_bar or _default_on_bar
    bars.updateEvent += cb
    print(f"📊 已訂閱 5s bar: {contract.symbol}")
    return bars


# ── 取消訂閱 ───────────────────────────────────────────────────────────────────

def cancel_tick(ib: IB, ticker: Ticker) -> None:
    ib.cancelMktData(ticker.contract)
    print(f"🚫 已取消 tick: {ticker.contract.symbol}")


def cancel_bar(ib: IB, bars: object) -> None:
    ib.cancelRealTimeBars(bars)  # type: ignore
    print("🚫 已取消 bar 訂閱")
