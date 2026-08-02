"""測試用的假 broker。

它做兩件事：回放測試指定的資料，以及**記錄自己被要求做了什麼**。
後者是關鍵——「自動下單開關關閉時，下單函式一次都沒被呼叫」這種驗收條件，
只有在能觀察呼叫紀錄時才證明得了。
"""

from __future__ import annotations

from broker import OpenPrices


class FakeBroker:
    """回放固定的開盤價，並計數被呼叫的次數。"""

    def __init__(self, tx: float, mtx: float, tmf: float):
        self._opens = OpenPrices(tx=tx, mtx=mtx, tmf=tmf)
        self.open_price_calls = 0

    def get_open_prices(self) -> OpenPrices:
        self.open_price_calls += 1
        return self._opens
