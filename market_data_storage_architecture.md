# 市場資料儲存系統架構設計

> 基於 `ibkr_gateway` 專案，設計可雲端部署的秒級市場資料儲存系統
> 儲存欄位：Delta、CDV、VWAP、O、H、L、C

---

## 一、目標與現況

### 現況問題
| 問題 | 說明 |
|---|---|
| 純記憶體 | 所有資料（QuoteState、deque）在程式結束後消失 |
| 最多 200 根 K 棒 | `deque(maxlen=200)` 限制歷史深度 |
| 僅 30 秒 K 棒 | 無法取得更細粒度的資料 |
| 2 個硬編碼商品 | QQQ、MNQ 寫死在程式碼中 |
| 無雲端部署 | 無 Docker、無 CI/CD、無持久化 |

### 目標
1. **即時寫入** 1 秒級 K 棒資料到資料庫（不影響終端機效能）
2. **長期保存** 歷史資料供機器學習訓練使用
3. **雲端部署**，穩定 24/7 運行

---

## 二、資料量估算

| 項目 | 數值 |
|---|---|
| 正規盤每日秒數（6.5 小時） | 23,400 秒 |
| 延盤每日秒數（16 小時） | 57,600 秒 |
| 10 檔商品 × 5 年 × 250 交易日 | **2.93 億筆** |
| 原始未壓縮大小 | ~26 GB |
| Parquet + zstd 壓縮（OHLCV 約 8:1）| **~3.3 GB** |
| QuestDB 熱資料（近 90 天） | ~500 MB |

> 結論：**中小型資料，不需要分散式系統**

---

## 三、系統架構圖

```
IBKR Gateway (port 4001)
        │
        │  ib_insync 回調（asyncio 事件迴圈）
        ▼
  make_bar_cb() → _ingest_bar()       ← terminal.py 現有邏輯
        │
        ├──► QuoteState（記憶體）→ Rich 終端機畫面   [不變]
        │
        └──► asyncio.Queue(maxsize=1000)   ← 非阻塞 put_nowait()
                    │
              _db_writer_task()           ← 新增協程
                    │ ILP TCP :9009（fire-and-forget）
                    ▼
              QuestDB（Docker 容器）      ← 熱儲存，滾動 90 天
                    │
              每日收盤後匯出任務（17:00 ET 觸發）
                    │ PyArrow → Parquet zstd float32 → S3
                    ▼
  s3://bucket/bars_1s/symbol=MNQ/year=2025/2025-04-12.parquet

  S3 ────────► ML 訓練 VM（EC2 Spot，按需啟動）
               pyarrow.dataset → pandas → torch.Tensor
```

---

## 四、技術選型

### 熱儲存：QuestDB

**與其他方案比較：**

| 對比 | 結論 |
|---|---|
| vs TimescaleDB | QuestDB 時序寫入/讀取快 10–50 倍，不需調整 PostgreSQL |
| vs InfluxDB v3 | v3 開源版本尚未成熟；v2 批次讀取慢 |
| vs ClickHouse | ClickHouse 適合 100 億筆以上，293M 筆是殺雞用牛刀 |
| vs Kafka 串流 | 目前 ~0.4 筆/秒，完全不需要 Kafka |

**QuestDB 關鍵優勢：**
- `ILP TCP` 協定 — Python 用 socket 送資料，完全非阻塞
- `SYMBOL` 型別 — symbol 欄位自動字典編碼（int32），GROUP BY 超快
- `PARTITION BY DAY` — 一行 SQL 刪除舊分區
- `DEDUP UPSERT` — 斷線重連重傳不產生重複資料

---

### 冷儲存：Parquet on S3

**與其他方案比較：**

| 對比 | 結論 |
|---|---|
| vs HDF5 | HDF5 無法原生串流 S3，單一 writer 限制 |
| vs Zarr | Zarr 適合多維陣列，金融表格資料用 Parquet 更合適 |
| vs Arrow IPC | Arrow IPC 無壓縮，是記憶體格式，不適合長期儲存 |

---

## 五、資料庫 Schema

### QuestDB 熱資料表

