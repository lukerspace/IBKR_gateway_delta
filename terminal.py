"""
IBKR Bloomberg-style Terminal  —  1s bar 版
執行: python terminal.py
     python terminal.py --data-type 1   # Live 帳戶（1s bars 需要 Live 訂閱）
     python terminal.py --data-type 3   # Delayed（最小 bar size 可能受限）

修正清單：
  [CRIT] daemon=False → daemon=True（子程序隨父程序結束）
  [CRIT] 子程序 thread 加 stop Event，能乾淨退出
  [CRIT] 時間戳改存 Unix ts int，徹底消除跨日 date 問題
  [CRIT] except Exception: pass → 改為 log.warning 記錄
  [HIGH] 資源清理：cancelMktData/cancelHistoricalData 各自獨立 try
  [HIGH] Queue 已滿改用 log.debug，不再靜默丟棄
  [NEW]  1s bar 儲存：QuoteState.bars_1s（maxlen=3600，1小時）
  [NEW]  reqHistoricalData barSizeSetting="5 secs"  durationStr="14400 S"（穩定支援 keepUpToDate）
  [NEW]  圖表下方改為 Price 1s + VWAP（原為 Price 30s）
  [FIX]  VWAP 先於 bar append 計算，確保每根 1s bar 帶正確 VWAP
  [FIX]  30s bar 帶 ts（Unix），子程序 _unix() 直接讀 ts 欄位
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import multiprocessing as mp
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Deque

from ib_insync import IB, Contract, Future, Stock, util

# 修補 asyncio，確保 ib_insync callback 在同一 event loop 執行（單執行緒，無需鎖）
util.patchAsyncio()

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ibkr_terminal")

# ── 主色調 ────────────────────────────────────────────────────────────────────
C_TITLE   = "bold orange1"
C_UP      = "bold bright_cyan"
C_DOWN    = "bold red"
C_FLAT    = "bold white"
C_DIM     = "grey42"
C_LABEL   = "dark_orange"
C_BORDER  = "orange3"
C_ACCENT  = "bright_yellow"
C_HEADER  = "bold orange1 on grey3"

OF_BUY        = "#00C853"
OF_SELL       = "#D50000"
OF_BUY_BOLD   = "bold #00C853"
OF_SELL_BOLD  = "bold #D50000"
OF_BUY_DIM    = "#1B5E20"
OF_SELL_DIM   = "#7f0000"
OF_BG_BID     = "on #0D1F0D"
OF_BG_ASK     = "on #1F0D0D"
OF_MOVE_UP    = "bold #00C853"
OF_MOVE_DOWN  = "bold #D50000"
OF_MOVE_FLAT  = "grey42"


# ── 資料容器 ──────────────────────────────────────────────────────────────────

@dataclass
class QuoteState:
    symbol:       str
    bid:          float = 0.0
    ask:          float = 0.0
    last:         float = 0.0
    last_size:    float = 0.0
    bid_size:     float = 0.0
    ask_size:     float = 0.0
    volume:       float = 0.0
    high:         float = 0.0
    low:          float = 0.0
    open_:        float = 0.0
    prev_close:   float = 0.0
    vwap:         float = 0.0
    updated:      str   = "—"

    # ── 1s bars（最多 3600 根 = 1 小時）────────────────────────────────────────
    # 每根 bar dict：{time, ts, open, high, low, close, vol, vwap}
    bars_1s:      Deque[dict] = field(default_factory=lambda: deque(maxlen=3600))

    # ── 累積 delta（每 5s flush 一根）────────────────────────────────────────
    cum_delta:    float = 0.0
    delta_bars:   Deque[dict] = field(default_factory=lambda: deque(maxlen=20))

    # ── VWAP 累積（日內，供 Terminal UI 顯示）──────────────────────────────────
    _vwap_cum_pv: float = 0.0
    _vwap_cum_v:  float = 0.0
    day_vwap:     float = 0.0

    @property
    def change(self) -> float:
        ref = self.prev_close or self.open_ or 0.0
        return (self.last - ref) if ref else 0.0

    @property
    def change_pct(self) -> float:
        ref = self.prev_close or self.open_ or 0.0
        return (self.change / ref * 100) if ref else 0.0

    def color(self) -> str:
        if self.change > 0: return C_UP
        if self.change < 0: return C_DOWN
        return C_FLAT


# ── 全域狀態（僅 asyncio event loop 存取，單執行緒，無需 Lock）───────────────
_states:    dict[str, QuoteState] = {}
_tick_log:  Deque[dict]           = deque(maxlen=200)
_msg_count: int = 0
_start_time = datetime.now()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _v(val) -> bool:
    """有效正數值。"""
    try:
        return val is not None and not math.isnan(float(val)) and float(val) > 0
    except Exception:
        return False

def _fmt(val: float, d: int = 2) -> str:
    return f"{val:,.{d}f}" if val else "—"

def _fmt_vol(vol: float) -> str:
    if vol >= 1_000_000: return f"{vol/1_000_000:.1f}M"
    if vol >= 1_000:     return f"{vol/1_000:.1f}K"
    return str(int(vol)) if vol else "—"

def _trade_side(last: float, bid: float, ask: float) -> str:
    if not (_v(last) and _v(bid) and _v(ask)):
        return "   "
    mid = (bid + ask) / 2
    if last >= ask: return "BUY"
    if last <= bid: return "SEL"
    return "BUY" if last >= mid else "SEL"

def _bar_ts(bar) -> tuple[str, int]:
    """從 ib_insync bar 物件提取 (HH:MM:SS 字串, Unix timestamp int)。"""
    if hasattr(bar, "date"):
        t = bar.date
        if hasattr(t, "timestamp"):
            ts = int(t.timestamp())
        else:
            ts = int(datetime.fromisoformat(str(t)).timestamp())
    else:
        ts = int(bar.time)
    ts_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    return ts_str, ts


# ── Callbacks ─────────────────────────────────────────────────────────────────

def make_tick_cb(sym: str):
    """Tick callback + 1s bar 聚合。

    每筆 last 成交觸發秒級 OHLCV 累積；秒邊界時 flush 前一根 1s bar。
    1s bars 由 tick 流直接構建，不依賴 reqHistoricalData 的 1 secs 設定。
    """
    # 1s bar 累積狀態（closure，每個 sym 獨立）
    _1s: dict = {}   # keys: sec, ts, o, h, l, c, vol

    def _flush_1s_bar() -> None:
        """把目前累積的 1s bar 寫入 bars_1s，並帶入最新 day_vwap。"""
        if not _1s or not _1s.get("c"):
            return
        s = _states.get(sym)
        vwap = s.day_vwap if s else 0.0
        _states.setdefault(sym, QuoteState(symbol=sym)).bars_1s.append({
            "time": _1s["sec"],
            "ts":   _1s["ts"],
            "open": _1s["o"],
            "high": _1s["h"],
            "low":  _1s["l"],
            "close": _1s["c"],
            "vol":  _1s["vol"],
            "vwap": vwap,
        })

    def cb(ticker):
        global _msg_count
        s = _states.setdefault(sym, QuoteState(symbol=sym))
        if _v(ticker.bid):      s.bid        = ticker.bid
        if _v(ticker.ask):      s.ask        = ticker.ask
        if _v(ticker.bidSize):  s.bid_size   = float(ticker.bidSize)
        if _v(ticker.askSize):  s.ask_size   = float(ticker.askSize)
        if _v(ticker.volume):   s.volume     = float(ticker.volume)
        if _v(ticker.high):     s.high       = ticker.high
        if _v(ticker.low):      s.low        = ticker.low
        if _v(ticker.open):     s.open_      = ticker.open
        if _v(ticker.close):    s.prev_close = ticker.close
        if _v(ticker.vwap):     s.vwap       = ticker.vwap
        s.updated = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        _msg_count += 1

        if _v(ticker.last) and ticker.last != s.last:
            prev  = s.last
            s.last = ticker.last
            if _v(ticker.lastSize): s.last_size = float(ticker.lastSize)
            price_move = s.last - prev if prev > 0 else 0.0
            _tick_log.append({
                "time":       s.updated,
                "sym":        sym,
                "side":       _trade_side(s.last, s.bid, s.ask),
                "last":       s.last,
                "last_size":  int(s.last_size or 0),
                "bid":        s.bid,
                "ask":        s.ask,
                "bid_size":   int(s.bid_size or 0),
                "ask_size":   int(s.ask_size or 0),
                "price_move": price_move,
                "change":     s.change,
                "change_pct": s.change_pct,
            })

            # ── 1s bar 聚合 ──────────────────────────────────────────────────
            now     = datetime.now()
            sec_str = now.strftime("%H:%M:%S")
            ts_now  = int(now.timestamp())
            size    = s.last_size or 0.0

            if _1s.get("sec") != sec_str:
                _flush_1s_bar()                     # 關閉前一秒
                _1s.clear()
                _1s.update({"sec": sec_str, "ts": ts_now,
                             "o": s.last, "h": s.last,
                             "l": s.last, "c": s.last, "vol": size})
            else:
                if s.last > _1s["h"]: _1s["h"] = s.last
                if s.last < _1s["l"]: _1s["l"] = s.last
                _1s["c"]   = s.last
                _1s["vol"] = _1s.get("vol", 0.0) + size

        elif _v(ticker.last):
            s.last = ticker.last
        if _v(ticker.lastSize): s.last_size = float(ticker.lastSize)
    return cb


def _ingest_bar(sym: str, b: dict) -> None:
    """處理 bar 資料（5s，來自 reqHistoricalData keepUpToDate）。

    負責：
      1. 累積日內 VWAP（精確體積加權，供 bars_1s 的 tick 聚合參考）
      2. 聚合 30s delta bar（CDV chart）
      3. 聚合 30s price bar（price_bars_30s，CDV chart 時間軸對齊）

    注意：bars_1s 由 make_tick_cb 的 1s 聚合邏輯填寫，此函式不寫入。
    """
    s = _states.setdefault(sym, QuoteState(symbol=sym))

    # VWAP 日內累積（用 bar 的精確 volume，比 tick lastSize 更可靠）
    typ = (b["high"] + b["low"] + b["close"]) / 3.0
    s._vwap_cum_pv += typ * b["vol"]
    s._vwap_cum_v  += b["vol"]
    s.day_vwap = s._vwap_cum_pv / s._vwap_cum_v if s._vwap_cum_v > 0 else 0.0

    # ── 累積 delta，每根 5s bar 直接 flush ───────────────────────────────────
    rng      = b["high"] - b["low"]
    delta    = b["vol"] * (b["close"] - b["open"]) / rng if rng > 0 else 0.0
    prev_cum = s.cum_delta
    s.cum_delta += delta

    s.delta_bars.append({
        "time":  b["time"],
        "ts":    b.get("ts", 0),
        "open":  prev_cum,
        "high":  max(prev_cum, s.cum_delta),
        "low":   min(prev_cum, s.cum_delta),
        "close": s.cum_delta,
        "net":   delta,
    })



def make_bar_cb(sym: str):
    def cb(bars_, has_new: bool):
        global _msg_count
        if not has_new:
            return
        bar = bars_[-1]
        ts_str, ts = _bar_ts(bar)
        _ingest_bar(sym, {
            "time": ts_str, "ts": ts,
            "open": bar.open, "high": bar.high,
            "low":  bar.low, "close": bar.close, "vol": bar.volume,
        })
        _msg_count += 1
    return cb


# ── UI 渲染 ───────────────────────────────────────────────────────────────────

def _has_data(sym: str) -> bool:
    s = _states.get(sym)
    return bool(s and s.last > 0)


def _render_header() -> Panel:
    now    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    uptime = str(datetime.now() - _start_time).split(".")[0]
    t = Text()
    t.append("  ▐█ IBKR TERMINAL █▌  ", style=C_HEADER)
    t.append(f"  {now}  ",              style=C_ACCENT)
    t.append("│",                       style=C_DIM)
    t.append(f"  UPTIME {uptime}  ",    style=C_DIM)
    t.append("│",                       style=C_DIM)
    t.append(f"  MSGS {_msg_count:,}  ", style=C_LABEL)
    return Panel(t, style=C_BORDER, padding=(0, 1), box=box.HEAVY)


def _render_quote(sym: str) -> Panel:
    s = _states.get(sym)
    if not s or not s.last:
        return Panel(
            Text("⏳ 等待資料...", style=C_DIM, justify="center"),
            title=f"[{C_TITLE}] {sym} [/{C_TITLE}]",
            style=C_BORDER, box=box.DOUBLE,
        )

    col  = s.color()
    sign = "▲" if s.change > 0 else ("▼" if s.change < 0 else "━")

    # ── 報價表（對齊截圖：LAST/CHG, BID/HIGH, ASK/LOW, VWAP/VOL, OPEN/UPD）──
    tbl = Table(box=None, padding=(0, 2), show_header=False, expand=True)
    tbl.add_column(justify="right", style=C_LABEL,     width=6)
    tbl.add_column(justify="left",  style="bold white", width=16)
    tbl.add_column(justify="right", style=C_LABEL,     width=6)
    tbl.add_column(justify="left",  style="bold white", width=16)

    tbl.add_row(
        "LAST", Text(_fmt(s.last), style=f"bold {col}"),
        "CHG",  Text(f"{sign} {abs(s.change):.2f} ({s.change_pct:+.2f}%)", style=col),
    )
    tbl.add_row(
        "BID",  Text(f"{_fmt(s.bid)} ×{int(s.bid_size)}", style=C_UP),
        "HIGH", Text(_fmt(s.high), style=C_UP),
    )
    tbl.add_row(
        "ASK",  Text(f"{_fmt(s.ask)} ×{int(s.ask_size)}", style=C_DOWN),
        "LOW",  Text(_fmt(s.low), style=C_DOWN),
    )
    tbl.add_row(
        "PREV", Text(_fmt(s.prev_close), style="bold white"),
        "VOL",  Text(_fmt_vol(s.volume), style="bold white"),
    )
    tbl.add_row(
        "VWAP", Text(_fmt(s.day_vwap or s.vwap), style=C_ACCENT),
        "UPD",  Text(s.updated, style=C_DIM),
    )

    # 最近 15 根 1s K
    recent = list(s.bars_1s)[-15:]
    bar_tbl = Table(
        "TIME", "O", "H", "L", "C", "VOL", "DELTA",
        box=box.SIMPLE_HEAD, style=C_DIM, header_style=C_LABEL,
        show_edge=False, padding=(0, 1), expand=True,
    )
    for b in reversed(recent):
        c_style = C_UP if b["close"] >= b["open"] else C_DOWN
        rng = b["high"] - b["low"]
        delta = b["vol"] * (b["close"] - b["open"]) / rng if rng > 0 else 0.0
        d_str   = f"{'+' if delta >= 0 else ''}{_fmt_vol(abs(delta))}"
        d_style = C_UP if delta > 0 else (C_DOWN if delta < 0 else C_FLAT)
        bar_tbl.add_row(
            b["time"],
            _fmt(b["open"]), _fmt(b["high"]), _fmt(b["low"]),
            Text(_fmt(b["close"]), style=c_style),
            _fmt_vol(b["vol"]),
            Text(d_str, style=d_style),
        )

    content = Group(
        tbl,
        Text("─" * 58, style=C_DIM),
        Text(" 1s BARS", style=C_LABEL),
        bar_tbl,
        _render_delta_chart(sym),
    )
    return Panel(content,
                 title=f"[{C_TITLE}]  {sym}  [/{C_TITLE}]",
                 subtitle=f"[{C_DIM}]{s.updated}[/{C_DIM}]",
                 style=C_BORDER, box=box.DOUBLE, padding=(0, 1))


def _vol_style(side: str, size: int) -> str:
    base = OF_BUY if side == "BUY" else OF_SELL
    if size > 10: return f"bold {base}"
    if size >= 5: return base
    return OF_BUY_DIM if side == "BUY" else OF_SELL_DIM

def _bg_style(bid_sz: int, ask_sz: int) -> str:
    if ask_sz > 0 and bid_sz > ask_sz * 1.5: return OF_BG_BID
    if bid_sz > 0 and ask_sz > bid_sz * 1.5: return OF_BG_ASK
    return ""


def _render_ticklog(symbols: list[str]) -> Panel:
    sym_label = " + ".join(symbols)
    entries   = [e for e in _tick_log if e["sym"] in symbols][-35:]
    text = Text()

    for e in entries:
        side     = e["side"]
        size     = e["last_size"]
        bg       = _bg_style(e["bid_size"], e["ask_size"])
        side_col = OF_BUY_BOLD if side == "BUY" else (OF_SELL_BOLD if side == "SEL" else C_DIM)
        vol_col  = _vol_style(side, size)
        vol_pfx  = "●" if size > 10 else "·"
        mv = e["price_move"]
        if mv > 0:   move_sym, move_col = "▲", OF_MOVE_UP
        elif mv < 0: move_sym, move_col = "▼", OF_MOVE_DOWN
        else:        move_sym, move_col = "—", OF_MOVE_FLAT
        chg_col = OF_BUY if e["change"] > 0 else (OF_SELL if e["change"] < 0 else OF_MOVE_FLAT)
        b_col = OF_BUY_DIM if e["bid_size"] >= e["ask_size"] else C_DIM
        a_col = OF_SELL_DIM if e["ask_size"] > e["bid_size"] else C_DIM
        base_bg = f" {bg}" if bg else ""

        text.append(f"[{e['time']}] ",              style=f"grey42{base_bg}")
        text.append(f"{e['sym']:<5} ",               style=f"bold orange1{base_bg}")
        text.append(f"{side:<3} ",                   style=f"{side_col}{base_bg}")
        text.append(f"{vol_pfx}×{size:<4}",          style=f"{vol_col}{base_bg}")
        text.append(f"L:{_fmt(e['last'])}  ",        style=f"bold white{base_bg}")
        text.append(f"{move_sym}{abs(mv):.2f}  ",    style=f"{move_col}{base_bg}")
        text.append(f"B:{_fmt(e['bid'])}×{e['bid_size']:<3}",  style=f"{b_col}{base_bg}")
        text.append(f"  A:{_fmt(e['ask'])}×{e['ask_size']:<3}", style=f"{a_col}{base_bg}")
        text.append(f"  {e['change_pct']:+.2f}%\n",  style=f"{chg_col}{base_bg}")

    return Panel(text,
                 title=f"[{C_TITLE}]  ORDER FLOW  [{C_DIM}]{sym_label}[/{C_DIM}]  [/{C_TITLE}]",
                 style=C_BORDER, box=box.DOUBLE, padding=(0, 1))


def _render_delta_chart(sym: str) -> Group:
    s = _states.get(sym)
    if not s or not s.delta_bars:
        return Group(
            Text("─" * 58, style=C_DIM),
            Text("  CUMULATIVE DELTA  ⏳ accumulating...", style=C_DIM),
        )

    bars    = list(s.delta_bars)
    all_nets = [b["net"] for b in bars]
    max_abs  = max((abs(n) for n in all_nets), default=1.0) or 1.0
    BAR_W    = 14

    def _spark(net: float) -> Text:
        width = max(1, int(abs(net) / max_abs * BAR_W))
        col   = OF_BUY if net >= 0 else OF_SELL
        t = Text()
        t.append(f"{'+' if net >= 0 else ''}{net:>7.1f}  ", style=col)
        t.append("█" * width, style=f"bold {col}")
        return t

    tbl = Table(
        "TIME", "OPEN", "HIGH", "LOW", "CLOSE", "NET ▸",
        box=box.SIMPLE_HEAD, style=C_DIM, header_style=C_LABEL,
        show_edge=False, padding=(0, 1), expand=True,
    )
    for b in reversed(bars[-20:]):
        net   = b["net"]
        c_col = OF_BUY if net >= 0 else OF_SELL
        tbl.add_row(
            b["time"],
            f"{b['open']:+.1f}", f"{b['high']:+.1f}", f"{b['low']:+.1f}",
            Text(f"{b['close']:+.1f}", style=c_col),
            _spark(net),
        )

    total_col = OF_BUY if s.cum_delta >= 0 else OF_SELL
    hdr = Text()
    hdr.append("  CUMULATIVE DELTA  5s  ", style=C_LABEL)
    hdr.append(
        f"{'▲' if s.cum_delta >= 0 else '▼'} {s.cum_delta:+.1f}",
        style=f"bold {total_col}",
    )
    return Group(Text("─" * 58, style=C_DIM), hdr, tbl)


def _render_footer(symbols: list[str], status: str = "CONNECTED") -> Panel:
    def _mini(sym: str) -> Text:
        s = _states.get(sym)
        if not s or not s.last:
            return Text(f"  {sym}: —  ", style=C_DIM)
        sign = "▲" if s.change > 0 else "▼"
        return Text(f"  {sym}: {_fmt(s.last)} {sign}{abs(s.change_pct):.2f}%  ",
                    style=s.color())
    t = Text()
    t.append("  STATUS: ", style=C_LABEL)
    t.append(f"● {status}  ", style=C_UP)
    t.append("│", style=C_DIM)
    for sym in symbols:
        t.append_text(_mini(sym))
        t.append("│", style=C_DIM)
    t.append("  Ctrl+C 離開  ", style=C_DIM)
    return Panel(t, style=C_BORDER, padding=(0, 0), box=box.HEAVY)


# ── Chart Subprocess（TradingView Lightweight Charts）─────────────────────────

def _delta_chart_proc(sym: str, queue: object, start_time: str) -> None:
    """子程序：純 CDV 5s K 全螢幕顯示。"""
    import json as _json
    import threading
    import time
    from datetime import date, datetime as _dt

    try:
        import pandas as pd
        from lightweight_charts import Chart
    except ImportError as e:
        print(f"\n⚠️  需要安裝: pip install lightweight-charts pandas  ({e})", flush=True)
        return

    _stop = threading.Event()

    def _unix(b: dict) -> int:
        ts = b.get("ts", 0)
        if ts:
            return int(ts)
        today = date.today().isoformat()
        t = b.get("time", "00:00:00")
        s = t[:8] if len(t) >= 8 else (t[:5] + ":00" if len(t) >= 5 else "00:00:00")
        try:
            return int(pd.Timestamp(f"{today} {s}").timestamp())
        except Exception:
            return 0

    def _to_ohlc(bars: list) -> list:
        out = []
        for b in bars:
            u = _unix(b)
            if u > 0 and b.get("close") is not None:
                out.append({"time": u, "open": b["open"],
                             "high": b["high"], "low": b["low"], "close": b["close"]})
        return out

    chart = Chart(
        title=f"{sym}  Cumulative Delta Volume (5s)",
        width=1400, height=900,
        inner_width=1.0, inner_height=1.0,
        toolbox=True,
    )
    chart.layout(background_color="#0d0d0d", text_color="#cccccc", font_size=11)
    chart.candle_style(
        up_color="#00C853",    down_color="#D50000",
        border_up_color="#00FF66", border_down_color="#FF3333",
        wick_up_color="#00C853",   wick_down_color="#D50000",
    )
    chart.grid(vert_enabled=False, horz_enabled=True, color="rgba(50,60,80,0.5)")
    chart.time_scale(right_offset=12, min_bar_spacing=18)
    try:
        chart.horizontal_line(0, color="rgba(80,110,140,0.8)", width=1, style="dashed")
    except Exception:
        pass
    chart.topbar.textbox("sym",   f"  {sym}  CDV (5s)")
    chart.topbar.textbox("start", f"  ▷ Start: {start_time}")
    chart.topbar.textbox("now",   "  ◁ Now: —")
    chart.topbar.textbox("val",   "  Δ —  ")
    chart.topbar.button("btn_live", "→ Live", separator=True, align="right",
                        func=lambda c: c.run_script(
                            f"{c.id}.chart.timeScale().scrollToRealTime()"))
    chart.topbar.button("btn_fit", "⊞ Fit", separator=False, align="right",
                        func=lambda c: c.fit())

    _last_nd = 0
    _inited  = False

    def _loop() -> None:
        nonlocal _last_nd, _inited

        while not _stop.is_set():
            latest = None
            while True:
                try:
                    latest = queue.get_nowait()  # type: ignore[union-attr]
                except Exception:
                    break

            if latest:
                all_bars = latest.get("bars", [])
                nd       = len(all_bars)

                if nd > 0:
                    records = _to_ohlc(all_bars)
                    if records:
                        if not _inited or nd != _last_nd:
                            chart.run_script(
                                f"{chart.id}.series.setData({_json.dumps(records)})")
                            if not _inited:
                                chart.run_script(
                                    f"{chart.id}.chart.timeScale().scrollToRealTime()")
                            _last_nd = nd
                            _inited  = True
                        else:
                            last = all_bars[-1]
                            bar  = {"time": _unix(last), "open": last["open"],
                                    "high": last["high"], "low": last["low"],
                                    "close": last["close"]}
                            chart.run_script(
                                f"{chart.id}.series.update({_json.dumps(bar)})")

                        cur_val = all_bars[-1]["close"]
                        now_str = _dt.now().strftime("%H:%M:%S")
                        sign    = "▲" if cur_val >= 0 else "▼"
                        chart.topbar["now"].set(f"  ◁ Now: {now_str}")
                        chart.topbar["val"].set(f"  Δ {sign} {cur_val:+.1f}  ")

            time.sleep(0.5)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()

    try:
        chart.show(block=True)
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
        t.join(timeout=3)


# ── Layout ────────────────────────────────────────────────────────────────────

def _build_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="qqq", ratio=2),
        Layout(name="mnq", ratio=2),
        Layout(name="log", ratio=3),
    )
    return layout


def _update_body(layout: Layout, symbols: list[str]) -> None:
    qqq_ok = "QQQ" in symbols and _has_data("QQQ")
    mnq_ok = "MNQ" in symbols and _has_data("MNQ")
    layout["qqq"].visible = qqq_ok
    layout["mnq"].visible = mnq_ok
    if qqq_ok and mnq_ok:
        layout["qqq"].ratio = 2
        layout["mnq"].ratio = 2
    elif mnq_ok:
        layout["mnq"].ratio = 4
    elif qqq_ok:
        layout["qqq"].ratio = 4


# ── 主程式 ────────────────────────────────────────────────────────────────────

async def run_terminal(
    host: str, port: int, client_id: int, data_type: int,
    symbols: list[str],
) -> None:
    ib = IB()

    # 啟動圖表子程序（daemon=True：父程序退出時自動終止）
    chart_start  = _start_time.strftime("%H:%M:%S")
    chart_queues: dict[str, mp.Queue] = {}
    chart_procs:  list[mp.Process]    = []
    for sym in symbols:
        q: mp.Queue = mp.Queue(maxsize=5)
        chart_queues[sym] = q
        p = mp.Process(
            target=_delta_chart_proc,
            args=(sym, q, chart_start),
            daemon=False,          # lightweight_charts 內部會再 spawn WebView，
                                   # daemonic process 不能有子程序，故維持 False。
                                   # 正常退出靠 finally 的 p.terminate()+join 清理。
        )
        p.start()
        chart_procs.append(p)
    print(f"📈 CDV 5s + Price 1s/VWAP 視窗已啟動（{', '.join(symbols)}）")

    _errors: list[str] = []
    def on_error(reqId, errorCode, errorString, contract):
        if errorCode not in (2104, 2106, 2158, 2119):
            msg = f"[{errorCode}] {errorString[:80]}"
            _errors.append(msg)
            log.warning("IB API error: %s", msg)
    ib.errorEvent += on_error

    try:
        ib.connect(host, port, clientId=client_id)
    except ConnectionRefusedError:
        raise ConnectionRefusedError(f"Connect call failed ('{host}', {port})")
    ib.reqMarketDataType(data_type)

    label = {1: "Live", 2: "Frozen", 3: "Delayed", 4: "Delayed Frozen"}.get(
        data_type, str(data_type))
    print(f"✅ 已連線  帳戶={ib.managedAccounts()}  資料模式=[{data_type}] {label}")
    print(f"📡 訂閱標的: {' + '.join(symbols)}")
    print(f"📊 Bar 精度: 1s（tick 聚合，即時）+ 5s（歷史回填 4 小時，CDV/VWAP 用）")

    qqq_c = mnq_c = None
    t_qqq = t_mnq = None
    b_qqq = b_mnq = None

    if "QQQ" in symbols:
        qqq_c = Stock("QQQ", "SMART", "USD")
        ib.qualifyContracts(qqq_c)
        t_qqq = ib.reqMktData(qqq_c, "", False, False)
        t_qqq.updateEvent += make_tick_cb("QQQ")
        b_qqq = ib.reqHistoricalData(
            qqq_c, endDateTime="", durationStr="14400 S",
            barSizeSetting="5 secs", whatToShow="TRADES",
            useRTH=False, keepUpToDate=True,
        )
        b_qqq.updateEvent += make_bar_cb("QQQ")
        for bar in b_qqq:
            ts_str, ts = _bar_ts(bar)
            _ingest_bar("QQQ", {
                "time": ts_str, "ts": ts,
                "open": bar.open, "high": bar.high,
                "low":  bar.low,  "close": bar.close, "vol": bar.volume,
            })

    if "MNQ" in symbols:
        details = ib.reqContractDetails(Future("MNQ", exchange="CME", currency="USD"))
        if not details:
            details = ib.reqContractDetails(Future("MNQ", exchange="GLOBEX", currency="USD"))
        valid = sorted(
            [d for d in details
             if len(d.contract.lastTradeDateOrContractMonth or "") >= 6],
            key=lambda d: d.contract.lastTradeDateOrContractMonth,
        )
        raw_mnq = valid[0].contract
        mnq_c   = ib.qualifyContracts(
            Contract(conId=raw_mnq.conId, exchange=raw_mnq.exchange)
        )[0]
        print(f"📌 MNQ: {mnq_c.localSymbol}  conId={mnq_c.conId}"
              f"  expiry={mnq_c.lastTradeDateOrContractMonth}")
        t_mnq = ib.reqMktData(mnq_c, "233", False, False)
        t_mnq.updateEvent += make_tick_cb("MNQ")
        b_mnq = ib.reqHistoricalData(
            mnq_c, endDateTime="", durationStr="14400 S",
            barSizeSetting="5 secs", whatToShow="TRADES",
            useRTH=False, keepUpToDate=True,
        )
        b_mnq.updateEvent += make_bar_cb("MNQ")
        for bar in b_mnq:
            ts_str, ts = _bar_ts(bar)
            _ingest_bar("MNQ", {
                "time": ts_str, "ts": ts,
                "open": bar.open, "high": bar.high,
                "low":  bar.low,  "close": bar.close, "vol": bar.volume,
            })

    await asyncio.sleep(2)

    console = Console()
    layout  = _build_layout()

    with Live(layout, console=console, refresh_per_second=4, screen=True):
        try:
            while True:
                _update_body(layout, symbols)
                layout["header"].update(_render_header())
                if "QQQ" in symbols and _has_data("QQQ"):
                    layout["qqq"].update(_render_quote("QQQ"))
                if "MNQ" in symbols and _has_data("MNQ"):
                    layout["mnq"].update(_render_quote("MNQ"))
                layout["log"].update(_render_ticklog(symbols))
                layout["footer"].update(_render_footer(symbols))

                # 推送最新資料到圖表子程序
                for _sym in symbols:
                    _s = _states.get(_sym)
                    if not _s or _sym not in chart_queues:
                        continue
                    try:
                        chart_queues[_sym].put_nowait({
                            "bars":    list(_s.delta_bars),
                            "current": _s.delta_bars[-1] if _s.delta_bars else None,
                        })
                    except Exception:
                        # Queue 已滿（子程序忙）→ 靜默跳過，下一輪重試
                        log.debug("%s chart queue full, skipping push", _sym)

                await asyncio.sleep(0.25)

        except asyncio.CancelledError:
            pass

        finally:
            # [HIGH FIX] 各自獨立 try，避免一個失敗導致後續清理跳過
            if t_qqq is not None:
                try:   ib.cancelMktData(qqq_c)
                except Exception as e: log.warning("cancelMktData QQQ: %s", e)
            if t_mnq is not None:
                try:   ib.cancelMktData(mnq_c)
                except Exception as e: log.warning("cancelMktData MNQ: %s", e)
            if b_qqq is not None:
                try:   ib.cancelHistoricalData(b_qqq)
                except Exception as e: log.warning("cancelHistoricalData QQQ: %s", e)
            if b_mnq is not None:
                try:   ib.cancelHistoricalData(b_mnq)
                except Exception as e: log.warning("cancelHistoricalData MNQ: %s", e)

            ib.disconnect()

            for _p in chart_procs:
                try:
                    _p.terminate()
                    _p.join(timeout=2)
                except Exception as e:
                    log.warning("chart proc cleanup: %s", e)

            console.print("\n[bold cyan]Terminal 已關閉[/bold cyan]")
            if _errors:
                console.print("[yellow]收到以下 API 錯誤：[/yellow]")
                for e in _errors[-10:]:
                    console.print(f"  [red]{e}[/red]")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="IBKR Bloomberg-style Terminal（1s bars）"
    )
    parser.add_argument("--host",      default="127.0.0.1")
    parser.add_argument("--port",      type=int, default=4001)
    parser.add_argument("--client-id", type=int, default=1)
    parser.add_argument("--data-type", type=int, default=1,
                        help="1=Live(預設) 2=Frozen 3=Delayed 4=DelayedFrozen")
    parser.add_argument("--symbols",   default="both",
                        choices=["qqq", "mnq", "both"],
                        help="訂閱標的：qqq / mnq / both（預設 both）")
    args = parser.parse_args()

    sym_map = {"qqq": ["QQQ"], "mnq": ["MNQ"], "both": ["QQQ", "MNQ"]}
    symbols = sym_map[args.symbols]

    try:
        ib = IB()
        ib.run(run_terminal(
            args.host, args.port, args.client_id, args.data_type, symbols
        ))
    except KeyboardInterrupt:
        pass
    except ConnectionRefusedError:
        print(f"❌ 無法連線到 IB Gateway/TWS（{args.host}:{args.port}）")
        print("   請確認 IB Gateway 或 TWS 已啟動，並已開啟 API 連線。")
        raise SystemExit(1)


if __name__ == "__main__":
    mp.freeze_support()   # Windows / macOS spawn 安全
    main()
