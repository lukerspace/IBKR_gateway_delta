# 市場資料儲存系統架構設計

> 基於 `ibkr_gateway` 專案，設計可長期持久化的市場資料儲存系統  
> 儲存欄位：O、H、L、C、Volume、Delta、CDV、VWAP

---

## 一、目前資料維度（現行程式碼）

### 資料來源與時間精度

| 資料層 | 時間精度 | 來源 | 目前容量 | 備註 |
|--------|---------|------|---------|------|
| **1s bar** | 1 秒 | `reqMktData` tick callback 手動聚合 | `deque(maxlen=3600)` = 記憶體最近 1 小時 | OHLCV + VWAP |
| **5s bar（原始）** | 5 秒 | `reqHistoricalData keepUpToDate` | 不直接保存，僅用於計算 | 用於 CDV/VWAP 計算 |
| **CDV bar** | 5 秒 flush | `_ingest_bar()` 每根 5s bar 直接 append | `deque(maxlen=20)` = 記憶體最近 100 秒 | delta + cum_delta |
| **Order flow log** | ~250ms 快照觸發 | `reqMktData` last 變動 | `deque(maxlen=200)` | side / price / size |

### 現況問題

| 問題 | 說明 |
|------|------|
| 純記憶體 | 程式重啟後所有歷史資料歸零 |
| 1s bar 上限 3600 根 | 只保留最近 1 小時，無法回顧日內完整結構 |
| CDV 只保留 20 根 | 100 秒的視窗，無法分析日內 CDV 趨勢 |
| 無跨日歷史 | 每日開盤重新計算 CDV/VWAP，無法比較不同交易日 |
| 無容錯 | 斷線、當機或系統更新即失去當日資料 |

### 目標

1. **即時持久化** 1s bars 與 5s CDV 到本地資料庫（不阻塞 asyncio 事件迴圈）
2. **長期保存** 歷史資料，供回測與機器學習訓練使用
3. **可部署** 至雲端 VM，穩定 24/7 運行

---

## 二、資料量估算

| 項目 | 數值 |
|------|------|
| 1s bar：正規盤每日（6.5h） | 23,400 筆/商品/日 |
| 1s bar：延盤每日（16h） | 57,600 筆/商品/日 |
| 5s CDV bar：正規盤每日 | 4,680 筆/商品/日 |
| 2 商品 × 5 年 × 250 交易日（1s） | ~5,850 萬筆 |
| 原始大小（1s，8 欄 float64） | ~3.7 GB |
| Parquet + zstd float32 壓縮（約 6:1） | **~620 MB** |
| QuestDB 熱資料（近 90 天） | ~90 MB |

> 資料量小，完全不需要分散式系統。單一 VM + 本地 QuestDB 即可。

---

## 三、系統架構

```
IBKR Gateway (port 4001)
        │
        │  ib_insync 回調（asyncio 事件迴圈，單執行緒）
        ▼
  make_tick_cb()          make_bar_cb() → _ingest_bar()
        │                         │
        │ 秒邊界 flush             │ 每根 5s bar
        ▼                         ▼
  bars_1s deque          delta_bars deque (5s CDV)
  {ts,O,H,L,C,vol,vwap}  {ts,delta,cdv,O,H,L,C}
        │                         │
        └──────┬───────────────────┘
               │ put_nowait()（非阻塞，QueueFull 靜默丟棄）
               ▼
     asyncio.Queue(maxsize=2000)     ← 新增
               │
        _db_writer_task()            ← 新增協程（每 0.5s 批次寫入）
               │ ILP TCP :9009
               ▼
         QuestDB（本地 Docker）       ← 熱儲存，滾動 90 天
               │
       每日 17:00 ET 排程匯出
               │ PyArrow → Parquet zstd → S3 / 本地磁碟
               ▼
  bars_1s/symbol=MNQ/year=2025/2025-04-14.parquet
  cdv_5s/symbol=MNQ/year=2025/2025-04-14.parquet
```

---

## 四、技術選型

### 熱儲存：QuestDB

| 對比 | 結論 |
|------|------|
| vs TimescaleDB | QuestDB 時序寫入快 10–50 倍，設定更簡單 |
| vs InfluxDB v2 | 批次讀取慢；v3 開源版未成熟 |
| vs ClickHouse | 適合 100 億筆以上，5800 萬筆殺雞用牛刀 |
| vs SQLite | 無時序壓縮；高頻 INSERT 效能差 |

**關鍵優勢：**
- `ILP TCP :9009`：Python `socket` 傳送，零依賴，完全非阻塞
- `SYMBOL` 型別：symbol 欄位字典編碼（int32），GROUP BY 快
- `PARTITION BY DAY`：一行 SQL 刪除過期分區
- `DEDUP UPSERT`：斷線重連補傳不產生重複資料

### 冷儲存：Parquet（本地或 S3）

| 對比 | 結論 |
|------|------|
| vs HDF5 | 不支援原生 S3 串流；單一 writer 限制 |
| vs CSV | 無壓縮；讀取慢 10 倍以上 |
| vs Arrow IPC | 無壓縮，為記憶體格式，不適合長期儲存 |