```sql
CREATE TABLE bars_1s (
    ts              TIMESTAMP,          -- UTC，奈秒精度
    symbol          SYMBOL CAPACITY 64 CACHE,  -- 字典編碼，如 'MNQ'
    bar_size_secs   SHORT,              -- 1 / 5 / 30 / 60（向後相容）
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    close           DOUBLE,
    volume          DOUBLE,
    delta           DOUBLE,             -- 單根 K 棒 delta（OHLC 近似計算）
    cdv             DOUBLE,             -- 累積 delta volume（每日重置）
    vwap_period     DOUBLE,             -- 周期 VWAP
    vwap_daily      DOUBLE              -- 每日累積 VWAP
) TIMESTAMP(ts)
PARTITION BY DAY
DEDUP UPSERT KEYS(ts, symbol, bar_size_secs);
```

**欄位說明：**
- `delta`：單根 K 棒的買賣力道差，計算方式：`vol × (close − open) / (high − low)`
- `cdv`：每日累積 delta，每天開盤重置為 0
- `vwap_daily`：`累積(典型價 × 量) / 累積量`，典型價 = `(H + L + C) / 3`
- `bar_size_secs = SHORT`：未來新增 5 秒、1 分鐘棒無需改 schema

### Parquet 冷儲存（ML 訓練）

相同欄位，但優化差異：
- 所有數值欄位改用 **float32**（ML 精度足夠，節省 50% 儲存空間）
- `symbol` 用字典編碼 `pa.dictionary(pa.int16(), pa.string())`
- `ts` 用 `int64` 奈秒

---

## 六、S3 分區策略

### 目錄結構

```
s3://your-bucket/bars_1s/
  symbol=MNQ/
    year=2021/
      2021-01-04.parquet    ← 每日約 260 KB（壓縮後）
      2021-01-05.parquet
      ...
    year=2022/
      ...
  symbol=QQQ/
    year=2021/
      ...
```

### 設計邏輯

| 設計 | 原因 |
|---|---|
| Symbol 在最外層 | 訓練只用 MNQ 時，不讀 QQQ 任何一個 byte |
| 每日一個檔案 | CDV/VWAP 每日重置，日邊界 = 特徵正規化邊界，不需跨檔 join |
| year 子目錄 | 避免單一 S3 前綴超過 10 萬個物件，防止 LIST 效能退化 |

---

## 七、ML 訓練讀取範例

```python
import pyarrow.dataset as ds
import pyarrow as pa
import pyarrow.fs as pafs

# 連接 S3
s3 = pafs.S3FileSystem(region="us-east-1")

# 建立 dataset（支援分區剪枝）
dataset = ds.dataset(
    "your-bucket/bars_1s/",
    filesystem=s3,
    format="parquet",
    partitioning=ds.partitioning(flavor="hive")
)

# 只讀取 MNQ 2023 年以後
table = dataset.to_table(
    filter=(
        (ds.field("symbol") == "MNQ") &
        (ds.field("year") >= 2023)
    ),
    columns=["ts", "open", "high", "low", "close",
             "volume", "delta", "cdv", "vwap_daily"]
)

# Arrow Table → pandas → torch.Tensor（5M 筆約 2 秒）
import torch
df = table.to_pandas()
tensor = torch.tensor(
    df[["open", "high", "low", "close", "delta", "cdv", "vwap_daily"]].values,
    dtype=torch.float32
)
```

---

## 八、雲端部署

### 為什麼用單一 VM 而不是 Kubernetes？

> **IBKR Gateway 要求穩定固定 IP 的持久 TCP 連線。**
> K8s Pod 有臨時 IP，可能在不同節點重啟，這會斷開 IBKR 連線。

### 推薦：AWS EC2 `c6i.large` + Elastic IP

```
EC2 c6i.large（2 vCPU, 4 GB RAM）+ Elastic IP（固定公網 IP）
│
├── Docker: gnzsnz/ib-gateway
│   ├── IBC 無頭模式（自動登入，無需 GUI）
│   └── Port 4001 → 僅限 localhost
│
├── Docker: questdb/questdb
│   ├── Port 9009（ILP TCP）→ 僅限 localhost
│   └── Port 9000（HTTP REST）→ 僅限 VPN/localhost
│
├── Systemd: terminal.py（開機自動啟動的 Python 服務）
│
└── EBS gp3 100 GB（QuestDB 資料儲存）
```

