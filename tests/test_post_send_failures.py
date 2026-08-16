"""送出委託之後的失敗，一律是「不知道結局」。

**這是本系統最貴的一條界線，值得一個比較貼實作的測試。**

`SendFutureOrderCLR` 回 0 的那一刻，單就在市場上了。之後不論發生什麼——
COM 元件壞掉、訊息幫浦拋例外、回報主機斷線——都**不能**說成「確定沒有部位」。
說錯的後果是上層不寫狀態檔，13:40 那班以為今天沒進場，部位直接進夜盤。

一般情況下這個檔案的做法（塞假的內部協作物件）不符合本專案的測試慣例，
因為它綁在實作結構上。這裡破例的理由是：這條保證只存在於 `place_order`
送單那一行之後的 try 區段，而那段程式必須有 COM 才跑得到。突變測試證實
沒有這個檔案時，把 `except Exception` 那條拿掉，211 個測試依然全綠。
"""

import pytest

from broker import BUY, ENTRY, FillUnknown, MTX_CODE, OrderFailed, OrderRequest
from broker.capital import CapitalBroker

REQUEST = OrderRequest(
    product=MTX_CODE, order_code="MTX08", contract_month="202608",
    side=BUY, lots=1, intent=ENTRY,
)


class _StubOrder:
    """假的 SKOrderLib。`code` 是 SendFutureOrderCLR 的回傳碼。"""

    def __init__(self, code=0, message="SEQ0000000001"):
        self._code, self._message = code, message

    def SendFutureOrderCLR(self, user_id, is_async, order):
        return self._message, self._code


class _StubSk:
    class FUTUREORDER:
        pass


class _StubReplyEvents:
    rows: list = []


def _broker(*, order_code=0, on_wait=None):
    """組一個內部協作物件都被換掉、不碰 COM 的 CapitalBroker。"""
    broker = CapitalBroker("id", "pw", environment="test", account="F9990001234567")
    broker._center = object()          # 只用來過「尚未登入」那道檢查
    broker._order_ready = True
    broker._order = _StubOrder(code=order_code)
    broker._sk = _StubSk()
    broker._reply_events = _StubReplyEvents()
    broker._message = lambda code: f"代碼 {code}"
    if on_wait is not None:
        broker._pump_until = on_wait
    return broker


# --- 送出去之前失敗：確定沒有部位 ---


def test_a_rejected_send_is_a_plain_order_failure():
    """送出這一步就被擋下 → 確定沒有部位產生，OrderFailed 名副其實。"""
    broker = _broker(order_code=1038)
    with pytest.raises(OrderFailed):
        broker.place_order(REQUEST)


# --- 送出去之後失敗：一律是「不知道」 ---


@pytest.mark.parametrize("error", [
    OSError("COM 物件已失效"),
    RuntimeError("訊息幫浦異常"),
    ImportError("pythoncom 不見了"),
])
def test_any_failure_after_the_send_becomes_fill_unknown(error):
    """單已經在市場上了，這些意外都不代表「沒有部位」。

    參數化三種不同的例外，是因為真正的風險在於「**有沒有漏掉某一類**」——
    只測一種的話，換成別種例外時這條保證會靜靜消失。
    """
    def _explode(*_args, **_kwargs):
        raise error

    broker = _broker(on_wait=_explode)
    with pytest.raises(FillUnknown) as exc:
        broker.place_order(REQUEST)
    assert exc.value.order_seq == "SEQ0000000001", "序號要帶出去，人工才查得到"


def test_the_original_error_is_not_lost():
    """轉成 FillUnknown 之後，原始原因仍要看得到，否則無從排查。"""
    def _explode(*_args, **_kwargs):
        raise OSError("COM 物件已失效")

    with pytest.raises(FillUnknown) as exc:
        _broker(on_wait=_explode).place_order(REQUEST)
    assert "COM 物件已失效" in str(exc.value)
    assert isinstance(exc.value.__cause__, OSError)


def test_an_order_failure_raised_after_the_send_is_not_masked():
    """`_await_fill` 判定「被拒且無成交」時是真的沒有部位，不該被改寫成不確定。

    回報在**送單之後**才到，所以是在 `_pump_until` 期間才塞進去——
    先塞的話 `rows[since:]` 會是空的，測到的就變成逾時而不是被拒。
    """
    rejected_row = [""] * 49
    rejected_row[0] = rejected_row[47] = "SEQ0000000001"
    rejected_row[1], rejected_row[2] = "TF", "N"
    rejected_row[3], rejected_row[20] = "Y", "1"

    broker = None

    def _reply_arrives(predicate, seconds):
        broker._reply_events.rows.append(",".join(rejected_row))
        return predicate()

    broker = _broker(on_wait=_reply_arrives)
    broker._reply_events.rows = []
    with pytest.raises(OrderFailed):
        broker.place_order(REQUEST)
