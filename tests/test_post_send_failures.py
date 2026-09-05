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

import os

import pytest

from broker import BUY, FillUnknown, MTX_CODE, OrderFailed, OrderRequest
from broker.capital import CapitalBroker

# 正式的 logs/ 路徑，在 conftest 的 fixture 導開之前就先算好——
# 直接讀 housekeeping.LOGS_PATH 的話拿到的是已經被導開的值。
_REAL_LOGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

def test_saving_a_reply_never_touches_the_real_logs_directory():
    """存下原始回覆這個動作，**絕不可以碰到正式的 `logs/`**。

    那個目錄是真實交易的鑑識記錄。2026-08-23 發現裡面積了 47 個
    `SEQ0000000001` 的假回覆檔——全是測試留下的，跑一次 pytest 就多一個，
    而且與真的券商回覆同一種檔名格式，肉眼分不出來。

    ⚠️ 這條同時斷言**存檔真的發生了**。少了那一半，`_save_raw_replies`
       哪天不再存檔、或這個測試沒走到存檔那條路，它都會靜靜地一直綠——
       一條永遠不會紅的測試比沒有測試更糟。
    """
    filled_row = [""] * 49
    filled_row[0] = filled_row[47] = "SEQ0000000001"
    filled_row[1], filled_row[2] = "TF", "N"
    filled_row[3], filled_row[20] = "Y", "1"

    def _listing(directory):
        return set(os.listdir(directory)) if os.path.isdir(directory) else set()

    before = _listing(_REAL_LOGS_PATH)

    broker = None

    def _reply_arrives(predicate, seconds):
        broker._reply_events.rows.append(",".join(filled_row))
        return predicate()

    broker = _broker(on_wait=_reply_arrives)
    broker._reply_events.rows = []
    with pytest.raises(OrderFailed):
        broker.place_order(REQUEST)

    assert _listing(_REAL_LOGS_PATH) == before, (
        f"測試在正式的 logs/ 留下了 {_listing(_REAL_LOGS_PATH) - before}"
    )
    import housekeeping
    saved = [f for f in _listing(housekeeping.LOGS_PATH) if f.startswith("replies-")]
    assert saved, "根本沒有存檔——這條測試沒有走到它要守的那條路"


