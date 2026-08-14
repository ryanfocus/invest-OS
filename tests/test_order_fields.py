"""委託參數 —— 送給群益的每一個欄位值。

期望值來自 [ADR-0003](../docs/adr/0003-market-ioc-orders.md) 與官方文件的
`FUTUREORDER` 結構定義，不是從程式反推的。這些欄位寫錯的後果各不相同但都很貴：

  時效寫成 ROD → 掛單整天，不是「立即成交否則取消」，收盤前都可能被成交
  買賣別寫反   → 開出完全相反的部位
  標記當沖     → 稅率與部位處理都不同，也可能被拒

這一層在真機上驗不到的只剩「群益收下之後怎麼解讀」，那是 ticket 04 最後一條。
"""

from broker import BUY, MTX_CODE, SELL, OrderRequest
from broker.capital import build_future_order_fields

REQUEST = OrderRequest(
    product=MTX_CODE, order_code="MTX08", contract_month="202608", side=BUY, lots=2
)
ACCOUNT = "F9990001234567"


def _fields(**overrides):
    from dataclasses import replace
    return build_future_order_fields(replace(REQUEST, **overrides), ACCOUNT)


# --- ADR-0003：市價、IOC、不標當沖 ---


def test_price_is_market():
    """群益用 bstrPrice = "M" 表示市價（官方文件：「可用「M」表示市價」）。"""
    assert _fields()["bstrPrice"] == "M"


def test_time_in_force_is_ioc():
    """0:ROD 1:IOC 2:FOK。

    IOC 是刻意的：開盤瞬間要嘛成交要嘛放棄，不要一張掛整天的單。
    官方文件也註明市價只能搭配 IOC 或 FOK。
    """
    assert _fields()["sTradeType"] == 1


def test_the_order_is_not_marked_as_day_trade():
    """0:否 1:是。ticket 04 明列不標當沖。

    這個策略確實當天進出，但「標記當沖」是券商的稅務與部位處理選項，
    不是在描述交易行為——標了會走不同的處理路徑。
    """
    assert _fields()["sDayTrade"] == 0


def test_the_order_is_an_intraday_order_not_a_reserved_one():
    """0:盤中（T盤及T+1盤）1:T盤預約。08:50 送的是盤中單。"""
    assert _fields()["sReserved"] == 0


# --- 買賣別 ---


def test_buy_maps_to_zero():
    """群益的 sBuySell：0 買進、1 賣出。"""
    assert _fields(side=BUY)["sBuySell"] == 0


def test_sell_maps_to_one():
    assert _fields(side=SELL)["sBuySell"] == 1


# --- 商品與口數 ---


def test_the_wire_uses_the_order_code_not_the_quote_code():
    """bstrStockNo 要帶下單代碼。報價代碼（MTX00AM）送過去會被拒。"""
    assert _fields()["bstrStockNo"] == "MTX08"


def test_quantity_comes_from_the_request():
    assert _fields(lots=5)["nQty"] == 5


def test_account_is_passed_through():
    assert _fields()["bstrFullAccount"] == ACCOUNT


# --- 倉別：唯一還沒定案的欄位 ---


def test_position_type_is_new_open():
    """sNewClose：0 新倉、1 平倉、2 自動。目前送 0。

    ⚠️ **這個值尚未實機驗證**（ticket 04 最後一條）。台指期同帳號同商品同月份
    是淨額計算，已有反向部位時「新倉」可能被拒。這條測試不是在說 0 是對的，
    是在確保它**沒有被隨手改掉**——真要改，得帶著實機證據一起改。
    """
    assert _fields()["sNewClose"] == 0
