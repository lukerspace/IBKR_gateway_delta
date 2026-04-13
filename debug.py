"""
逐步診斷：連線 → 設資料類型 → 取 MNQ → 等 5 秒 → 印 ticker 狀態
執行: python debug.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ib_insync import IB, Stock, Future, Contract

ib = IB()
ib.connect('127.0.0.1', 4002, clientId=99)
print(f"連線: {ib.isConnected()}  帳戶: {ib.managedAccounts()}")

# ── 1. 設市場資料類型 ─────────────────────────────────────────────────────────
ib.reqMarketDataType(3)   # 3 = Delayed（Paper 帳戶最容易拿到）
print("已設 reqMarketDataType(3)")

# ── 2. QQQ ───────────────────────────────────────────────────────────────────
qqq = Stock('QQQ', 'SMART', 'USD')
ib.qualifyContracts(qqq)
t_qqq = ib.reqMktData(qqq, '', False, False)

# ── 3. MNQ：先列出所有可用合約 ────────────────────────────────────────────────
base = Future(symbol='MNQ', exchange='CME', currency='USD')
details = ib.reqContractDetails(base)
print(f"\nMNQ 合約數: {len(details)}")
for d in details[:5]:
    c = d.contract
    print(f"  conId={c.conId}  localSymbol={c.localSymbol}"
          f"  expiry={c.lastTradeDateOrContractMonth}"
          f"  exchange={c.exchange}")

# 取近月
valid = sorted(
    [d for d in details if len(d.contract.lastTradeDateOrContractMonth or '') >= 6],
    key=lambda d: d.contract.lastTradeDateOrContractMonth
)
if not valid:
    print("❌ 找不到有效 MNQ 合約")
    ib.disconnect(); sys.exit(1)

front = valid[0].contract
print(f"\n近月合約: {front.localSymbol}  conId={front.conId}")

# 用 conId qualify
c = Contract(conId=front.conId, exchange=front.exchange)
qualified = ib.qualifyContracts(c)
mnq = qualified[0]
print(f"Qualified: {mnq}")

t_mnq = ib.reqMktData(mnq, '233', False, False)

# ── 4. 等 8 秒讓資料進來（ib.sleep 會處理事件迴圈）─────────────────────────
print("\n⏳ 等 8 秒接收資料...")
ib.sleep(8)

# ── 5. 印結果 ─────────────────────────────────────────────────────────────────
print("\n══ QQQ ══")
print(f"  bid={t_qqq.bid}  ask={t_qqq.ask}  last={t_qqq.last}  volume={t_qqq.volume}")
print(f"  high={t_qqq.high}  low={t_qqq.low}  close={t_qqq.close}")

print("\n══ MNQ ══")
print(f"  bid={t_mnq.bid}  ask={t_mnq.ask}  last={t_mnq.last}  volume={t_mnq.volume}")
print(f"  high={t_mnq.high}  low={t_mnq.low}  close={t_mnq.close}")
print(f"  hasBidAsk={t_mnq.hasBidAsk()}")

print("\n══ 完整 MNQ Ticker ══")
print(t_mnq)

ib.cancelMktData(qqq)
ib.cancelMktData(mnq)
ib.disconnect()
print("\n✅ 診斷完成")
