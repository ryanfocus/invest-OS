"""期交所取數 —— 對帳唯一的第三方真相。

HTTP 那一層用 monkeypatch 置換（SPEC 的接縫表：「外部 HTTP，比照 invest-hm
crawler 的 monkeypatch 手法」）。這裡測的是**組裝與把關**，不是連線本身：
連線是否成功由實機驗證，而下面這幾條連線正常時反而最危險——
它們守的是「拿到了東西，但那個東西不對」。
"""

import pytest

import taifex


def _ohlc(date_str: str, open_price: float) -> dict:
    return {"date": date_str, "month": "202608", "open": open_price,
            "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0.0}


def _stub(monkeypatch, by_commodity):
    def fake(commodity, date_str, **_):
        value = by_commodity.get(commodity)
        return value(date_str) if callable(value) else value
    monkeypatch.setattr(taifex, "fetch_ohlc", fake)


def test_all_three_products_are_returned(monkeypatch):
    _stub(monkeypatch, {
        "TX": _ohlc("2026/08/17", 45850.0),
        "MTX": _ohlc("2026/08/17", 45812.0),
        "TMF": _ohlc("2026/08/17", 45863.0),
    })
    assert taifex.fetch_official_opens(20260817) == {
        "tx": 45850.0, "mtx": 45812.0, "tmf": 45863.0}


def test_a_missing_product_makes_the_whole_day_unavailable(monkeypatch):
    """**不可以只回兩個商品。**

    拿到兩個時無從判斷是「期交所還沒公布完」還是「格式變了」。
    前者該安靜跳過、後者該修程式——而只比兩個商品會讓後者看起來像前者。
    """
    _stub(monkeypatch, {
        "TX": _ohlc("2026/08/17", 45850.0),
        "MTX": None,
        "TMF": _ohlc("2026/08/17", 45863.0),
    })
    assert taifex.fetch_official_opens(20260817) is None


def test_data_for_the_wrong_day_is_refused(monkeypatch):
    """**查詢參數說要哪一天，回傳列也要真的是那一天。**

    只信參數的話，期交所改行為（忽略範圍、回最近一個交易日）時我們會拿
    別天的資料去跟這天的觀測比對——報出一個**看起來完全真實**的不一致，
    然後人會去查一個根本沒問題的日子。連線正常時這種錯最難察覺。
    """
    _stub(monkeypatch, {
        "TX": _ohlc("2026/08/14", 45850.0),      # ← 回了上一個交易日
        "MTX": _ohlc("2026/08/17", 45812.0),
        "TMF": _ohlc("2026/08/17", 45863.0),
    })
    assert taifex.fetch_official_opens(20260817) is None


def test_the_query_date_is_formatted_the_way_taifex_wants(monkeypatch):
    """`20260801` → `2026/08/01`。少補一個零就查不到，而查不到會被
    當成「還沒公布」而安靜跳過——對帳於是每天都沒跑。"""
    seen = []

    def fake(commodity, date_str, **_):
        seen.append(date_str)
        return _ohlc(date_str, 1.0)

    monkeypatch.setattr(taifex, "fetch_ohlc", fake)
    taifex.fetch_official_opens(20260801)
    assert seen == ["2026/08/01"] * 3
