-- ── 建立前先確認 QuestDB 已啟動（docker compose up -d questdb）──────────────
-- 執行方式：在 QuestDB Web Console (http://localhost:9000) 貼上執行，或用 REST API

-- 1s OHLCV bars（從 tick 聚合）
CREATE TABLE IF NOT EXISTS bars_1s (
    ts        TIMESTAMP,
    symbol    SYMBOL CAPACITY 64 CACHE,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    volume    DOUBLE,
    vwap      DOUBLE
) TIMESTAMP(ts)
PARTITION BY DAY
WAL
DEDUP UPSERT KEYS(ts, symbol);

-- 5s CDV bars（cum_delta OHLC + per-bar delta）
CREATE TABLE IF NOT EXISTS cdv_5s (
    ts        TIMESTAMP,
    symbol    SYMBOL CAPACITY 64 CACHE,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    delta     DOUBLE,
    vwap_day  DOUBLE
) TIMESTAMP(ts)
PARTITION BY DAY
WAL
DEDUP UPSERT KEYS(ts, symbol);
