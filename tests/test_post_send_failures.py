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


def _broker(*, order_code=0, on_wait=None, message="SEQ0000000001"):
    """組一個內部協作物件都被換掉、不碰 COM 的 CapitalBroker。"""
    broker = CapitalBroker("id", "pw", environment="test", account="F9990001234567")
    broker._center = object()          # 只用來過「尚未登入」那道檢查
    broker._order_ready = True
    broker._order = _StubOrder(code=order_code, message=message)
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


# --- 送出成功卻拿不到委託序號 ---
#
# 2026-08-19 實測發現的漏洞。序號來自 `SendFutureOrderCLR` 的回傳訊息，
# 而那是整個系統裡**唯一從來沒有真正執行過的 API**——它成功時回不回序號，
# 我們其實不知道。
#
# 序號是 OS 認出自己那筆回報的唯一依據：回報事件是共用的，使用者的手動交易
# 走同一條線（當天換倉的價差單就出現在這條串流上，見
# fixtures/onnewdata-spread-2026-08-19.txt）。


@pytest.mark.parametrize("message", ["", "   ", None])
def test_a_send_without_a_sequence_number_is_fill_unknown(message):
    """**單已經在市場上了**（code == 0），只是我們認不出它的回報。

    `OrderFailed` 會是災難性的誤述：它的意思是「確定沒有部位」，
    上層因此不寫狀態檔，13:40 那班不會去平——部位直接進夜盤。
    """
    waited = []
    broker = _broker(message=message, on_wait=lambda *a, **k: waited.append(1))
    with pytest.raises(FillUnknown):
        broker.place_order(REQUEST)
    # ⚠️ 一起斷言「沒有進入等待」。少了這一條，`message=None` 會靜靜地
    #    走到逾時才拋 FillUnknown——測試照樣綠，但那是**因為別的理由**。
    #    （`str(None)` 是 "None"，一個看起來很正常的非空字串。）
    assert waited == []


def test_a_send_without_a_sequence_number_says_what_to_do():
    """訊息要講「去確認帳戶」，不是講一個看起來像市場沒成交的逾時。"""
    broker = _broker(message="")
    with pytest.raises(FillUnknown, match="人工確認"):
        broker.place_order(REQUEST)


def test_a_send_without_a_sequence_number_never_waits_for_fills():
    """沒有序號就等於沒有過濾條件，等下去只會等到別人的回報。

    ⚠️ 這條也擋住一種「修對了一半」：只在彙整那層擋，`place_order` 仍會
    白等 10 秒，然後報「仍未結束」——那句話聽起來像市場沒成交，
    而真正的問題是我們認不出來。兩者的處理完全不同。
    """
    waited = []
    broker = _broker(message="", on_wait=lambda *a, **k: waited.append(1))
    with pytest.raises(FillUnknown):
        broker.place_order(REQUEST)
    assert waited == [], "不該進入等待迴圈"


# --- 成交查詢的兩段之間必須隔五秒（ticket 09）---
#
# 官方文件明載「限制每次查詢間需間隔五秒」。不隔的話第二次查詢可能直接被拒，
# 而被拒的結果是 None——看起來就像「查不到」，於是這條後備管道靜靜地失效，
# 每天照樣要人介入。那正是本張票要消滅的處境。


class _StubQueryOrder(_StubOrder):
    def __init__(self, order_text="", fulfill_text=""):
        super().__init__()
        self.order_text, self.fulfill_text = order_text, fulfill_text
        self.calls: list = []

    def GetOrderReport(self, user_id, account, fmt):
        self.calls.append("order")
        return self.order_text

    def GetFulfillReport(self, user_id, account, fmt):
        self.calls.append("fulfill")
        return self.fulfill_text


def _query_broker(order_text="", fulfill_text=""):
    broker = _broker()
    broker._order = _StubQueryOrder(order_text, fulfill_text)
    return broker


def _order_row(seq="SEQ0000000001", book="x0582", day=20260819):
    f = [""] * 40
    f[0], f[7], f[8], f[11] = "TF", book, seq, str(day)
    return ",".join(f)


def test_the_two_queries_are_spaced_out():
    """兩次查詢之間要等，而且**等在兩者之間**——等在最後面沒有意義。

    ⚠️ 第一段必須查得到委託書號，否則會提早返回而根本不 sleep，
    這條測試就會因為「沒睡」而失敗（第一版正是如此）。
    """
    broker = _query_broker(order_text=_order_row())
    slept = []
    broker.query_filled_lots(
        order_seq="SEQ0000000001", trading_day=20260819, requested_lots=1,
        sleep=lambda s: slept.append((s, list(broker._order.calls))),
    )
    assert slept, "兩段查詢之間完全沒有間隔"
    seconds, calls_at_that_moment = slept[0]
    assert seconds >= 5.0, f"間隔只有 {seconds} 秒，文件要求五秒"
    assert calls_at_that_moment == ["order"], "要等在兩次查詢之間，不是等在最後"


def test_no_second_query_when_the_first_finds_nothing():
    """第一段查不到委託書號就沒有東西可比——白等五秒還多打一次 API。"""
    broker = _query_broker(order_text="M003")
    broker.query_filled_lots(
        order_seq="SEQ0000000001", trading_day=20260819, requested_lots=1,
        sleep=lambda s: None,
    )
    assert broker._order.calls == ["order"]


def test_a_query_that_raises_returns_unknown_not_an_exception():
    """後備管道自己炸掉的話，原本只是「不確定」的一天會變成整班掛掉。"""
    broker = _broker()

    class _Boom:
        def GetOrderReport(self, *a):
            raise RuntimeError("COM 掛了")

    broker._order = _Boom()
    assert broker.query_filled_lots(
        order_seq="SEQ0000000001", trading_day=20260819, requested_lots=1,
        sleep=lambda s: None,
    ) is None
