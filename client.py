"""IBKR Gateway 連線管理。

負責建立與維持 ib_insync IB 物件，提供單一連線給所有模組共用。
"""
from __future__ import annotations

from ib_insync import IB

_ib: IB | None = None


def get_ib() -> IB:
    global _ib
    if _ib is None:
        _ib = IB()
    return _ib


_MARKET_DATA_TYPES = {
    1: "Live（即時）",
    2: "Frozen（凍結）",
    3: "Delayed（延遲15分鐘）",
    4: "Delayed Frozen",
}


def connect(
    host: str = "127.0.0.1",
    port: int = 4001,
    client_id: int = 1,
    readonly: bool = False,
    market_data_type: int = 3,
) -> IB:
    """連線到 IBKR Gateway，回傳已連線的 IB 物件。

    Args:
        host:             Gateway IP（預設 localhost）
        port:             4002 = 模擬帳戶 / 4001 = 真實帳戶
        client_id:        每個連線需唯一，避免衝突
        readonly:         只讀模式（不下單）
        market_data_type: 1=Live / 2=Frozen / 3=Delayed / 4=Delayed Frozen
                          Paper 帳戶建議先用 3，Live 帳戶用 1
    """
    ib = get_ib()
    if ib.isConnected():
        return ib

    ib.connect(host, port, clientId=client_id, readonly=readonly)

    # 必須在 qualify / reqMktData 之前設定
    ib.reqMarketDataType(market_data_type)
    label = _MARKET_DATA_TYPES.get(market_data_type, str(market_data_type))
    print(f"✅ 已連線 Gateway {host}:{port} (clientId={client_id})")
    print(f"   帳戶: {ib.managedAccounts()}")
    print(f"   市場資料模式: [{market_data_type}] {label}")
    return ib


def disconnect() -> None:
    ib = get_ib()
    if ib.isConnected():
        ib.disconnect()
        print("🔌 已斷線")
