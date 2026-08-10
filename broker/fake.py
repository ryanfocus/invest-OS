"""測試用的假 broker。

它做兩件事：回放測試指定的資料，以及**記錄自己被要求做了什麼**。
後者是關鍵——「自動下單開關關閉時，下單函式一次都沒被呼叫」這種驗收條件，
只有在能觀察呼叫紀錄時才證明得了。

契約與真實 broker 相同（`broker.__init__` 的例外與資料結構），
所以同一組行為測試可以同時跑在兩者上。
"""

from __future__ import annotations

from broker import OpenPrices


class FakeBroker:
    """回放預先安排的回應。

    兩種用法：

        FakeBroker(tx=44177, mtx=44142, tmf=44258)   # 每次都回這組
        FakeBroker(script=[QuoteNotReady("..."), OpenPrices(...)])

    `script` 逐次消耗；用完之後**重複最後一項**，這樣「一直失敗」只要寫一個項目。
    項目是例外就 raise，是 `OpenPrices` 就回傳。
    """

    def __init__(
        self,
        tx: float | None = None,
        mtx: float | None = None,
        tmf: float | None = None,
        *,
        script: list | None = None,
        login_error: Exception | None = None,
    ):
        if script is None:
            script = [OpenPrices(tx=tx, mtx=mtx, tmf=tmf)]
        self._script = list(script)
        self._login_error = login_error
        self.open_price_calls = 0
        self.login_calls = 0

    def login(self) -> None:
        self.login_calls += 1
        if self._login_error is not None:
            raise self._login_error

    def get_open_prices(self, expected_trading_day: int | None = None) -> OpenPrices:
        self.open_price_calls += 1
        item = self._script[0] if len(self._script) == 1 else self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
