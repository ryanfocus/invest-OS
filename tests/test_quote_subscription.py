"""訂閱報價、重新連線、會跑訊息幫浦的時鐘——進場改成開盤前起跑後（ADR-0006）才有的三件事。

群益元件只在真機上才有，所以這裡用替身：`_StubQuote` 記下被呼叫了什麼，
報價事件則像真的一樣**只在跑訊息幫浦時才一批一批進來**——
「訂閱後要等三個商品都到齊才讀」守的正是這個時間差。
"""

import sys
import time
import types

import pytest

from broker import QuoteNotReady
from broker.capital import _STOCKS_READY, CapitalBroker, SystemClock, _QuoteEvents

INDEXES = (0, 7, 18)          # 2026-11-23 實測的三個商品索引（大台、小台、微台）


class _StubQuote:
    """假的報價元件。`enter_codes` 逐次消耗，用完之後一律成功。"""

    def __init__(self, events, *, batches=None, enter_codes=None):
        self.events = events
        self.calls: list = []
        self.pending: list = []                 # 還沒送進來的事件，跑幫浦時才送
        self.batches = batches if batches is not None else [[i] for i in INDEXES]
        self.enter_codes = list(enter_codes or [])

    def SKQuoteLib_EnterMonitorLONG(self):
        self.calls.append("enter")
        code = self.enter_codes.pop(0) if self.enter_codes else 0
        if code == 0:
            self.pending.append(lambda: self.events.OnConnection(_STOCKS_READY, 0))
        return code

    def SKQuoteLib_LeaveMonitor(self):
        self.calls.append("leave")
        return 0

    def SKQuoteLib_RequestStocks(self, page, codes):
        self.calls.append("request")
        for batch in self.batches:
            self.pending.append(
                lambda b=batch: [self.events.OnNotifyQuoteLONG(2, i) for i in b])
        return page, 0


def _broker(**stub_kw) -> CapitalBroker:
    b = CapitalBroker("U1234", "pw")
    b._center = object()
    b._quote_events = _QuoteEvents()
    b._quote = _StubQuote(b._quote_events, **stub_kw)
    b._message = lambda code: f"代碼 {code}"

    def pump_until(predicate, seconds):
        # 每一輪送進一批事件再看條件——跟真的幫浦一樣，「事件進來了才會變」
        while b._quote.pending:
            b._quote.pending.pop(0)()
            if predicate():
                return True
        return predicate()

    b._pump_until = pump_until
    b._pump_for = lambda seconds: pump_until(lambda: False, seconds)
    return b


# --- 訂閱：等三個商品都到齊 ---


def test_subscribing_waits_until_all_three_products_have_reported():
    """只等「任一個」商品的話，另外兩個的開盤價會是 0——2026-11-23 以前 08:50
    常常第一次讀到 0，就是這個原因（同一時刻探測程式 08:45:00 就讀得到）。
    """
    b = _broker()
    b.subscribe_quotes()
    assert b._quote_events.updated_indexes == set(INDEXES)


def test_a_product_that_never_reports_does_not_fail_the_subscription():
    """開盤前有的商品可能就是還沒更新——那不算失敗，讀開盤價時會重試。"""
    b = _broker(batches=[[0], [7]])
    b.subscribe_quotes()                     # 不拋例外
    assert b._subscribed


def test_subscribing_twice_does_not_request_again():
    b = _broker()
    b.subscribe_quotes()
    b.subscribe_quotes()
    assert b._quote.calls.count("request") == 1


# --- 重新連線 ---


def test_reconnecting_leaves_reconnects_and_resubscribes_from_a_clean_slate():
    """重連前收到過的報價不可以算數——斷線那一刻的舊資料正是要甩掉的東西。"""
    b = _broker()
    b.subscribe_quotes()
    b._quote_events.updated_indexes.add(99)            # 斷線前留下的舊東西
    b.reconnect_quotes()
    assert b._quote.calls == ["enter", "request", "leave", "enter", "request"]
    assert b._quote_events.updated_indexes == set(INDEXES)
    assert b._subscribed


def test_a_failed_reconnect_raises_quote_not_ready_and_stays_unsubscribed():
    """連不上要讓呼叫端知道（它會繼續照時間表重試），而且不可以假裝已經訂閱好了。"""
    b = _broker(enter_codes=[0, 1001])                 # 第一次連得上，重連時連不上
    b.subscribe_quotes()
    with pytest.raises(QuoteNotReady):
        b.reconnect_quotes()
    assert not b._subscribed


# --- 事件接收端的方法不可以跑到別的類別去 ---


def test_every_com_event_handler_lives_on_an_events_class():
    """群益的 COM 事件是**照方法名稱**送到接收端的。處理函式不在接收端類別上，
    事件就安靜地沒人接——不會報錯，只會在真機上「等不到」。

    2026-11-23 真的發生過：把 `SystemClock` 插進 `_QuoteEvents` 中間，
    `OnNotifyCommodityListWithTypeNo` 就變成 `SystemClock` 的方法。全部測試綠燈，
    是當晚用真元件試跑時「商品清單 0 個片段」才抓到。
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parent.parent / "broker" / "capital.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    misplaced = [
        f"{cls.name}.{fn.name}"
        for cls in tree.body if isinstance(cls, ast.ClassDef)
        for fn in cls.body
        if isinstance(fn, ast.FunctionDef) and fn.name.startswith("On")
        and not cls.name.endswith("Events")
    ]
    assert misplaced == [], f"這些事件處理函式不在接收端類別上：{misplaced}"


def test_the_quote_events_class_handles_every_event_the_broker_waits_for():
    """報價那一側等的三種事件：連線狀態、報價更新、商品清單。少一個就有一件事永遠等不到。"""
    for name in ("OnConnection", "OnNotifyQuoteLONG", "OnNotifyCommodityListWithTypeNo"):
        assert callable(getattr(_QuoteEvents, name, None)), f"_QuoteEvents 少了 {name}"


# --- 正式環境的時鐘 ---


def test_the_system_clock_keeps_pumping_com_messages_while_it_sleeps(monkeypatch):
    """開盤前那一分多鐘若完全不處理 COM 事件，報價會在佇列裡堆到讀價那一刻才湧進來。"""
    pumped = []
    monkeypatch.setitem(sys.modules, "pythoncom",
                        types.SimpleNamespace(PumpWaitingMessages=lambda: pumped.append(1)))
    started = time.monotonic()
    SystemClock().sleep(0.12)
    assert time.monotonic() - started >= 0.12
    assert len(pumped) >= 2, "整段睡眠只處理了一次事件"


def test_the_system_clock_does_not_sleep_for_zero_or_negative(monkeypatch):
    pumped = []
    monkeypatch.setitem(sys.modules, "pythoncom",
                        types.SimpleNamespace(PumpWaitingMessages=lambda: pumped.append(1)))
    SystemClock().sleep(0)
    SystemClock().sleep(-5)
    assert pumped == []