REQUEST = OrderRequest(
    product=MTX_CODE, order_code="MTX08", contract_month="202608",
    side=BUY, lots=1,
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
    broker = CapitalBroker("id", "pw", account="F9990001234567")
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


# --- 下單前必須讀憑證（2026-08-21 實機里程碑抓到）---
#
# 第一次真的送出委託時被擋下：代碼 1038 SK_ERROR_CERT_NOT_VERIFIED。
#
# 憑證本身完全正常——裝了、沒過期（剩 351 天）、有私鑰、CN 與登入 ID 相符、
# 而且電腦裡只有一張群益憑證。問題在**程式漏了一步**：
# `SKOrderLib_Initialize` 之後必須 `ReadCertByID(user_id)` 把憑證載進來。
#
# 看報價、查商品清單、查委託成交都不需要憑證，只有**送出委託**需要——
# 所以這個洞在真的下單之前不可能被發現。這正是實機里程碑存在的理由，
# 而且它零成本就抓到了：委託在送出那一步就被擋下，沒有部位產生。


class _StubOrderWithCert(_StubOrder):
    """記錄下單元件上被呼叫過的方法與順序。"""

    def __init__(self, cert_code=0):
        super().__init__()
        self.calls: list = []
        self._cert_code = cert_code

    def SKOrderLib_Initialize(self):
        self.calls.append("init")
        return 0

    def ReadCertByID(self, user_id):
        self.calls.append(f"cert:{user_id}")
        return self._cert_code

    def GetUserAccount(self):
        self.calls.append("account")
        return 0


def _cert_broker(cert_code=0):
    broker = CapitalBroker("U1234", "pw", account="F9990001234567")
    broker._center = object()
    broker._order = _StubOrderWithCert(cert_code)
    broker._reply = type("R", (), {"SKReplyLib_ConnectByID": lambda self, uid: 0})()
    broker._reply_events = type("E", (), {"connected": True, "connect_error": ""})()
    broker._sk = _StubSk()
    broker._message = lambda code: f"代碼 {code}"
    broker._pump_for = lambda seconds: None
    return broker


def test_the_certificate_is_read_before_ordering():
    """**沒有這一步，送出委託會被回 1038。**"""
    broker = _cert_broker()
    broker._ensure_order_ready()
    assert "cert:U1234" in broker._order.calls


def test_the_certificate_is_read_after_the_order_lib_is_initialised():
    """順序有意義：元件還沒初始化就讀憑證，讀不到東西。"""
    broker = _cert_broker()
    broker._ensure_order_ready()
    calls = broker._order.calls
    assert calls.index("init") < calls.index("cert:U1234")


def test_a_failed_certificate_read_stops_before_any_order():
    """**憑證讀不到就別送單。**

    送了也只會被回 1038，而那個錯誤訊息（「Cert Not Verified」）
    要人自己去猜是哪一步漏了——2026-08-21 那次就花了時間才定位到。
    在這裡失敗，訊息可以直接說「憑證讀取失敗」。
    """
    broker = _cert_broker(cert_code=1038)
    with pytest.raises(OrderFailed, match="憑證"):
        broker._ensure_order_ready()


def test_the_certificate_is_not_read_twice():
    """`_ensure_order_ready` 會被重複呼叫（進場一次、查詢一次）。

    憑證讀取要走網路驗證，重複做只是拖時間——而 08:50 的每一秒都在
    逼近開盤價的有效窗口。
    """
    broker = _cert_broker()
    broker._ensure_order_ready()
    broker._ensure_order_ready()
    assert broker._order.calls.count("cert:U1234") == 1


# --- 保存券商回覆的原始內容（2026-08-21 缺口三）---
#
# 券商成交後會回一段訊息。程式從裡面挑出「成交幾口」就把整段丟掉了，
# 於是：
#
#   1. 出事時沒有原始資料可以回頭看
#   2. ticket 04／06 的「平倉回報原始字串」永遠拿不到——再下十次真單也一樣
#
# 存的是**視窗內收到的全部內容**，不只我們自己那筆。2026-08-19 就是靠
# 「別人的單也在同一條線上」這個證據，才發現序號比對的漏洞。


class _RecordingReplies:
    def __init__(self, rows=None):
        self.rows = list(rows or [])


def _reply_row(seq="SEQ0000000001", row_type="D", qty="1", tail="x"):
    """一列格式合法的 TF 回報。太短的列會被解析器當成看不懂而跳過，
    於是委託永遠不會「結束」——第一版的測試就是栽在這裡。"""
    f = [""] * 49
    f[0], f[1], f[2], f[3] = seq, "TF", row_type, "N"
    f[20] = qty
    f[47] = seq
    f[48] = tail
    return ",".join(f)


def _saving_broker(tmp_path, *, rows=(), message="SEQ0000000001"):
    """回覆在**送單之後**才到。

    ⚠️ 第一版把回覆預先放進去，結果全部被 `since` 跳過（那個位移正是
    為了「只算我們送單之後收到的」而存在的）。回覆改由訊息幫浦在
    等待期間注入，才是真實的時序。

    委託會不會「結束」由 `rows` 的內容決定：湊得滿委託口數就結束，
    湊不滿就走 FillUnknown——兩種都要存下原始回覆。
    """
    broker = _broker(message=message)
    broker._reply_events = _RecordingReplies()
    broker._replies_dir = str(tmp_path)

    def pump(predicate, timeout):
        broker._reply_events.rows.extend(rows)

    broker._pump_until = pump
    return broker


def _saved(tmp_path):
    return sorted(p for p in os.listdir(str(tmp_path)) if "reply" in p or "replies" in p)


def test_the_saved_file_holds_every_row_in_the_window(tmp_path):
    """**存整個視窗，不只自己那筆。**

    2026-08-19 的換倉價差單就出現在 OS 自己的回報串流上——那份證據
    是靠「別人的單也在」才成立的。只存自己那筆就看不到了。
    """
    ours = _reply_row(tail="ours")
    theirs = _reply_row(seq="OTHERSEQ", tail="someone-else")
    broker = _saving_broker(tmp_path, rows=[ours, theirs])
    broker.place_order(REQUEST)
    files = _saved(tmp_path)
    assert files, "應該要有一個存檔"
    text = open(os.path.join(str(tmp_path), files[0]), encoding="utf-8").read()
    assert ours in text and theirs in text


def test_rows_that_arrived_before_our_order_are_not_saved(tmp_path):
    """**只存我們送單之後那段視窗。**

    回報頻道是共用的：登入之後、送單之前，別人的成交就可能已經流進來了。
    那些與這筆委託無關，存進來只會讓日後排查的人以為它們有關聯。

    突變測試抓到：把 `rows[since:]` 改成 `rows`，全部測試照樣綠——
    因為那時每條測試的緩衝區都是從空的開始，`since` 剛好是 0。
    """
    earlier = _reply_row(seq="BEFORE_OUR_ORDER", tail="not-ours")
    broker = _saving_broker(tmp_path, rows=[_reply_row()])
    broker._reply_events.rows.append(earlier)      # 送單之前就在緩衝區裡
    broker.place_order(REQUEST)
    files = _saved(tmp_path)
    text = open(os.path.join(str(tmp_path), files[0]), encoding="utf-8").read()
    assert "BEFORE_OUR_ORDER" not in text, "送單前就在的回覆不屬於這一筆"
    assert "SEQ0000000001" in text


def test_the_filename_carries_the_order_sequence(tmp_path):
    """找得到才有用。序號是把檔案與那筆委託對起來的唯一線索。"""
    broker = _saving_broker(tmp_path, rows=[_reply_row()])
    broker.place_order(REQUEST)
    assert any("SEQ0000000001" in f for f in _saved(tmp_path))


def test_replies_are_saved_even_when_the_fill_is_unknown(tmp_path):
    """**收不到回報那天最需要原始資料**——那正是要回頭查的時候。

    只在成功時存的話，能查的都是不必查的。
    """
    # 只有別人的回覆進來，我們那筆永遠湊不滿 → FillUnknown
    broker = _saving_broker(tmp_path, rows=[_reply_row(seq="OTHERSEQ")])
    with pytest.raises(FillUnknown):
        broker.place_order(REQUEST)
    assert _saved(tmp_path), "不確定的那天也要留下證據"


def test_a_failed_save_does_not_mask_the_real_exception(tmp_path):
    """**這是本組最重要的一條。**

    存檔跑在 `finally` 裡，而 `finally` 拋出的例外會**取代**正在傳遞的那個。
    所以存檔一旦出錯，使用者收到的會是「寫檔失敗」而不是「收不到成交回報」——
    真正該處理的問題被蓋掉，而且蓋得無聲無息。
    """
    broker = _saving_broker(tmp_path, rows=[_reply_row(seq="OTHERSEQ")])
    broker._replies_dir = str(tmp_path / "blocked" / "deeper")
    (tmp_path / "blocked").write_text("我是檔案不是目錄", encoding="utf-8")
    with pytest.raises(FillUnknown):        # 不是 OSError／NotADirectoryError
        broker.place_order(REQUEST)


def test_a_failed_save_does_not_turn_a_good_order_into_a_failure(tmp_path):
    """委託成交了就是成交了。存檔失敗只是少一份參考資料，
    不該讓一筆成功的下單變成錯誤——那會讓 13:40 去平一個記錄不存在的部位。"""
    broker = _saving_broker(tmp_path, rows=[_reply_row()])
    broker._replies_dir = str(tmp_path / "blocked" / "deeper")
    (tmp_path / "blocked").write_text("我是檔案不是目錄", encoding="utf-8")
    broker.place_order(REQUEST)     # 不該拋
