"""合約定義：QQQ（股票）與 MNQ（Micro E-mini Nasdaq-100 期貨）。

MNQ 合約說明：
  - Exchange  : CME（或 GLOBEX，兩者皆可；CME 更穩定）
  - Multiplier: 2（$2 × Nasdaq-100 指數）
  - 季月合約  : 3月(H) / 6月(M) / 9月(U) / 12月(Z)
  - 推薦用 reqContractDetails 自動取近月，避免手動維護到期月
"""
from __future__ import annotations

from ib_insync import IB, Future, Stock


def qqq() -> Stock:
    """QQQ ETF — SMART routing, USD."""
    return Stock("QQQ", "SMART", "USD")


def mnq_front(ib: IB) -> Future:
    """自動查詢 MNQ 近月合約並完整 qualify。

    先用 CME 查，查不到再試 GLOBEX。
    回傳已 qualify（含 conId）的合約，確保 reqMktData 能正確訂閱。
    """
    for exchange in ("CME", "GLOBEX"):
        base = Future(symbol="MNQ", exchange=exchange, currency="USD")
        details = ib.reqContractDetails(base)
        if details:
            break
    else:
        raise RuntimeError(
            "找不到 MNQ 合約。\n"
            "請確認：\n"
            "  1. Gateway / TWS 已啟動且 API 已開啟\n"
            "  2. 帳戶有期貨資料訂閱權限（CME Futures）"
        )

    # 只保留一般季月合約（排除 spread / combo）
    valid = [
        d for d in details
        if d.contract.lastTradeDateOrContractMonth
        and len(d.contract.lastTradeDateOrContractMonth) >= 6
        and not d.contract.localSymbol.startswith("D:")  # 排除日曆價差
    ]
    valid.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    front = valid[0].contract

    # 用 conId + exchange 重新 qualify，確保欄位完整
    from ib_insync import Contract
    c = Contract(conId=front.conId, exchange=front.exchange)
    qualified = ib.qualifyContracts(c)
    if not qualified:
        raise RuntimeError(f"無法 qualify MNQ conId={front.conId}")

    result = qualified[0]
    print(
        f"📌 MNQ 近月合約  "
        f"LocalSymbol={result.localSymbol}  "
        f"ConId={result.conId}  "
        f"Exchange={result.exchange}  "
        f"到期={result.lastTradeDateOrContractMonth}  "
        f"Multiplier={result.multiplier}"
    )
    return result


def mnq_fixed(expiry: str = "202606") -> Future:
    """手動指定到期月的 MNQ 合約（格式 YYYYMM）。

    Args:
        expiry: 例如 "202606"（2026年6月）、"202609"（9月）

    範例:
        mnq_fixed("202609")  # 2026年9月合約
    """
    return Future(
        symbol                       = "MNQ",
        lastTradeDateOrContractMonth = expiry,
        exchange                     = "CME",
        currency                     = "USD",
        multiplier                   = "2",
    )
