# 專案概覽：IBKR Bloomberg-style Terminal

**檔案位置**：`/Users/hsieh/Desktop/ibkr_gateway/terminal.py`  
**啟動方式**：`mnq` / `qqq` / `ibkr`（~/.zshrc alias）

---

## 整體架構

```
terminal.py
├── Process A：主程式（asyncio event loop）
│   ├── ib_insync 連線 IBKR Gateway
│   ├── 訂閱 Tick 資料（reqMktData）
│   ├── 訂閱歷史+即時 Bar（reqHistoricalData + keepUpToDate）
│   ├── 資料處理（_ingest_bar / make_tick_cb）
│   └── Rich terminal UI（Live 刷新）
│
└── Process B：圖表子程序（multiprocessing.Process）
    └── _delta_chart_proc → TradingView Lightweight Charts 視窗
        ├── 上方 Chart：CDV 30s K 棒
        └── 下方 Subchart（sync）：Price + VWAP 30s K 棒
```

---

## 資料流

```
IBKR Gateway (port 4001)
    │
    ├── reqMktData → make_tick_cb()
    │   └── 更新 QuoteState：bid/ask/last/volume/high/low
    │       └── 寫入 _tick_log（Order Flow 面板）
    │
    └── reqHistoricalData(30 secs, 28800 S, keepUpToDate=True)
        └── make_bar_cb() → _ingest_bar()
            ├── 每根 30s bar 計算 delta = vol×(close-open)/(high-low)
            ├── 累積 cum_delta，寫入 delta_bars（最多 200 根）
            ├── 累積 VWAP = Σ(典型價×量) / Σ(量)
            └── 寫入 price_bars_30s（含 vwap 欄位）
                    │
                    ▼（每 0.25s 推送）
            multiprocessing.Queue（maxsize=20）
                    │
                    ▼（chart process _loop，每 1s 消費）
            chart.run_script() → JavaScript setData/update
```

---

## 模組說明

### `QuoteState`（dataclass）

每個標的一個實例，記錄所有狀態：

| 欄位 | 說明 |
|------|------|
| `bid/ask/last/volume/...` | 即時報價 |
| `bars` | 最近 12 根 30s bar（terminal UI 用）|
| `cum_delta` | 日內累積 delta |
| `delta_bars` | 已完成的 30s delta K（最多 200 根）|
| `_d_30s/_d_open/_d_high/_d_low` | 當前進行中的 30s delta bar |
| `price_bars_30s` | 已完成的 30s 價格 K（含 vwap）|
| `_p_30s/_p_open/.../_p_close` | 當前進行中的 30s 價格 bar |
| `day_vwap` | 日內累積 VWAP |

---

### Terminal UI（Rich）

```
┌─────────────── HEADER ────────────────┐
│ IBKR TERMINAL  時間  UPTIME  MSGS     │
├────────────┬──────────────────────────┤
│ MNQ Panel  │  QQQ Panel               │
│ ├ 報價表   │  ├ 報價表                │
│ ├ 30s bar  │  ├ 30s bar               │
│ └ CDV表格  │  └ CDV表格               │
├────────────┴──────────────────────────┤
│        ORDER FLOW（Tick Log）          │
│ 時間 標的 BUY/SEL ×量 價格 動量       │
├───────────────────────────────────────┤
│ FOOTER：狀態 / 快捷鍵說明             │
└───────────────────────────────────────┘
```

---

### 圖表視窗（TradingView Lightweight Charts）

| 功能 | 說明 |
|------|------|
| 上方 CDV pane | 30s 累積 delta K 棒，綠漲紅跌，零軸基準線 |
| 下方 Price pane | 30s 價格 K 棒 + 橘色 VWAP 線，與上方時間軸同步 |
| 時間軸同步 | `sync=True` subchart，縮放/捲動聯動 |
| `→ Live` 按鈕 | `scrollToRealTime()`，跳到最新 bar |
| `⊞ Fit` 按鈕 | `fitContent()`，顯示全部歷史 |
| 預設對齊 | 啟動時自動 `scrollToRealTime()` |

---

### IBKR API 設定

| 項目 | 值 |
|------|----|
| Port | 4001（Gateway） |
| MNQ client-id | 1 |
| QQQ client-id | 2 |
| Both client-id | 3 |
| Bar size | `30 secs` |
| Duration | `28800 S`（8小時，涵蓋完整交易時段）|
| MNQ whatToShow | `TRADES` |
| QQQ data-type | `3`（Delayed，無需付費訂閱）|

---

### Delta 計算邏輯

```
delta = volume × (close - open) / (high - low)
```

- `close > open` → 淨買壓（正 delta）
- `close < open` → 淨賣壓（負 delta）
- 每根 30s bar 的 delta 累加 → `cum_delta`

---

### VWAP 計算

```
典型價 (typical_price) = (high + low + close) / 3
VWAP = Σ(典型價 × 量) / Σ(量)  ← 日內所有 bar 累積
```

---

## 目前限制與可擴展方向

| 現況 | 可擴展 |
|------|--------|
| 30s bar（28800 S = 8小時）| 多段請求串接更長歷史 |
| 單一視窗 CDV + Price/VWAP | 可加 Volume Profile、DOM |
| Delta 用 close-open 估算 | 改用 Tick-by-Tick 精確計算（需訂閱）|
| 2 標的（MNQ + QQQ）| 可擴充任意標的 |