---

## 五、資料庫 Schema

### 表一：`bars_1s`（1 秒 OHLCV）

```sql
CREATE TABLE bars_1s (
    ts        TIMESTAMP,
    symbol    SYMBOL CAPACITY 64 CACHE,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    volume    DOUBLE,
    vwap      DOUBLE              -- 日內累積 VWAP（每根 bar 更新）
) TIMESTAMP(ts)
PARTITION BY DAY
DEDUP UPSERT KEYS(ts, symbol);
```

### 表二：`cdv_5s`（5 秒 CDV / Delta）

```sql
CREATE TABLE cdv_5s (
    ts        TIMESTAMP,
    symbol    SYMBOL CAPACITY 64 CACHE,
    open      DOUBLE,            -- 本 bar 開始時的 cum_delta
    high      DOUBLE,            -- 本 bar 內 cum_delta 最高點
    low       DOUBLE,            -- 本 bar 內 cum_delta 最低點
    close     DOUBLE,            -- 本 bar 結束時的 cum_delta
    delta     DOUBLE,            -- 本 bar 的 delta（= close − open）
    vwap_day  DOUBLE             -- 當時的日內 VWAP
) TIMESTAMP(ts)
PARTITION BY DAY
DEDUP UPSERT KEYS(ts, symbol);
```

**設計說明：**
- 兩張表獨立查詢，不強制 JOIN，讀取靈活
- `cdv_5s.open/high/low/close` 是 **cum_delta 的 OHLC**，非價格，保留完整 CDV 結構
- `DEDUP UPSERT` 保證重連補傳冪等，不產生重複資料
- `PARTITION BY DAY` 對應 CDV/VWAP 每日重置的邊界

---

## 六、ILP 寫入格式

```
# bars_1s
bars_1s,symbol=MNQ open=19345.5,high=19348.0,low=19344.25,close=19347.0,volume=12.0,vwap=19341.3 1713052800000000000

# cdv_5s
cdv_5s,symbol=MNQ open=342.0,high=356.0,low=342.0,close=356.0,delta=14.0,vwap_day=19341.3 1713052800000000000
```

---

## 七、新增檔案：`storage/writer.py`

```python
"""非同步 QuestDB ILP 寫入器。"""
import asyncio
import socket
from datetime import datetime, timezone


async def db_writer_task(queue: asyncio.Queue, host: str = "127.0.0.1", port: int = 9009):
    """消費 queue，每 0.5s 批次寫入 QuestDB。斷線自動重連。"""
    sock = None
    while True:
        rows = []
        try:
            await asyncio.sleep(0.5)
            while not queue.empty():
                rows.append(queue.get_nowait())
            if not rows:
                continue
            if sock is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.connect((host, port))
            payload = "\n".join(rows) + "\n"
            sock.sendall(payload.encode())
        except Exception:
            if sock:
                sock.close()
                sock = None  # 下一輪重連


def fmt_1s(sym: str, bar: dict) -> str:
    ts_ns = bar["ts"] * 1_000_000_000
    return (
        f"bars_1s,symbol={sym} "
        f"open={bar['open']},high={bar['high']},low={bar['low']},"
        f"close={bar['close']},volume={bar['vol']},vwap={bar.get('vwap', 0.0)} "
        f"{ts_ns}"
    )


def fmt_5s_cdv(sym: str, bar: dict) -> str:
    ts_ns = bar["ts"] * 1_000_000_000
    return (
        f"cdv_5s,symbol={sym} "
        f"open={bar['open']},high={bar['high']},low={bar['low']},"
        f"close={bar['close']},delta={bar['net']},vwap_day={bar.get('vwap_day', 0.0)} "
        f"{ts_ns}"
    )
```

---

## 八、terminal.py 修改清單

```
1. 全域新增：
       _db_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

2. make_tick_cb() 的 _flush_1s_bar() 末尾新增：
       try:
           _db_queue.put_nowait(fmt_1s(sym, bar_dict))
       except asyncio.QueueFull:
           log.debug("db_queue full, drop 1s bar")

3. _ingest_bar() 的 delta_bars.append() 之後新增：
       try:
           _db_queue.put_nowait(fmt_5s_cdv(sym, bar_dict))
       except asyncio.QueueFull:
           log.debug("db_queue full, drop 5s cdv")

4. run_terminal() 連線後新增：
       asyncio.create_task(db_writer_task(_db_queue))
```

---

## 九、Parquet 冷儲存結構

```
data/
  bars_1s/
    symbol=MNQ/
      year=2025/
        2025-04-14.parquet    ← 每日約 50 KB（壓縮後）
        2025-04-15.parquet
  cdv_5s/
    symbol=MNQ/
      year=2025/
        2025-04-14.parquet    ← 每日約 10 KB（壓縮後）
```

**匯出時機：** 每日 17:05 ET（收盤後 5 分鐘），用 QuestDB REST API 查詢並寫出 Parquet。

