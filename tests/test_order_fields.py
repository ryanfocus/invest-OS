"""委託參數 —— 送給群益的每一個欄位值。

期望值來自 [ADR-0003](../docs/adr/0003-market-ioc-orders.md) 與官方文件的
`FUTUREORDER` 結構定義，不是從程式反推的。這些欄位寫錯的後果各不相同但都很貴：

  時效寫成 ROD → 掛單整天，不是「立即成交否則取消」，收盤前都可能被成交
  買賣別寫反   → 開出完全相反的部位
  標記當沖     → 稅率與部位處理都不同，也可能被拒

這一層在真機上驗不到的只剩「群益收下之後怎麼解讀」，那是 ticket 04 最後一條。
"""

from broker import BUY, ENTRY, EXIT, MTX_CODE, SELL, OrderRequest
from broker.capital import build_future_order_fields

REQUEST = OrderRequest(
    product=MTX_CODE, order_code="MTX08", contract_month="202608",
    side=BUY, lots=2, intent=ENTRY,
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


def test_entry_position_type_is_new_open():
    """sNewClose：0 新倉、1 平倉、2 自動。進場目前送 0。

    ⚠️ **這個值尚未實機驗證**（ticket 04 里程碑 1）。台指期同帳號同商品同月份
    是淨額計算，已有反向部位時「新倉」可能被拒。這條測試不是在說 0 是對的，
    是在確保它**沒有被隨手改掉**——真要改，得帶著實機證據一起改。
    """
    assert _fields()["sNewClose"] == 0


def test_exit_uses_auto_not_close_because_the_order_must_be_able_to_cross_zero():
    """出場送「自動」(2)，不是「平倉」(1)。

    理由來自 SPEC 部位隔離那張策略作者確認過的表：使用者固定持有 +1、OS 做空 3 口，

        08:50  OS 賣 3    +1 − 3 = −2
        13:40  OS 買回 3  −2 + 3 = +1

    出場那筆買 3 口時，帳上只有 **2 口空單**。「平倉」平不掉 3 口——
    它做不到跨越零。用「平倉」的話，這個**策略作者親自確認過的情境每天都會失敗**。
    「自動」由券商拆成平倉 2 ＋ 新倉 1，才得到表上那個 +1。

    ⚠️ 代價要講清楚：若 OS 的部位已經不在了（例如使用者自己手動平掉），
    「自動」會開出一個反向新倉，而「平倉」會被拒。這是真實的取捨，
    選「自動」是因為上面那個情境是**規格確認過的常態**，而部位被手動平掉是例外。
    仍待里程碑 2 實機驗證。
    """
    assert _fields(intent=EXIT)["sNewClose"] == 2


def test_intent_cannot_be_omitted():
    """**忘記填 intent 必須是錯誤，不可以靜默變成新倉。**

    給預設值的話，出場那條路徑少傳一個參數就會送出「新倉」的平倉單——
    那正是 ticket 06 開頭警告的「平倉單被當成新倉，部位不減反增」。
    這種錯誤不會報錯、不會有錯誤訊息，只會讓部位默默變成兩倍。
    """
    import pytest
    with pytest.raises(TypeError):
        OrderRequest(product=MTX_CODE, order_code="MTX08",
                     contract_month="202608", side=BUY, lots=1)


def test_entry_and_exit_differ_only_in_position_type():
    """其餘欄位（市價、IOC、不標當沖、盤中單）兩邊完全一樣。"""
    entry, exit_ = _fields(intent=ENTRY), _fields(intent=EXIT)
    differing = {k for k in entry if entry[k] != exit_[k]}
    assert differing == {"sNewClose"}
