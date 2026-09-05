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


class _Unset:
    """「沒有設定」的哨兵。與 `None` 刻意分開——見 `query_result`。"""

    def __repr__(self) -> str:
        return "<未設定>"


_UNSET = _Unset()


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
        position_type: str = "",
        accounts: list | None = None,
        fills: list | None = None,
        query_result: object = _UNSET,
    ):
        if script is None and quotes is None:
            script = [OpenPrices(tx=tx, mtx=mtx, tmf=tmf)]
        self._script = list(script) if script is not None else None
        self._quotes = quotes
        self._login_error = login_error
        # 券商回報的倉別。預設空字串＝「不知道」，與真實的『看不懂』一致——
        # 想測倉別檢查的測試必須自己寫明，那件事因此在測試碼裡看得見。
        self._position_type = position_type
        self._accounts = accounts or []
        self._contracts = contracts if contracts is not None else {
            code: ContractInfo(code=code, last_trading_day=20260819) for code in PRODUCT_CODES
        }
        self._contracts_error = contracts_error
        self._order_error = order_error
        self._fills = list(fills) if fills is not None else None
        # 成交查詢（ticket 09 的後備管道）要回什麼。
        #
        # `_UNSET` ≠ `None`：**沒設定**代表這個測試不走查詢那條路，查了就是
        # 測試沒寫對；**明確設成 None** 代表「查了但查不到」，那是要測的狀態之一。
        # 兩者若共用 None，「忘了設定」與「刻意設成查不到」會長得一模一樣。
        self._query_result = query_result
        self.query_calls: list = []
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

    def _next_order_error(self):
        """下一次 `place_order` 該拋什麼（`None` 代表成功）。

        `order_error` 給單一例外時每次都拋（「一直失敗」）；給 list 時逐次消耗，
        用完之後不再拋——這樣「前兩次失敗、第三次成功」寫成
        `[OrderFailed(...), OrderFailed(...), None]` 就好。
        """
        if self._order_error is None:
            return None
        if not isinstance(self._order_error, list):
            return self._order_error
        return self._order_error.pop(0) if self._order_error else None

    def list_accounts(self) -> list:
        """假的帳號清單。預設空的——想測「查得到」的測試要自己給。"""
        return list(self._accounts)

    def place_order(self, request) -> OrderResult:
        """記下委託並回報成交。

        預設全部成交（`fills=None`）；要測部分成交或未成交就用 `fills=[口數, ...]`，
        逐次消耗、用完重複最後一項。
        """
        self.orders.append(request)
        error = self._next_order_error()
        if error is not None:
            raise error
        if self._fills is None:
            filled = request.lots
        else:
            filled = self._fills[0] if len(self._fills) == 1 else self._fills.pop(0)
        return OrderResult(filled_lots=filled,
                           order_seq=f"FAKE{len(self.orders):09d}",
                           position_type=self._position_type)

    def query_filled_lots(self, *, order_seq, trading_day, requested_lots, sleep=None):
        """後備管道：主動問券商主機成交幾口。回 `None` 代表還是不知道。

        記下每次呼叫的參數。**序號要一起斷言**——查詢若拿錯序號，
        查回來的會是別人的成交，而那比查不到更糟。

        ⚠️ **與真 broker 的兩處刻意不同，都寫在這裡免得日後被當成 bug：**

        1. `query_result` 沒設定時**拋 AssertionError**，而真的保證不拋例外。
           那是測試用的哨兵：它要抓的是「程式走到了不該查的地方」。
           真的不會這樣做（那會讓補救措施自己把整班弄掛）。
           要模擬「查詢炸掉」請自己繼承覆寫，別依賴這個哨兵。

        2. 真的**不可能**回傳超過委託口數（`parse_filled_lots` 有上限守衛，
           推錯欄位最常見的症狀就是抓到一個不相干的大數字）。假的以前照收，
           於是測試組得出來的情境在正式環境根本不會發生——那種測試證明不了事。
           現在假的也擋。
        """
        self.query_calls.append(
            {"order_seq": order_seq, "trading_day": trading_day,
             "requested_lots": requested_lots}
        )
        if self._query_result is _UNSET:
            raise AssertionError(
                "這個測試沒有安排 query_result，但流程走到了成交查詢。"
                "要嘛是測試該補上 query_result=，要嘛是程式不該查這一次。"
            )
        if self._query_result is not None and self._query_result > requested_lots:
            raise AssertionError(
                f"query_result={self._query_result} 超過委託的 {requested_lots} 口。"
                "真的 broker 有上限守衛，回不出這個值——這個測試在模擬一個"
                "正式環境不會發生的情境。"
            )
        return self._query_result
