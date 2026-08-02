"""OS 策略的訊號判定 —— 純函式，無 I/O、無狀態。

規則見 CONTEXT.md「訊號方向」：比較大台、小台、微台的**開盤價**，
要嘛純大於、要嘛純小於，其餘一律不動作。
"""

from __future__ import annotations

from dataclasses import dataclass

# 訊號方向。「不動作」是明確的第三種結果，不是失敗、也不是無資料——
# 這兩者的區別在 CONTEXT.md 有特別標註，混為一談會讓故障被當成正常。
LONG = "LONG"
SHORT = "SHORT"
NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class SignalResult:
    """當日訊號與其判斷依據。三個開盤價一併帶著，供 Discord 發報使用。"""

    signal: str
    tx: float
    mtx: float
    tmf: float


def compute(tx: float, mtx: float, tmf: float) -> SignalResult:
    """依三個開盤價判定當日訊號方向。

    參數為**當日 AM 盤（日盤）開盤價**，非即時價、非夜盤價。
    """
    if tx > mtx and tx > tmf:
        signal = LONG
    elif tx < mtx and tx < tmf:
        signal = SHORT
    else:
        signal = NO_TRADE

    return SignalResult(signal=signal, tx=tx, mtx=mtx, tmf=tmf)
