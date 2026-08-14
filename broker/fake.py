"""測試用的假 broker。

它做兩件事：回放測試指定的資料，以及**記錄自己被要求做了什麼**。
後者是關鍵——「自動下單開關關閉時，下單函式一次都沒被呼叫」這種驗收條件，
只有在能觀察呼叫紀錄時才證明得了。

契約與真實 broker 相同（`broker.__init__` 的例外與資料結構），
所以同一組行為測試可以同時跑在兩者上。
"""

from __future__ import annotations

from broker import (
    ContractInfo,
    OpenPrices,
    OrderResult,
    PRODUCT_CODES,
    build_open_prices,
)


class FakeBroker:
    """回放預先安排的回應。

    三種用法：

        FakeBroker(tx=44177, mtx=44142, tmf=44258)   # 每次都回這組
        FakeBroker(script=[QuoteNotReady("..."), OpenPrices(...)])
        FakeBroker(quotes={TX_CODE: Quote(...), ...})   # 走真正的 L0/L1/L2 檢查

    `script` 逐次消耗；用完之後**重複最後一項**，這樣「一直失敗」只要寫一個項目。
    項目是例外就 raise，是 `OpenPrices` 就回傳。

    ⚠️ **`script` 與 `tx/mtx/tmf` 兩種用法會忽略 `expected_trading_day`**——
    它們回的是成品 `OpenPrices`，沒有日期可比。要驗證新鮮度守衛真的接在流程上，
    必須用 `quotes=`：那條路會呼叫真正的 `build_open_prices`，跟正式 broker 同一份邏輯。
    （2026-08-11 突變測試發現：在 `quotes=` 出現以前，把 `main.py` 傳日期那行
    改成 `None`，全部測試依然通過——假 broker 收下參數卻不用，等於沒有人守這條線。）
    """

    def __init__(
        self,
        tx: float | None = None,
        mtx: float | None = None,
        tmf: float | None = None,
        *,
        script: list | None = None,
        quotes: dict | None = None,
        login_error: Exception | None = None,
        contracts: dict | None = None,
        contracts_error: Exception | None = None,
        order_error: Exception | None = None,
        fills: list | None = None,
    ):
        if script is None and quotes is None:
            script = [OpenPrices(tx=tx, mtx=mtx, tmf=tmf)]
        self._script = list(script) if script is not None else None
        self._quotes = quotes
        self._login_error = login_error
        self._contracts = contracts if contracts is not None else {
            code: ContractInfo(code=code, last_trading_day=20260819) for code in PRODUCT_CODES
        }
        self._contracts_error = contracts_error
        self._order_error = order_error
        self._fills = list(fills) if fills is not None else None
        self.open_price_calls = 0
        self.login_calls = 0
        self.contract_calls = 0
        # 收到的委託，依序記錄。「一次都沒被呼叫」要斷言 `orders == []`——
        # 這是本系統唯一會動到錢的路徑，證明它沒被走過比證明它走對了更重要。
        self.orders: list = []

    def login(self) -> None:
        self.login_calls += 1
        if self._login_error is not None:
            raise self._login_error

    def get_open_prices(self, expected_trading_day: int | None = None) -> OpenPrices:
        self.open_price_calls += 1
        if self._quotes is not None:
            # 走真正的檢查——這是唯一能證明 L0 有接上流程的路徑。
            return build_open_prices(self._quotes, expected_trading_day=expected_trading_day)
        item = self._script[0] if len(self._script) == 1 else self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get_contracts(self, wait: float = 8.0) -> dict:
        self.contract_calls += 1
        if self._contracts_error is not None:
            raise self._contracts_error
        return self._contracts

    def place_order(self, request) -> OrderResult:
        """記下委託並回報成交。

        預設全部成交（`fills=None`）；要測部分成交或未成交就用 `fills=[口數, ...]`，
        逐次消耗、用完重複最後一項。
        """
        self.orders.append(request)
        if self._order_error is not None:
            raise self._order_error
        if self._fills is None:
            filled = request.lots
        else:
            filled = self._fills[0] if len(self._fills) == 1 else self._fills.pop(0)
        return OrderResult(filled_lots=filled, order_seq=f"FAKE{len(self.orders):09d}")
