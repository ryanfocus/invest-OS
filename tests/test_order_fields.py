"""委託參數 —— 送給群益的每一個欄位值。

期望值來自 [ADR-0003](../docs/adr/0003-market-ioc-orders.md) 與官方文件的
`FUTUREORDER` 結構定義，不是從程式反推的。這些欄位寫錯的後果各不相同但都很貴：

  時效寫成 ROD → 掛單整天，不是「立即成交否則取消」，收盤前都可能被成交
  買賣別寫反   → 開出完全相反的部位
  標記當沖     → 稅率與部位處理都不同，也可能被拒

這一層在真機上驗不到的只剩「群益收下之後怎麼解讀」，那是 ticket 04 最後一條。
"""

from broker import BUY, MTX_CODE, SELL, OrderRequest
from broker.capital_wire import build_future_order_fields

REQUEST = OrderRequest(
    product=MTX_CODE, order_code="MTX08", contract_month="202608",
    side=BUY, lots=2,
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


def test_every_order_uses_auto_so_it_can_cross_zero():
    """sNewClose：0 新倉、1 平倉、2 自動。**進場與出場都送 2。**

    ⚠️ 曾經是「進場 0、出場 2」。那個分岔是不刻意的，花了三次實機測試才發現。

    ══ 出場為什麼不能用「平倉」(1) ══

    SPEC 部位隔離那張策略作者確認過的表：使用者固定持有 +1、OS 做空 3 口，

        08:50  OS 賣 3    +1 − 3 = −2
        13:40  OS 買回 3  −2 + 3 = +1

    出場那筆買 3 口時帳上只有 **2 口空單**。「平倉」的語意是「平掉 N 口」，
    做不到跨越零——用它的話，這個規格確認過的情境每天都會失敗。

    ══ 進場為什麼不能用「新倉」(0) ══

    ❌ **2026-08-24 實機退單**：使用者持有 1 口 TM2609 多單、訊號做空，
    OS 送出「賣 1 口、新倉」：

        代碼 980：[980] 勾選新倉而有對應反向部位,退單!

    淨額計算下，賣掉那 1 口只是把使用者的多單收掉，生不出任何新部位——
    「新倉」在語意上就是假的。同一張表要求 08:50 的賣單能從 +1 跨到 −2，
    而**「新倉」跨不過零**。

    ══ 三次實機確認 ══

    ✅ 2026-08-21 出場：帳上 2 口多單賣 1 口，成交（@45135），倉別回 `O`
       ⚠️ 那次沒有跨越零
    ✅ 2026-08-24 進場：跨越零，成交（@45245），倉別回 `O`——真的平了倉，
       帳戶 1 口 → 0 口
    ✅ 2026-08-24 出場：帳上 0 口，成交（@44680），倉別回 `N`——開出新倉，
       把使用者原本那口還回去

    ⚠️ **代價是真的，只是換了受害情境。** 「自動」在 OS 的部位已經不在時
    會開出反向新倉（8/24 下午那筆就是），而在跨越零的往返裡那剛好是正確行為。
    使用者中途手動平掉 OS 部位的話，同樣的行為就是錯的——那個情境現在由
    「進出場的倉別必須相反」檢查來偵測（見 main.run_exit）。

    📌 兩邊都是 2 之後，`OrderRequest` 的 `intent` 欄位就不影響任何東西，
       2026-08-25 拿掉。要讓兩邊再分岔得重新加回那個參數，而那是看得見的動作。
    """
    assert _fields()["sNewClose"] == 2
