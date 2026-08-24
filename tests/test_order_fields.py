"""委託參數 —— 送給群益的每一個欄位值。

期望值來自 [ADR-0003](../docs/adr/0003-market-ioc-orders.md) 與官方文件的
`FUTUREORDER` 結構定義，不是從程式反推的。這些欄位寫錯的後果各不相同但都很貴：

  時效寫成 ROD → 掛單整天，不是「立即成交否則取消」，收盤前都可能被成交
  買賣別寫反   → 開出完全相反的部位
  標記當沖     → 稅率與部位處理都不同，也可能被拒

這一層在真機上驗不到的只剩「群益收下之後怎麼解讀」，那是 ticket 04 最後一條。
"""

from broker import BUY, ENTRY, EXIT, MTX_CODE, SELL, OrderRequest
from broker.capital_wire import build_future_order_fields

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


# --- 倉別：2026-08-21 實機驗證（但只驗到一半）---
#
# 實機里程碑：1 口微台完整來回。進場送 0、出場送 2，兩筆都成功，
# 帳戶由 1 口 → 2 口 → 1 口。成交查詢的倉別欄位回報 `N`（進場）與 `O`（出場），
# 也就是券商**沒有把出場當成新倉**。
#
# ⚠️ **只驗到「同方向持倉」那一半。** 使用者當時持有多單、OS 也做多，
#    全程沒有跨越零——而跨越零正是選「自動」的**唯一理由**（見下面第二條）。
#    那一格要等訊號方向與使用者持倉相反的那天才碰得到。


def test_entry_uses_auto_so_the_order_can_cross_zero():
    """sNewClose：0 新倉、1 平倉、2 自動。**進場送 2，不是 0。**

    ❌ **2026-08-24 實機退單**：使用者持有 1 口 TM2609 多單、訊號做空，
    OS 送出「賣 1 口、新倉」，券商回：

        代碼 980：[980] 勾選新倉而有對應反向部位,退單!

    台指期同帳號同商品同月份採**淨額計算**——賣掉那 1 口只是把使用者的多單
    收掉，生不出任何新部位，所以「新倉」在語意上就是假的。

    這推翻的不只是一個參數，是 SPEC 部位隔離那張策略作者確認過的表：
    它要求 08:50 的賣單能從 +1 跨到 −2。**「新倉」跨不過零。**
    留著 0 的話，使用者手上只要有反向部位，每個反向訊號日都會被退單。

    ⚠️ 這一格改成 2 之後**仍未實機驗證**。今天證明的是「新倉」會被拒，
       不是「自動」會成功——那是兩件事，要等下一個反向訊號日。
    """
    assert _fields()["sNewClose"] == 2


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
    ✅ **2026-08-21 實機確認**：帳上 2 口多單時送出 `sNewClose=2` 賣 1 口，
    成交 1 口（序號 2315609905580，成交價 45135），帳戶回到 1 口。
    成交查詢的倉別欄位回報 `O`，與進場那筆的 `N` 不同——
    **券商沒有把它當成新倉**，也就是上面那個「代價」沒有發生。

    ⚠️ **但那次沒有跨越零**（帳上 2 口，只賣 1 口）。而跨越零正是選「自動」
    的唯一理由，也是「代價」會兌現的唯一場合——**這一條的核心論證仍未實測**。
    別因為 2026-08-21 順利就把它當成定案。
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


def test_entry_and_exit_now_send_identical_fields():
    """**2026-08-24 起兩邊完全一樣**——包含倉別。

    在那之前唯一的差別是 `sNewClose`（進場 0、出場 2），而那個差別是
    980 退單的成因：進場的「新倉」跨不過零。兩邊都改成「自動」之後，
    這張表就沒有任何一格會因為 `intent` 而不同了。

    ⚠️ 這條會紅代表有人讓兩邊又分岔了。分岔本身不是錯（出場那格的取捨
       隨時可能再變），但**必須是刻意的**——上一次不刻意的分岔花了三次
       實機測試才發現。
    """
    entry, exit_ = _fields(intent=ENTRY), _fields(intent=EXIT)
    differing = {k for k in entry if entry[k] != exit_[k]}
    assert differing == set(), f"進出場又分岔了：{differing}"