```python
import pyarrow as pa
import pyarrow.parquet as pq
import requests

def export_day(table: str, symbol: str, date: str, out_path: str):
    q = f"SELECT * FROM {table} WHERE symbol='{symbol}' AND ts::date = '{date}'"
    r = requests.get("http://localhost:9000/exp", params={"query": q})
    # 解析 CSV 回應 → Arrow Table → Parquet
    import io, pyarrow.csv as pacsv
    tbl = pacsv.read_csv(io.BytesIO(r.content))
    pq.write_table(tbl.cast(tbl.schema), out_path,
                   compression="zstd", use_dictionary=True)
```

---

## 十、ML 訓練讀取範例

```python
import pyarrow.dataset as ds

# 讀取 1s bars
bars = ds.dataset("data/bars_1s/", format="parquet", partitioning="hive")
df_bars = bars.to_table(
    filter=(ds.field("symbol") == "MNQ") & (ds.field("year") >= 2025),
    columns=["ts", "open", "high", "low", "close", "volume", "vwap"]
).to_pandas()

# 讀取 CDV
cdv = ds.dataset("data/cdv_5s/", format="parquet", partitioning="hive")
df_cdv = cdv.to_table(
    filter=(ds.field("symbol") == "MNQ") & (ds.field("year") >= 2025),
    columns=["ts", "open", "close", "delta"]
).to_pandas()

# 合併（5s CDV 對齊到 1s bar 的最近 ts）
import pandas as pd
df_bars["ts"] = pd.to_datetime(df_bars["ts"])
df_cdv["ts"]  = pd.to_datetime(df_cdv["ts"])
df = pd.merge_asof(df_bars.sort_values("ts"),
                   df_cdv.sort_values("ts"),
                   on="ts", direction="backward")
```

---

## 十一、雲端部署

### 推薦：AWS EC2 `t3.small` + Elastic IP

```
EC2 t3.small（2 vCPU, 2 GB RAM）+ Elastic IP
│
├── Docker: gnzsnz/ib-gateway
│   └── Port 4001 → 僅 localhost
│
├── Docker: questdb/questdb
│   ├── Port 9009（ILP TCP）→ 僅 localhost
│   └── Port 9000（REST）→ 僅 localhost
│
├── Systemd: terminal.py（開機自動啟動）
│
└── EBS gp3 20 GB（QuestDB 90 天熱資料）
```

### docker-compose.yml

```yaml
services:
  questdb:
    image: questdb/questdb:latest
    ports:
      - "127.0.0.1:9000:9000"
      - "127.0.0.1:9009:9009"
    volumes:
      - questdb_data:/var/lib/questdb

  ibgateway:
    image: gnzsnz/ib-gateway:latest
    ports:
      - "127.0.0.1:4001:4001"
    environment:
      - TWSUSERID=${IB_USERNAME}
      - TWSPASSWORD=${IB_PASSWORD}

volumes:
  questdb_data:
```

### 每月費用

| 項目 | 費用 |
|------|------|
| EC2 t3.small（1 年期預付） | ~$10/月 |
| EBS gp3 20 GB | ~$1.6/月 |
| S3（620 MB 五年歷史） | ~$0.01/月 |
| **合計** | **~$12/月** |

---

## 十二、實作步驟

| 步驟 | 工作 | 狀態 |
|------|------|------|
| 1 | 建立 `storage/writer.py`（ILP 格式化 + writer task） | 待實作 |
| 2 | 修改 `terminal.py`（新增 `_db_queue`，在 flush 點 put_nowait） | 待實作 |
| 3 | 建立 `docker-compose.yml` + 本機驗證 QuestDB 寫入 | 待實作 |
| 4 | 建立每日 Parquet 匯出腳本 | 待實作 |
| 5 | EC2 部署 + systemd 服務設定 | 待實作 |

### 驗證指令

```bash
# 確認 QuestDB 收到資料
curl -G "http://localhost:9000/exec" \
  --data-urlencode "query=SELECT count() FROM bars_1s"

# 確認最新幾筆
curl -G "http://localhost:9000/exec" \
  --data-urlencode "query=SELECT * FROM bars_1s LATEST ON ts PARTITION BY symbol"

# 確認 CDV
curl -G "http://localhost:9000/exec" \
  --data-urlencode "query=SELECT * FROM cdv_5s ORDER BY ts DESC LIMIT 10"
```

---

## 附錄：計算方式

### Delta（OHLC 近似法）

```python
rng   = bar["high"] - bar["low"]
delta = bar["vol"] * (bar["close"] - bar["open"]) / rng if rng > 0 else 0.0
```

### CDV（Cumulative Delta Volume）

```python
# _ingest_bar() 中每根 5s bar 累加，每日開盤重置為 0
state.cum_delta += delta
```

### VWAP（日內成交量加權平均價）

```python
# 典型價 = (H + L + C) / 3
typ = (bar["high"] + bar["low"] + bar["close"]) / 3.0
state._vwap_cum_pv += typ * bar["vol"]
state._vwap_cum_v  += bar["vol"]
state.day_vwap = state._vwap_cum_pv / state._vwap_cum_v
```
