"""群益的**線路格式** —— 從券商回來的那些字串長什麼樣。

這裡沒有一行碰 COM。它是純資料處理：吃字串、吐結構。

## 為什麼獨立成一個檔案（2026-08-23 架構檢視）

它**本來就已經是一個獨立的模組了，只是沒有檔案**——證據是它早就有
COM 以外的呼叫者：`tools/verify_order_path.py` 直接 import 這裡的解析函式
而完全不建 `CapitalBroker`，另有七個測試檔同樣繞過它。

搬出來之前，這些函式與 400 行 COM 生命週期擠在同一個檔案：欄位常數在 106–120 行，
用它們的函式在 334／398／487 行，中間隔著整個狀態機。
`summarize_fills` 甚至定義在它呼叫的 `parse_reply_row` **之前**。

搬出來之後，格式漂移時要改的東西——常數、解析、fixture、測試——落在同一個地方。

## 三種**互不相容**的格式

同一家券商，同一筆委託，三種格式：

| 來源 | 通道 | 委託序號在哪 |
|------|------|------------|
| `OnNewData` | Solace 推播，一次性 | `[0]` 與 `[47]` |
| `GetOrderReport` | 請求／回應 | `[8]` 與 `[9]` |
| `GetFulfillReport` | 請求／回應 | **沒有**（`[8]` 是成交編號）|

最後那一格是 2026-08-19 實測發現的，而它推翻了 ticket 09 原本的做法
（「查一次成交就好」認不出哪幾列是自己的）。串得起後兩者的只有**委託書號**。

⚠️ 商品代碼也有三種寫法，跨格式比對一定失敗：
報價 `TM0000AM`、下單／推播 `TM2608`、查詢回傳 `TMFH6`。

## 為什麼這裡處處在「拒絕回答」

回報頻道是**共用**的：使用者手動下的單就在同一份資料裡（2026-08-19 換倉實測證實）。
認錯的後果是把別人的成交寫進自己的狀態檔 → 13:40 照那個口數平倉 →
多平的部分變成方向相反、沒人管的新倉。

而回不出答案的代價只是「維持不確定」——發個 Discord 要人看一眼帳戶。
兩者差了一個數量級，所以**只要有一絲不確定就回 None**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from broker import (
    BUY,
    ENTRY,
    EXIT,
    PRODUCT_CODES,
    ContractInfo,
    OrderRequest,
    ProductListUnavailable,
)

logger = logging.getLogger(__name__)


# FUTUREORDER 的欄位值（官方文件 5 章的結構定義）
_TRADE_TYPE_IOC = 1        # 0:ROD 1:IOC 2:FOK
_BUY, _SELL = 0, 1         # sBuySell
_DAY_TRADE_NO = 0          # 不標記當沖
_SESSION_INTRADAY = 0      # sReserved：0 盤中（T盤及T+1盤）、1 T盤預約
_MARKET_PRICE = "M"        # bstrPrice：「M」市價、「P」範圍市價；限 IOC/FOK


# 倉別 sNewClose：0 新倉、1 平倉、2 自動。**進出場都送 2（自動）。**
#
# ══ 2026-08-24 實機定案 ══
#
# 進場原本送 0（新倉），當天被**退單**：
#
#     代碼 980：[980] 勾選新倉而有對應反向部位,退單!
#
# 情境：使用者持有 1 口 TM2609 多單，訊號做空，OS 送出「賣 1 口、新倉」。
# 台指期同帳號同商品同月份採**淨額計算**，賣掉那 1 口只會把使用者的多單收掉，
# 生不出任何新部位——券商因此拒絕。
#
# 這推翻的不只是一個參數，是 SPEC「部位隔離」那張策略作者確認過的表：
#
#     開盤前  使用者固定持有        +1
#     08:50   OS 賣 3     +1 − 3 = −2
#     13:40   OS 買回 3   −2 + 3 = +1
#
# 那張表**要求進場的賣單能跨越零**，而「新倉」跨不過去。用 0 的話，
# 只要使用者手上有反向部位，策略每一個反向訊號日都會被退單。
#
# 所以進場改成 2，與出場一致。這是這段註解原本就預先登記好的動作
# （「已有反向部位時『新倉』可能被拒；若實測如此改用 2」）。
#
# ⚠️ **代價沒有消失，只是換了形狀。** 「自動」在 OS 的部位已經不在時
#    （使用者自己手動平掉、或前一天沒收掉而被今天的單吃掉）會**安靜地**
#    開出反向新倉，而「新倉」會大聲退單。980 那次退單其實意外幫我們擋住了
#    這件事——現在那個保護沒了，改由 `main` 在進場前檢查狀態檔裡有沒有
#    前一交易日未出場的記錄，有就發 Discord 提醒（**只提醒，不擋交易**：
#    多一口沒人管的部位不會讓今天的交易變錯，只會讓它沒人知道）。
#
# ⚠️ **進場的「自動」尚未實機驗證。** 今天證明的是「新倉」會被拒，
#    不是「自動」會成功——那是兩件事。
#
# 📌 `intent` 目前只影響這張表，而表的兩格已經一樣了。保留它是因為
#    那兩筆委託的用途本來就不同，而且出場那格的取捨（見下）隨時可能再分岔。
#
# ── 出場 = 2（自動）的理由（2026-08-21 實機確認過同方向那一半）──
#
#   選「自動」：SPEC 那張表裡 13:40 買回 3 口時帳上只有 2 口空單。
#     「平倉」的語意是「平掉 N 口」，對 N 大於現有部位做不到——
#     用「平倉」的話，那個規格確認過的情境每天都會失敗。
#   代價：OS 的部位若已不在，「自動」會安靜地開出反向新倉；「平倉」會被拒。
#
#   ✅ 2026-08-21：帳上 2 口多單時送「自動」賣 1 口，成交，帳戶回到 1 口，
#      成交回報倉別欄位是 `O`（不是 `N`）——券商沒把它當新倉。
#   ⚠️ 但那次**沒有跨越零**，而跨越零正是選「自動」的唯一理由。
_NEW_CLOSE_BY_INTENT = {ENTRY: 2, EXIT: 2}


# OnNewData 的欄位位置。**2026-08-17 實機驗證通過**——監聽整個交易日收到兩則
# 真實回報（1 口微台的委託 + 成交），四個位置全部正確。
# 去識別化的樣本存在 tests/fixtures/onnewdata-real-2026-08-17.txt，
# tests/test_reply_parsing.py 對著它斷言。
_REPLY_KEYNO, _REPLY_MARKET, _REPLY_TYPE, _REPLY_ERR, _REPLY_QTY = 0, 1, 2, 3, 20

# 倉別在推播裡：欄位 [6] 是三合一編碼「買賣別 ＋ 倉別 ＋ ROD/IOC」，
# 例 `SOI10` = 賣出、平倉、IOC。第 2 個字才是倉別。
#
# ⚠️ **這是券商解析淨部位之後的結論，不是我們送出的 `sNewClose`。**
#    2026-08-24 的兩筆送出的都是「自動」，回來卻分別是 O 與 N。
#
# 七個樣本（2026-08-17／19／21／24）與查詢回報的 `[27]` 全部一致，
# 樣本存在 tests/fixtures/onnewdata-crossing-zero-2026-08-24.txt。
_REPLY_POSITION_CODE = 6
_POSITION_NEW, _POSITION_OFFSET = "N", "O"
_MARKET_FUTURES_REPLY = "TF"
_REPLY_TYPES = frozenset("NCUPDBS")     # N委託 C取消 U改量 P改價 D成交 B改價改量 S動態退單
_REPLY_FILLED = "D"        # 成交
_REPLY_CANCELLED = "C"     # 取消。IOC 的剩餘量會走這裡，收到它才代表委託真的結束


# ── 兩個查詢函式的回傳格式（2026-08-19 實機取得，見
#    tests/fixtures/reports-2026-08-19.txt）。與 OnNewData 是**三種不同的格式**。
#
# ⚠️ **委託序號只在 GetOrderReport 裡，GetFulfillReport 沒有。**
#    所以後備管道要兩段：先用序號換出「委託書號」，再拿它去比對成交。
#    串得起兩份的只有委託書號。
_ORDER_MARKET, _ORDER_BOOK_NO, _ORDER_SEQ, _ORDER_DAY = 0, 7, 8, 11
_FILL_MARKET, _FILL_BOOK_NO, _FILL_DAY = 0, 7, 9
_FILL_PRODUCT, _FILL_PRICE, _FILL_QTY = 12, 25, 26

# 查詢的兩種非答案。**必須分開處理**：查無資料是正常的（單還沒成交、
# 或那天沒交易），查詢錯誤是故障。兩者都不可以當成「確定沒成交」。
_QUERY_NO_DATA = "M003"
_QUERY_ERROR = "M999"


def parse_product_list(raw: str) -> dict:
    """解析群益商品清單，取出我們交易的三個商品。

    這是**群益的線路格式**，所以住在這裡而不是 broker 套件的共用契約層——
    假 broker 直接回傳 ContractInfo，不需要解析任何東西。

    格式：以 `;` 分隔的 `商品代碼,名稱,最後交易日,交易所代碼`，整串還會夾雜
    `%類別碼%類別名%` 的分類標頭——不剝掉的話第一筆代碼會變成
    `%201%期指數%TX00AM` 而永遠對不上。標頭可能出現在中段，不只開頭。

    殘缺的列直接跳過（清單裡混雜殘列很正常）；但三個目標商品缺任一個就 raise，
    因為訊號需要三個價才算得出來，帶著半套資料往下走只會在更遠的地方壞掉。
    """
    entries = {}
    for entry in raw.replace("\n", ";").split(";"):
        if entry.startswith("%"):
            entry = entry.rsplit("%", 1)[-1]
        fields = entry.split(",")
        if len(fields) < 3:
            continue
        code, last_day = fields[0].strip(), fields[2].strip()
        if last_day.isdigit():
            entries[code] = int(last_day)

    contracts = {}
    for code in PRODUCT_CODES:
        if code not in entries:
            continue
        last_day = entries[code]
        contracts[code] = ContractInfo(
            code=code,
            last_trading_day=last_day,
            order_code=_find_order_code(code, last_day, entries),
        )

    missing = [c for c in PRODUCT_CODES if c not in contracts]
    if missing:
        raise ProductListUnavailable(f"商品清單缺少：{', '.join(missing)}")
    return contracts


def _find_order_code(quote_code: str, last_day: int, entries: dict) -> str:
    """從清單中找出對應的**下單代碼**（指名月份的那個）。

    報價用 `TX00AM`，下單要用 `TX08`。配對條件是「同商品前綴 + 同最後交易日」：
    清單裡同時有 TX08（20260819）與 TX09（20260916），用最後交易日才分得開。

    ⚠️ 刻意不自己組字串。三個商品的月份寫法互不相同——大台 `TX08`、
    小台 `MTX08`、微台 `TM2608`（多了年份兩碼）——任何「規則」都會在微台上錯。

    找不到就 raise。退而求其次用近月連續代碼是不行的：那個代碼能不能下單
    沒有文件保證，猜錯的後果是委託被拒、或下到完全不同的商品上。
    """
    prefix = quote_code[:-2].rstrip("0")      # TX00AM → TX00 → TX
    candidates = [
        code for code, day in entries.items()
        if day == last_day
        and code != quote_code
        and code.endswith("AM")
        and code.startswith(prefix)
        # 前綴之後必須全是數字，否則 TX 會撈到選擇權之類的其他商品
        and code[len(prefix):-2].isdigit()
    ]
    if not candidates:
        raise ProductListUnavailable(
            f"商品清單找不到 {quote_code}（最後交易日 {last_day}）對應的下單代碼"
        )
    # 同前綴同最後交易日理應只有一個；真有多個時取最短的，
    # 那是「商品代碼＋月份」最基本的形式。
    order_code = min(candidates, key=len)
    return order_code[:-2]                    # 去掉 AM 後綴，下單不用盤別代碼


def build_future_order_fields(request: OrderRequest, account: str) -> dict:
    """把一筆 `OrderRequest` 轉成 `FUTUREORDER` 的欄位值。

    抽成純函式是為了**讓委託參數可被測試**。這些欄位寫錯的後果各不相同但都很貴：
    當沖標錯會被課不同稅率也可能被拒、時效寫成 ROD 會掛單整天、
    買賣別寫反會開出完全相反的部位。留在 COM 呼叫裡的話，沒有 Windows
    與帳號就一行都驗不到。

    參數依據 ADR-0003（市價、IOC、不標當沖）與官方文件的 FUTUREORDER 結構定義。
    """
    return {
        "bstrFullAccount": account,
        # 下單代碼（TX08），不是報價代碼（TX00AM）
        "bstrStockNo": request.order_code,
        "sBuySell": _BUY if request.side == BUY else _SELL,
        # 市價在群益是 bstrPrice="M"，且官方註明只能配 IOC 或 FOK
        "bstrPrice": _MARKET_PRICE,
        "sTradeType": _TRADE_TYPE_IOC,
        "nQty": request.lots,
        "sDayTrade": _DAY_TRADE_NO,
        "sNewClose": _NEW_CLOSE_BY_INTENT[request.intent],
        "sReserved": _SESSION_INTRADAY,
    }


@dataclass(frozen=True)
class ReplyRow:
    """一列已經看懂的委託回報。"""

    seq: str
    type: str
    failed: bool
    qty: int
    # 券商說這筆對淨部位做了什麼：N 開倉、O 平倉。**看不懂時是空字串**——
    # 空的意思是「不知道」，上層要跳過檢查而不是拿它去比對。
    position_type: str = ""


@dataclass(frozen=True)
class FillSummary:
    """一筆委託目前為止的回報彙整。

    ⚠️ **「收到回報」不等於「這筆委託結束了」。** IOC 的回報依序是：

        N 委託回報 → 交易所收下了，**單還在市場上**
        D 成交回報 → 成交一批（可能有好幾列）
        C 取消回報 → 剩餘量取消，到這裡才真的結束

    只看「有沒有收到回報」的話，`N` 一到就會以為結束了，於是在單還可能正在成交
    的時候回報 0 口、不寫狀態檔、不發通知——帳上多出來的部位沒有人知道。
    要問的問題是 `is_settled()`，不是 `matched_rows > 0`。
    """

    filled_lots: int
    rejected: bool
    matched_rows: int
    saw_cancel: bool = False
    reject_reason: str = ""
    # 券商說這筆對淨部位做了什麼（N 開倉／O 平倉）。看不懂時是空字串。
    position_type: str = ""

    def is_settled(self, requested_lots: int) -> bool:
        """這筆委託確定不會再有成交了嗎？

        三種確定的結局：
          被拒          → 沒有部位
          剩餘量已取消  → IOC 走完了，成交多少就是多少
          全部成交      → 沒有剩餘量可取消，等 C 只會等到逾時

        其他情況一律是「還沒結束」，交給呼叫端當成不確定處理。
        """
        return (
            self.rejected
            or self.saw_cancel
            or self.filled_lots >= requested_lots
        )


def parse_reply_row(row: str) -> ReplyRow | None:
    """解析一列 `OnNewData` 回報。看不懂就回 `None`（不是我們的單、或格式不符）。

    欄位位置**已於 2026-08-17 實機驗證**（見 tests/test_reply_parsing.py，
    期望值來自交易所回來的真實字串，不是對文件的推導）。

    但形狀檢查**沒有因此變得多餘**，理由換了一個：回報事件是共用的，
    證券（TS）、海期（OF）等格式完全不同的東西會走同一條線進來，
    而已驗證的樣本只涵蓋一種商品、一種情境。遇到沒見過的格式時，
    唯一安全的行為是承認看不懂——回 None 讓上層當成「沒收到回報」處理
    （ticket 05 會記為「不確定」並要求人工確認）。

    Qty 抓錯的代價仍然是最貴的那個：記進狀態檔的口數錯 → 下午照那個數字平倉
    → 多平的部分變成方向相反的新倉。
    """
    fields = row.split(",")
    if len(fields) <= _REPLY_QTY:
        return None
    if fields[_REPLY_MARKET].strip() != _MARKET_FUTURES_REPLY:
        return None
    row_type = fields[_REPLY_TYPE].strip()
    if row_type not in _REPLY_TYPES:
        return None
    qty = fields[_REPLY_QTY].strip()
    if not qty.isdigit():
        return None

    code = (fields[_REPLY_POSITION_CODE].strip()
            if len(fields) > _REPLY_POSITION_CODE else "")
    position = code[1] if len(code) > 1 and code[1] in (_POSITION_NEW,
                                                        _POSITION_OFFSET) else ""

    return ReplyRow(
        seq=fields[_REPLY_KEYNO].strip() or fields[-1].strip(),
        type=row_type,
        failed=fields[_REPLY_ERR].strip() == "Y",
        qty=int(qty),
        position_type=position,
    )


def summarize_fills(rows, seq: str) -> FillSummary:
    """彙整同一筆委託的回報，算出實際成交口數。

    三條規則，都是不對稱的——因為代價不對稱：

    1. **只要有成交就不算失敗。** 市價 IOC 的正常結局就是「成交一部分、
       剩下的取消」，取消列可能在成交列之後才到。把它當成失敗而不寫狀態檔的話，
       已成交的部位就沒有人知道，13:40 不會去平，直接進夜盤。
    2. **完全沒成交才談失敗。** 那時候確實沒有部位產生。
    3. **認不出是哪一筆就一列都不算。** 回報事件是共用的：手動下的單、
       連線時沖進來的前一批回報都在同一個緩衝區裡。加總到別人的成交上，
       下午就會平錯口數，多出來的部分變成反向新倉。

    比對委託序號用**整列子字串搜尋**而不是特定欄位。這個選擇原本是因為欄位位置
    還沒驗過，而 2026-08-17 的真實資料證明它另有價值：**成交回報的 `[0] KeyNo`
    是空的**，序號只出現在最後一欄。固定看某一欄的話，成交列就配不到委託列。

    ⚠️ **`seq` 為空字串時一列都不算**，因為空字串是任何字串的子字串。

    原本寫的是 `if seq and seq not in row: continue`——那個 `seq and` 讓
    序號拿不到時整條過濾被跳過，視窗內**每一列**期貨回報都被算成自己的成交。
    而序號來自 `SendFutureOrderCLR` 的回傳訊息，那是整個系統裡
    **唯一從來沒真正執行過的 API**，它成功時回不回序號我們並不知道。

    2026-08-19 實測證實這不是理論風險：使用者換倉的價差單就出現在
    OS 自己的回報串流上（見 fixtures/onnewdata-spread-2026-08-19.txt）。
    後果是 OS 記下一個它沒有的部位 → 13:40 送出平倉單 → 開出反向新倉。

    回空的彙整而不是拋例外：這是純函式，讓上層的 `is_settled()` 判定為
    「還沒結束」，自然走到 `FillUnknown`。那才是正確的結局——
    單確實送出去了，只是我們無法辨識回報，**絕不等於沒有部位**。
    """
    if not seq:
        return FillSummary(filled_lots=0, rejected=False, matched_rows=0)

    filled = 0
    matched = 0
    reject_reason = ""
    saw_cancel = False
    position = ""

    for row in rows:
        parsed = parse_reply_row(row)
        if parsed is None:
            continue
        if seq not in row:
            continue
        matched += 1
        if parsed.failed and not reject_reason:
            reject_reason = row
        if not position:
            # 委託（N）那一列就有了，不必等成交——這也是「不必再查一次回報」的根據。
            position = parsed.position_type
        if parsed.type == _REPLY_FILLED:
            filled += parsed.qty
        elif parsed.type == _REPLY_CANCELLED:
            saw_cancel = True

    return FillSummary(
        filled_lots=filled,
        # 有成交就不是失敗——剩餘量被取消是 IOC 的常態，不是錯誤
        rejected=bool(reject_reason) and filled == 0,
        matched_rows=matched,
        saw_cancel=saw_cancel,
        reject_reason=reject_reason if filled == 0 else "",
        position_type=position,
    )


def _query_rows(text: str) -> list[list[str]]:
    """把查詢回傳切成一列一列的欄位。非答案（空、M003、M999）回空 list。

    **`M003` 與 `M999` 都不是「沒成交」**——前者是查無資料（單可能還沒送到
    券商主機的紀錄裡），後者是查詢本身故障。當成「確定沒有部位」的話，
    真的成交了的那些口數就沒有人知道，13:40 不會去平，直接進夜盤。
    """
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith(_QUERY_NO_DATA):
        # 查無資料是**正常**的：單還沒進到券商主機的紀錄、或那天根本沒交易。
        logger.info("查詢回報：查無資料（%s）", _QUERY_NO_DATA)
        return []
    if text.startswith(_QUERY_ERROR):
        # 查詢**故障**。與上面那條長得很像但意思相反，所以等級刻意不同——
        # 兩者都回 []（絕不可當成「確定沒成交」），但這一種代表後備管道
        # 自己壞了，而那正是這張票要消滅的處境。log 分不出來的話，
        # 它會安靜地永遠回 None 而沒有人發現。
        logger.error("查詢回報：查詢錯誤（%s）：%s", _QUERY_ERROR, text[:120])
        return []
    return [line.split(",") for line in text.splitlines() if line.strip()]


def parse_order_book_no(text: str, order_seq: str, trading_day: int) -> str | None:
    """後備管道第一段：用**委託序號**在委託查詢裡找到自己那列，回傳**委託書號**。

    委託序號是 `SendFutureOrderCLR` 給的，也是 `OnNewData` 帶的；
    但成交查詢裡沒有它（2026-08-19 實測）。委託書號是唯一串得起兩份的欄位。

    ⚠️ **`order_seq` 為空字串時一列都不比。** 空字串是任何字串的子字串——
    ticket 04 的漏洞就是這樣把別人的成交算成自己的。

    ⚠️ **一定要比日期。** 委託書號的唯一性尚未驗證（每筆唯一？當日唯一？
    跨日重複？），在那之前日期是唯一擋得住「隔日殘留紀錄被誤認」的東西。
    """
    if not order_seq:
        return None

    for fields in _query_rows(text):
        if len(fields) <= _ORDER_DAY:
            continue
        if fields[_ORDER_MARKET].strip() != _MARKET_FUTURES_REPLY:
            continue        # 證券、海期走同一條線，格式完全不同
        if fields[_ORDER_SEQ].strip() != order_seq:
            continue
        if fields[_ORDER_DAY].strip() != str(trading_day):
            continue
        book_no = fields[_ORDER_BOOK_NO].strip()
        if book_no:
            return book_no
    return None


def parse_filled_lots(
    text: str, order_book_no: str, trading_day: int, requested_lots: int
) -> int | None:
    """後備管道第二段：加總這筆委託的成交口數。**認不出來就回 `None`。**

    回 `None` 與回 `0` 是完全不同的答案：

        None  不知道成交幾口 → 維持「不確定」，發 Discord 要人看帳戶
        0     **確定**沒有成交 → 上層什麼都不做

    所以「找不到成交列」回的是 `None` 而不是 `0`。查詢可能只是還沒反映，
    而若其實成交了卻記成 0，那個部位就沒有人知道、13:40 不會去平。

    ⚠️ **單腳成交列的欄位位置尚未實機驗證。** 手上唯一的真實樣本是價差單
    （使用者換倉），單腳的列會不會把第二隻腳那幾欄留白、還是整個往前移，
    我們不知道——這正是當初把微台 CID 推成 `FITMF` 的同一類錯誤。

    所以除了欄位，還有三道**與欄位位置無關**的檢查：日期要對、口數不得超過
    委託量、不得是價差列。推錯欄位最可能的症狀是抓到一個不相干的數字，
    而那三道擋得住它。實機里程碑送出第一筆真單後要回來把期望值釘死。
    """
    if not order_book_no:
        return None

    total = 0
    matched = 0
    for fields in _query_rows(text):
        if len(fields) <= _FILL_QTY:
            continue
        if fields[_FILL_MARKET].strip() != _MARKET_FUTURES_REPLY:
            continue
        if fields[_FILL_BOOK_NO].strip() != order_book_no:
            continue
        if fields[_FILL_DAY].strip() != str(trading_day):
            continue
        if "/" in ",".join(fields):
            # 價差列：一隻腳一列，加總會得到兩倍。OS 自己不下價差單，
            # 所以看到它就代表委託書號認錯了——前提已破，別硬算。
            #
            # ⚠️ **看整列，不是只看 `[12]`。** 初版只檢查 `[12]`，而真實的
            #    價差成交列在那一欄放的是**單腳**代碼（`TMFH6` / `TMFI6`）——
            #    斜線只出現在最後一欄。那道守衛因此對真實資料完全不會觸發
            #    （2026-08-19 code-review 實測；當時的測試之所以綠，是被
            #    「超額口數」那道擋下的，不是被這道）。
            #
            #    整列掃是刻意保守：價格、日期、序號都不含斜線，所以誤殺的機會
            #    很低；而萬一誤殺，結果只是回 None → 維持「不確定」→ 要人看一眼。
            return None
        qty = fields[_FILL_QTY].strip()
        if not qty.isdigit() or int(qty) < 1:
            # 0 口的成交列是矛盾的（沒成交就不該有列），多半是抓到別的欄位。
            return None
        try:
            float(fields[_FILL_PRICE].strip())
        except ValueError:
            # 口數與價格必須**同時**說得通。只驗口數的話，欄位整個位移時
            # 仍可能剛好撿到一個合法的小數字。
            return None
        total += int(qty)
        matched += 1

    if not matched:
        return None
    if total > requested_lots:
        # 委託 1 口卻算出 5 口——不可能是真的，而且是最貴的一種錯：
        # 下午照 5 口平倉，多出來的變成方向相反的新倉。
        logger.error("成交查詢算出 %d 口，超過委託的 %d 口，判定為解析錯誤",
                     total, requested_lots)
        return None
    return total