### 每月費用估算

| 項目 | 費用 |
|---|---|
| EC2 c6i.large（1 年期預付） | ~$18/月 |
| EC2 c6i.large（隨用隨付） | ~$61/月 |
| EBS gp3 100 GB | ~$8/月 |
| S3（3.3 GB 五年歷史） | ~$0.08/月 |
| Elastic IP（掛載中免費） | $0 |
| **合計（預付）** | **~$26/月** |

---

## 九、需要修改/新增的檔案

### 新增：`storage/writer.py`

| 函數 | 功能 |
|---|---|
| `_db_writer_task(queue, host)` | 非同步協程，消費 queue，批次寫入 QuestDB |
| `_flush_ilp(sock, rows)` | 格式化 InfluxDB Line Protocol 並透過 socket 送出 |
| `export_day_to_parquet(date, symbols, ...)` | 每日收盤後從 QuestDB 匯出 Parquet 到 S3 |
| `get_training_dataset(symbols, dates, ...)` | ML 訓練資料集入口，回傳 `pyarrow.Dataset` |

### 修改：`terminal.py`

```
1. barSizeSetting='30 secs'  →  '1 secs'   （升級 bar 解析度）
2. 新增模組全域：_db_queue = asyncio.Queue(maxsize=1000)
3. _ingest_bar() 末尾：queue.put_nowait(row_dict)  ← try/except QueueFull 靜默丟棄
4. 啟動時：asyncio.create_task(_db_writer_task(_db_queue))
```

### 修改：`client.py`

```
新增非同步重連迴圈
原因：IBKR Gateway 每天約 23:45 ET 強制重啟，需要自動重連
```

### 新增：`docker-compose.yml`

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
    driver: local
```

---

## 十、實作步驟

| 步驟 | 工作 |
|---|---|
| 1 | 建立 `storage/writer.py`（ILP 寫入 + Parquet 匯出邏輯） |
| 2 | 修改 `terminal.py`（bar 解析度、queue、writer task） |
| 3 | 修改 `client.py`（重連迴圈） |
| 4 | 建立 `docker-compose.yml` |
| 5 | **本機測試**：啟動 QuestDB Docker，驗證 ILP 寫入，查詢確認 |
| 6 | **部署 EC2**：配置 EIP、設定 IBKR IBC 自動登入 |
| 7 | 設定每日匯出排程（systemd timer 或 AWS EventBridge + Lambda） |

### 驗證方式

```bash
# 1. 確認 QuestDB 有收到資料
curl -G "http://localhost:9000/exec" --data-urlencode "query=SELECT count() FROM bars_1s"

# 2. 確認分區正常建立
ls /var/lib/questdb/db/bars_1s/

# 3. 確認 Parquet 匯出到 S3
aws s3 ls s3://your-bucket/bars_1s/symbol=MNQ/year=2025/

# 4. Python 讀取驗證
python -c "
import pyarrow.dataset as ds
d = ds.dataset('s3://your-bucket/bars_1s/', format='parquet', partitioning='hive')
print(d.to_table(filter=ds.field('symbol')=='MNQ').shape)
"
```

---

## 附錄：Delta 與 VWAP 計算方式

### Delta（現有 OHLC 近似法）

```python
# 現有程式碼（terminal.py 約 218 行）
rng = bar["high"] - bar["low"]
delta = bar["vol"] * (bar["close"] - bar["open"]) / rng if rng > 0 else 0.0
```

> 若需要更精確的 tick-by-tick delta，需要 IBKR Level 2 訂閱或 `reqTickByTickData()`

### CDV（累積 Delta Volume）

```python
# 每根 K 棒後累加
state.cum_delta += delta  # 每日開盤時重置為 0
```

### VWAP（成交量加權平均價）

```python
# 典型價 = (H + L + C) / 3
typical_price = (bar["high"] + bar["low"] + bar["close"]) / 3.0
state._vwap_cum_pv += typical_price * bar["vol"]
state._vwap_cum_v  += bar["vol"]
state.day_vwap = state._vwap_cum_pv / state._vwap_cum_v
```
