"""委託／成交查詢回傳的解析 —— 推播收不到時的後備管道。

## 為什麼要兩段查詢

委託序號只存在於其中一種回傳裡（2026-08-19 實測）：

    OnNewData          [0] 與 [47]   2315609394137   ✅
    GetOrderReport     [8] 與 [9]    2315609394137   ✅
    GetFulfillReport   （沒有）                       ❌  [8] 是成交編號

所以不能直接查成交。要先用序號在委託查詢裡找到自己那列、讀出**委託書號**，
再拿它去成交查詢裡比對。串得起兩份的只有委託書號。

## 為什麼這個檔案處處在「拒絕回答」

回報是共用的：使用者手動下的單就在同一份資料裡（2026-08-19 換倉實測證實）。
認錯的後果是把別人的成交寫進自己的狀態檔 → 13:40 照那個口數平倉 →
多平的部分變成**方向相反、沒人管的新倉**。

而回不出答案的代價只是「維持不確定」——發個 Discord 要人看一眼帳戶。
兩者差了一個數量級，所以**只要有一絲不確定就回 None**。

## ⚠️ 單腳成交的欄位位置**尚未實機驗證**

手上唯一的真實成交查詢樣本是**價差單**（使用者換倉）。單腳的列會不會
把第二隻腳那幾欄留白、還是整個往前移，我們**不知道**。
這正是當初把微台 CID 推成 `FITMF` 的同一類錯誤。

所以解析不只看欄位，還要通過幾道與欄位位置**無關**的檢查
（日期、口數不得超過委託量、不得是價差列）。實機里程碑送出第一筆真單後，
要拿真實的單腳回傳回來把這些期望值釘死。
"""

import os

import pytest

from broker.capital_wire import parse_filled_lots, parse_order_book_no

CRLF = chr(13) + chr(10)     # 群益查詢回傳的換行
DAY = 20260819
SEQ = "2315609394137"
BOOK = "x0582"


def _fixture() -> tuple[str, str]:
    """2026-08-19 使用者換倉的真實回傳（價差單）。"""
    path = os.path.join(os.path.dirname(__file__), "fixtures", "reports-2026-08-19.txt")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    order = text.split("=== GetOrderReport（格式 1），共 1 列 ===")[1]
    order = order.split("=== GetFulfillReport")[0].strip()
    fulfill = text.split("=== GetFulfillReport（格式 1），共 2 列 ===")[1]
    fulfill = fulfill.split("=== 欄位位置")[0].strip()
    return order, fulfill


def _order_row(*, seq=SEQ, book=BOOK, day=DAY, product="TMFH6"):
    """一列格式合法的委託查詢回傳。內容不代表真實資料，只用來測比對規則。"""
    f = [""] * 40
    f[0], f[1] = "TF", "FUT"
    f[7], f[8], f[9] = book, seq, seq
    f[11], f[12] = str(day), "113811"
    f[15] = product
    return ",".join(f)


def _fill_row(*, book=BOOK, day=DAY, qty="1", price="44846.0000", product="TMFH6"):
    """一列格式合法的成交查詢回傳（**單腳**——欄位位置尚未實機驗證）。"""
    f = [""] * 40
    f[0], f[1] = "TF", "FUT"
    f[7], f[8] = book, "0000006131"
    f[9], f[10] = str(day), "113812"
    f[12] = product
    f[21] = "B"
    f[25], f[26] = price, qty
    return ",".join(f)


# ─────────────────────────────────────────────────────────
# 第一段：用委託序號換出委託書號
# ─────────────────────────────────────────────────────────


def test_the_order_book_number_is_found_by_sequence():
    assert parse_order_book_no(_order_row(), SEQ, DAY) == BOOK


def test_the_real_settlement_day_order_is_parsed():
    """用當天真實的回傳驗，不是自己組的假資料。

    這一列是使用者 11:38 換倉的價差委託。我們要的欄位（序號、委託書號、日期）
    在價差單與單腳應該是同樣的位置——它們描述的是**委託**，不是腳。
    """
    order, _ = _fixture()
    assert parse_order_book_no(order, SEQ, DAY) == BOOK


def test_someone_elses_order_is_not_matched():
    """**這是本檔存在的理由。** 回報是共用的，別人的單就在同一份資料裡。"""
    order, _ = _fixture()
    assert parse_order_book_no(order, "OUR000000001", DAY) is None


def test_an_order_from_another_day_is_not_matched():
    """委託書號的唯一性**尚未驗證**（每筆唯一？當日唯一？跨日重複？）。

    在驗證之前，日期是唯一擋得住「隔日殘留紀錄被誤認」的東西。
    """
    order, _ = _fixture()
    assert parse_order_book_no(order, SEQ, 20260820) is None


def test_no_data_is_not_an_answer():
    """`M003` 是「查無資料」。當成「沒成交」的話，實際上成交的部位就沒人知道。"""
    assert parse_order_book_no("M003", SEQ, DAY) is None


def test_a_query_error_is_not_an_answer():
    assert parse_order_book_no("M999: 查詢錯誤", SEQ, DAY) is None


@pytest.mark.parametrize("text", ["", "   ", "\r\n"])
def test_an_empty_response_is_not_an_answer(text):
    assert parse_order_book_no(text, SEQ, DAY) is None


def test_an_empty_sequence_matches_nothing():
    """空字串是任何字串的子字串。ticket 04 的漏洞就是這樣來的。"""
    assert parse_order_book_no(_order_row(), "", DAY) is None


def test_a_row_without_a_book_number_is_refused():
    assert parse_order_book_no(_order_row(book=""), SEQ, DAY) is None


def test_a_non_futures_row_is_ignored():
    """回報頻道上有證券（TS）、海期（OF），格式完全不同。"""
    row = _order_row().replace("TF,FUT", "TS,STK", 1)
    assert parse_order_book_no(row, SEQ, DAY) is None


def test_a_short_row_is_refused_not_index_errored():
    assert parse_order_book_no("TF,FUT,TAIFEX", SEQ, DAY) is None


# ─────────────────────────────────────────────────────────
# 第二段：用委託書號加總成交口數
# ─────────────────────────────────────────────────────────


def test_a_single_fill_is_counted():
    assert parse_filled_lots(_fill_row(qty="1"), BOOK, DAY, requested_lots=1) == 1


def test_multiple_fills_of_the_same_order_are_added_up():
    """市價 IOC 可能分批成交。少加一列的後果是下午少平，殘留進夜盤。"""
    text = "\r\n".join([_fill_row(qty="1"), _fill_row(qty="2")])
    assert parse_filled_lots(text, BOOK, DAY, requested_lots=3) == 3


def test_another_orders_fills_are_not_counted():
    text = "\r\n".join([_fill_row(book="x9999", qty="5"), _fill_row(qty="1")])
    assert parse_filled_lots(text, BOOK, DAY, requested_lots=1) == 1


def test_fills_from_another_day_are_not_counted():
    """委託書號的唯一性尚未驗證——跨日若會重複，隔日殘留的紀錄會被誤認。

    ⚠️ 口數刻意用 1（在委託量之內）。突變測試抓到：原本寫 5 口時，
    拿掉日期檢查仍然通過——因為被「超額口數」那道擋下來了。
    測試綠了，但守的不是這一條。
    """
    text = _fill_row(day=20260818, qty="1")
    assert parse_filled_lots(text, BOOK, DAY, requested_lots=1) is None


def test_no_matching_row_is_not_zero():
    """**「查不到成交」不等於「確定沒成交」。**

    回 0 的話上層會寫下「確定沒有部位」而什麼都不做；若其實成交了，
    那個部位就沒有人知道，13:40 不會去平，直接進夜盤。
    查不到就維持「不確定」，發 Discord 讓人看一眼——那才是安全的方向。
    """
    assert parse_filled_lots(_fill_row(book="x9999"), BOOK, DAY, requested_lots=1) is None


@pytest.mark.parametrize("text", ["M003", "M999: 查詢錯誤", "", "   "])
def test_a_non_answer_stays_unknown(text):
    assert parse_filled_lots(text, BOOK, DAY, requested_lots=1) is None


def test_an_empty_book_number_matches_nothing():
    assert parse_filled_lots(_fill_row(), "", DAY, requested_lots=1) is None


# ─────────────────────────────────────────────────────────
# 擋住「欄位位置推錯」——與欄位無關的檢查
# ─────────────────────────────────────────────────────────
#
# 單腳成交列的欄位位置尚未實機驗證（手上只有價差單樣本）。
# 下面這幾條不靠欄位位置，而是靠「答案本身說不通」把錯誤攔下來。


def test_more_lots_than_requested_is_refused():
    """**推錯欄位最可能的症狀就是抓到一個不相干的數字。**

    委託 1 口卻算出 5 口——那不可能是真的。而它正是最貴的一種錯：
    下午照 5 口平倉，多出來的 4 口變成方向相反的新倉。
    寧可回「不確定」讓人來看。
    """
    assert parse_filled_lots(_fill_row(qty="5"), BOOK, DAY, requested_lots=1) is None


def test_exactly_the_requested_amount_is_fine():
    """對照組：等於委託量是最常見的正常結果，不可以被上面那條誤殺。"""
    assert parse_filled_lots(_fill_row(qty="3"), BOOK, DAY, requested_lots=3) == 3


def test_a_spread_row_is_refused():
    """價差單一隻腳一列，每列都寫 1 口——整份加總會得到 2 而實際只有 1 口。

    OS 自己不下價差單，所以這只會在「委託書號認錯」時發生。
    看到價差列就代表前提已經破了，回 None 比硬算安全。

    ⚠️ 口數與 `requested_lots` 刻意都設 2，讓「超額口數」那道守衛**不會**觸發。
    初版兩條都用 1 口，於是真正擋下它的是超額那道——價差守衛自己壞掉也測不出來。
    """
    text = _fill_row(product="TMFH6/I6", qty="2")
    assert parse_filled_lots(text, BOOK, DAY, requested_lots=2) is None


def test_the_real_spread_fills_are_refused():
    """用當天真實的價差成交驗上一條。**這是本檔最重要的一條。**

    這兩列的委託書號確實是 x0582、日期也對得上，只有「這是價差」擋得住它。

    ⚠️ `requested_lots=2` 是刻意的。用 1 的話，兩列加總 2 口會先被
    「超額口數」那道擋掉，價差守衛壞掉也照樣綠——2026-08-19 的 code-review
    正是這樣抓到初版的守衛看錯欄位：真實價差列的 `[12]` 放的是**單腳**代碼
    （`TMFH6` / `TMFI6`），斜線只出現在最後一欄。
    """
    _, fulfill = _fixture()
    assert parse_filled_lots(fulfill, BOOK, DAY, requested_lots=2) is None


def test_the_spread_marker_is_looked_for_anywhere_in_the_row():
    """真實資料裡斜線出現在**最後一欄**，不在商品那一欄。

    只看 `[12]` 的話，這一列會被當成單腳而把口數加進去。
    """
    f = _fill_row(qty="2").split(",")
    f[-1] = "TMFH6/I6"          # 斜線只在最後一欄，就像真實資料
    assert parse_filled_lots(",".join(f), BOOK, DAY, requested_lots=2) is None


def test_a_non_numeric_quantity_is_refused():
    assert parse_filled_lots(_fill_row(qty="一"), BOOK, DAY, requested_lots=1) is None


def test_a_row_whose_price_is_not_a_number_is_refused():
    """口數與價格必須**同時**說得通。只驗口數的話，欄位整個位移時
    仍可能剛好撿到一個合法的小數字。"""
    assert parse_filled_lots(_fill_row(price="不是價格"), BOOK, DAY, requested_lots=1) is None


def test_a_zero_lot_row_is_refused():
    """成交列寫 0 口是矛盾的——沒成交就不該有成交列。
    多半代表抓到的是別的欄位。"""
    assert parse_filled_lots(_fill_row(qty="0"), BOOK, DAY, requested_lots=1) is None


# ─────────────────────────────────────────────────────────
# 回報連線的靜默失敗（code-review 2026-08-16 指出，ticket 09 收尾）
# ─────────────────────────────────────────────────────────
#
# `SKReplyLib_ConnectByID` 回 0 **只代表「請求已受理」**——官方文件把
# `OnConnect` / `OnComplete` 列為它的通知事件，連線結果是非同步回來的。
#
# 連線若靜默失敗，正式程式會照常送單、然後永遠等不到 OnNewData
# → 每一天都變成「不確定」→ 每天都要人介入。那正是本張票要消滅的處境。


def test_a_fresh_reply_channel_is_not_yet_connected():
    """預設不可以是「已連線」。**樂觀的預設值等於沒有這個檢查。**"""
    from broker.capital import _ReplyEvents
    assert _ReplyEvents().connected is None


def test_a_successful_connect_event_marks_it_connected():
    from broker.capital import _ReplyEvents
    events = _ReplyEvents()
    events.OnConnect("U123", 0)
    assert events.connected is True


def test_a_failed_connect_event_marks_it_disconnected():
    """錯誤碼非 0 就是沒連上。當成連上的話，等不到回報時會去查錯方向。"""
    from broker.capital import _ReplyEvents
    events = _ReplyEvents()
    events.OnConnect("U123", 3001)
    assert events.connected is False


def test_a_disconnect_event_clears_the_connection():
    """中途斷線也要看得見——它與「從未連上」的後果一樣（收不到推播）。"""
    from broker.capital import _ReplyEvents
    events = _ReplyEvents()
    events.OnConnect("U123", 0)
    events.OnDisconnect("U123", 3002)
    assert events.connected is False


def test_the_last_connection_error_is_kept_for_the_message():
    """只知道「沒連上」對排查沒有幫助，錯誤碼要留著。"""
    from broker.capital import _ReplyEvents
    events = _ReplyEvents()
    events.OnConnect("U123", 3001)
    assert "3001" in events.connect_error


# ─────────────────────────────────────────────────────────
# M003（查無資料）與 M999（查詢錯誤）必須分得開
# ─────────────────────────────────────────────────────────
#
# 兩者都回 []（絕不可當成「確定沒成交」），但意思相反：
#
#   M003  正常——單還沒進到紀錄裡、或那天沒交易
#   M999  故障——後備管道自己壞了，而那正是這張票要消滅的處境
#
# ⚠️ 原本這幾條測試是**空斷言**：把整段前綴判斷刪掉，全庫測試依然全綠。
#    因為 "M003" 逗號切完只有 1 欄，欄位數檢查（len(fields) <= 索引）
#    就先把它 continue 掉了。所以要斷言的是**兩者被區分開**這件事本身。


def test_no_data_and_a_query_error_are_logged_differently(caplog):
    """行為相同（都回 []），所以唯一分得出兩者的就是這條 log 線索。

    不釘住它，「查詢元件壞掉」會長得跟「單還沒成交」一模一樣，
    這條後備管道靜靜地永遠回 None，而每天照樣要人介入。
    """
    import logging
    from broker.capital_wire import _query_rows

    with caplog.at_level(logging.INFO, logger="broker.capital_wire"):
        _query_rows("M003")
    levels_for_no_data = {r.levelno for r in caplog.records}

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="broker.capital_wire"):
        _query_rows("M999: 查詢錯誤")
    levels_for_error = {r.levelno for r in caplog.records}

    assert levels_for_no_data == {logging.INFO}, "查無資料是正常的，不該是 ERROR"
    assert levels_for_error == {logging.ERROR}, "查詢故障要看得見，不能只是 INFO"


def test_a_query_error_row_is_never_parsed_as_data():
    """萬一 M999 的訊息剛好有夠多逗號，也不可以被當成資料列。"""
    from broker.capital_wire import _query_rows
    assert _query_rows("M999," + ",".join(["x"] * 40)) == []


def test_a_no_data_marker_is_never_parsed_as_data():
    from broker.capital_wire import _query_rows
    assert _query_rows("M003," + ",".join(["x"] * 40)) == []


# ─────────────────────────────────────────────────────────
# 成交查詢的期貨市場別檢查
# ─────────────────────────────────────────────────────────
#
# 委託查詢那一側本來就有測，成交查詢這一側漏了——而真正把數字加起來
# 寫進狀態檔的是這一半。證券（TS）與海期（OF）的列就在同一份回傳裡，
# 而它們的欄位語意與期貨完全不同。


def test_a_non_futures_fill_row_is_not_counted():
    row = _fill_row(qty="1").replace("TF,FUT", "TS,STK", 1)
    assert parse_filled_lots(row, BOOK, DAY, requested_lots=1) is None


def test_a_non_futures_row_does_not_contaminate_a_real_one():
    """混在一起時只算期貨那一列。少了市場別檢查，兩列都會被加進去。"""
    text = "\r\n".join([
        _fill_row(qty="1").replace("TF,FUT", "TS,STK", 1),
        _fill_row(qty="1"),
    ])
    assert parse_filled_lots(text, BOOK, DAY, requested_lots=1) == 1


def test_the_sequence_is_matched_exactly_not_as_a_substring():
    """**這是 ticket 04 那個漏洞在新解析器裡的翻版。**

    序號用子字串比對的話，`23156093941370`（別人的單，多一位數）會被
    `2315609394137`（我們的）比中，於是拿到別人的委託書號 → 加總別人的成交
    → 13:40 平錯口數 → 多平的部分變成反向新倉。

    突變測試抓到：把 `!=` 換成子字串比對，424 條測試全綠。
    """
    longer = _order_row(seq=SEQ + "0", book="z9999")
    assert parse_order_book_no(longer, SEQ, DAY) is None


def test_a_sequence_that_is_a_prefix_of_ours_is_not_matched():
    """反方向也要擋：我們的序號含有別人的序號當前綴時。"""
    shorter = _order_row(seq=SEQ[:-1], book="z9999")
    assert parse_order_book_no(shorter, SEQ, DAY) is None


def test_our_order_is_still_found_among_other_peoples():
    """對照組：混在一堆別人的單裡面，仍然找得到自己那一列。"""
    text = "\r\n".join([
        _order_row(seq=SEQ + "0", book="z9999"),
        _order_row(),
        _order_row(seq="9" + SEQ, book="z8888"),
    ])
    assert parse_order_book_no(text, SEQ, DAY) == BOOK


def test_the_book_number_is_matched_exactly_too():
    """委託書號同樣不可以用子字串比對——`x058` 不該比中 `x0582`。"""
    assert parse_filled_lots(_fill_row(book="x05820"), BOOK, DAY,
                             requested_lots=1) is None


# ─────────────────────────────────────────────────────────
# ✅ 單腳格式：**實機驗證完成**（2026-08-21 實機里程碑）
# ─────────────────────────────────────────────────────────
#
# 本檔開頭那段「單腳欄位位置尚未實機驗證」的警告，到這裡結束。
#
# OS 自己送出 1 口微台，取回真實的單腳回傳。答案是：**欄位不會位移，
# 第二隻腳的欄位是留白／0**。所以從價差單推出來的
# `_FILL_PRODUCT/_FILL_PRICE/_FILL_QTY = 12, 25, 26` 是對的。
#
# 這幾條的價值在於**它們是對著真實資料斷言的**，不是對著我的推導。


def _singleleg() -> tuple[str, str]:
    """2026-08-21 實機里程碑的委託與成交查詢（各兩列：出場在前、進場在後）。"""
    path = os.path.join(os.path.dirname(__file__), "fixtures",
                        "reports-singleleg-2026-08-21.txt")
    with open(path, encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.startswith("TF,")]
    # 委託查詢的列比成交查詢長很多（60+ 欄 vs 45 欄），用長度分得開，
    # 不必依賴標題文字——標題會隨列數改寫，而那正好在 2026-08-21 咬過一次。
    order = [ln for ln in lines if len(ln.split(",")) > 55]
    fulfill = [ln for ln in lines if len(ln.split(",")) <= 55]
    return CRLF.join(order), CRLF.join(fulfill)


def _fill_rows() -> tuple[str, str]:
    """回傳（進場那列, 出場那列）。"""
    _, fulfill = _singleleg()
    rows = fulfill.splitlines()
    entry = next(r for r in rows if r.split(",")[21] == "B")
    exit_ = next(r for r in rows if r.split(",")[21] == "S")
    return entry, exit_


def _crossing_zero() -> tuple[str, str]:
    """2026-08-24 跨越零那天的委託與成交查詢（各三列）。

    ⚠️ **不能用 `[21]`（買賣別）分進出場。** 那天是**做空**：進場是 S、
       出場是 B——與 2026-08-21 做多那天正好相反。用買賣別分的話兩天會
       互相顛倒，而斷言仍然會過，只是意義整個反了。用委託書號分。
    """
    path = os.path.join(os.path.dirname(__file__), "fixtures",
                        "reports-crossing-zero-2026-08-24.txt")
    with open(path, encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.startswith("TF,")]
    order = [ln for ln in lines if len(ln.split(",")) > 55]
    fulfill = [ln for ln in lines if len(ln.split(",")) <= 55]
    return CRLF.join(order), CRLF.join(fulfill)


def _by_book(text: str, book: str) -> str:
    return next(r for r in text.splitlines() if r.split(",")[7] == book)


CZ_ENTRY_BOOK = "m0193"      # 09:26 賣 1 口，帳上有 1 口反向多單 → 跨越零
CZ_EXIT_BOOK = "v0702"       # 13:40 買回 1 口，帳上已經是 0
CZ_REJECTED_BOOK = "00000"   # 08:51 用「新倉」送出，被 980 退單（沒配到書號）

# 倉別欄位的位置在兩份報告裡不同
POSITION_TYPE_IN_FULFILL = 27
POSITION_TYPE_IN_ORDER = 33


SL_DAY = 20260821
SL_SEQ = "2315609807637"          # 進場的委託序號
SL_BOOK = "g0106"                 # 進場的委託書號
SL_EXIT_SEQ = "2315609905580"     # 出場的
SL_EXIT_BOOK = "s0838"


def test_the_real_single_leg_order_yields_its_book_number():
    order, _ = _singleleg()
    assert parse_order_book_no(order, SL_SEQ, SL_DAY) == SL_BOOK


def test_the_real_single_leg_fill_is_counted_correctly():
    """**這是整個 ticket 09 最重要的一條斷言。**

    1 口的委託、1 口的成交。若 `_FILL_QTY` 的位置是錯的，這裡會抓到
    別的數字——而下午就會照那個數字平倉。

    ⚠️ 資料裡有**兩筆**（進場與出場），所以這條同時在驗「只算自己那一筆」——
    委託書號比對若失效，會得到 2。
    """
    _, fulfill = _singleleg()
    assert parse_filled_lots(fulfill, SL_BOOK, SL_DAY, requested_lots=1) == 1


def test_the_single_leg_fill_price_position_is_real():
    """口數對不代表位置對——也可能是剛好撿到另一個 1。

    價格那欄一起驗：44838 進、45135 出，都是當天真實的成交價。
    兩個位置同時對，才排除得掉「碰巧」。
    """
    from broker.capital_wire import _FILL_PRICE
    entry, exit_ = _fill_rows()
    assert float(entry.split(",")[_FILL_PRICE]) == 44838.0
    assert float(exit_.split(",")[_FILL_PRICE]) == 45135.0


def test_the_second_leg_fields_are_blank_not_shifted():
    """**這才是先前不知道、而且推錯會很貴的那件事。**

    價差單在 `[17][18]` 放第二隻腳的 CID 與月份。單腳若是「整個往前移」，
    `[25]` 與 `[26]` 就會指到別的東西。實機證實是**留白**，不是位移。
    """
    for row in _fill_rows():
        f = row.split(",")
        assert f[17].strip() == "", "第二隻腳的 CID 應該是空的"
        assert float(f[20]) == 0.0, "第二隻腳的價格應該是 0"
        assert f[13].strip() == "FITM", "第一隻腳仍在原位"


def test_another_days_single_leg_fill_is_not_counted():
    """日期守衛對真實的單腳資料一樣有效。"""
    _, fulfill = _singleleg()
    assert parse_filled_lots(fulfill, SL_BOOK, 20260820, requested_lots=1) is None


def test_the_two_real_book_numbers_have_nothing_in_common():
    """`x0582`（08-19 價差）與 `g0106`（08-21 單腳）——格式完全不同。

    委託書號**不是有規律的流水號**，任何想從它解析出意義的念頭都要打消：
    它只該被當成不透明的比對用字串。
    """
    assert not SL_BOOK.startswith("x")
    assert len(SL_BOOK) == len(BOOK)      # 長度巧合，但字元集不同


def test_the_entry_and_exit_fills_differ_only_where_expected():
    """進場與出場的成交列並列，**只有六個欄位不同**。

    這條是「記錄」而不是「能力」——我們刻意**不寫程式去讀倉別欄位**
    （2026-08-21 決定：兩個樣本撐不起一個抽象，而 N/O 的含義未經證實）。
    但把差異釘住有價值：日後格式若變、或某個欄位開始跟著別的東西動，
    這裡會紅，而不是等到某天平倉單被當成新倉才發現。
    """
    entry, exit_ = _fill_rows()
    e, x = entry.split(","), exit_.split(",")
    differing = {i for i in range(len(e)) if e[i] != x[i]}
    assert differing == {7, 8, 10, 21, 25, 27, 37, 43, 49}, (
        "進出場成交列的差異欄位變了。原本是："
        "[7]委託書號 [8]成交編號 [10]時間 [21]買賣別 [25]成交價 [27]倉別 "
        "[37]時間（第二次出現）[43]成交價（第二次出現）[49]毫秒時戳"
    )


def test_the_price_and_time_each_appear_twice():
    """`[25]`/`[43]` 是同一個價格，`[10]`/`[37]` 是同一個時間。

    ⚠️ 這條是 2026-08-21 寫上一條時**自己抓到的**：原本只斷言六個欄位，
    因為我只數了前半段。對著真實資料斷言才會發現後面還有重複的欄位。

    價值：解析若哪天改抓 `[43]` 而不是 `[25]`，行為不變、測試也不會紅——
    釘住「它們相等」，那個沉默的改動至少有一條測試在描述它。
    """
    for row in _fill_rows():
        f = row.split(",")
        assert f[25] == f[43], "成交價出現兩次，值應相同"
        assert f[10] == f[37], "時間出現兩次，值應相同"


def test_the_position_type_field_reports_what_the_broker_did_not_what_we_asked():
    """**倉別欄位反映券商對淨部位實際做了什麼，不是我們請求了什麼。**

    2026-08-24 的兩筆送出的是**同一個** `sNewClose=2`（自動），回來卻不同：

        09:26  賣 1 口，帳上有 1 口反向多單  → **O**（平掉使用者的多單）
        13:40  買 1 口，帳上是 0 口          → **N**（開一口新的還回去）

    這是四組樣本裡最關鍵的一組。在它之前只有 2026-08-21 的 N／O 對照，
    而那天送出的 `sNewClose` 本來就不同（0 與 2），所以「回報的差異
    來自請求的差異」這個解釋當時無法排除。現在排除掉了。
    """
    _, fulfill = _crossing_zero()
    entry = _by_book(fulfill, CZ_ENTRY_BOOK)
    exit_ = _by_book(fulfill, CZ_EXIT_BOOK)
    assert entry.split(",")[POSITION_TYPE_IN_FULFILL] == "O", "跨越零的進場記成平倉"
    assert exit_.split(",")[POSITION_TYPE_IN_FULFILL] == "N", "帳上空手時的出場記成新倉"


def test_the_two_reports_agree_on_the_position_type():
    """委託回報 `[33]` 與成交回報 `[27]` 是同一件事，位置不同。

    位置不同這件事沒有文件，是從真實資料比對出來的。哪天有人把其中一個
    常數改成另一個的位置，這條會紅。
    """
    order, fulfill = _crossing_zero()
    for book in (CZ_ENTRY_BOOK, CZ_EXIT_BOOK):
        o = _by_book(order, book).split(",")[POSITION_TYPE_IN_ORDER]
        f = _by_book(fulfill, book).split(",")[POSITION_TYPE_IN_FULFILL]
        assert o == f, f"{book} 兩份報告的倉別不一致：委託 {o!r} vs 成交 {f!r}"


def test_entry_and_exit_position_types_are_always_opposite():
    """**進場與出場的倉別必然相反**——兩天四筆都符合。

    ⚠️ **2026-09-02 更正：那四筆都是 1 口。** 委託口數大於帳上反向部位時，
    券商會把單拆成平倉＋新倉而只回一個旗標，「相反」就不成立了
    （那天持有 1 口多單、賣 2 口，進出場兩筆都回 N）。

        進場 N（開了倉）      → 出場必然 O（平掉它）
        進場 O（平掉別人的）  → 出場必然 N（開一口還回去）

    ⚠️ **兩邊相同代表出了事。** 都是 N 意味著開了兩次——那正是 ticket 06
       開頭警告的「平倉單被當成新倉，部位不減反增」。

    這條目前只是對著歷史資料的觀察，**程式沒有在執行期檢查它**。
    要檢查得在出場後再查一次回報（兩次阻塞查詢），而且只有事後才知道。
    那是一個還沒做的決定，記在 ticket 06。
    """
    sl_entry, sl_exit = _fill_rows()
    _, cz = _crossing_zero()
    pairs = [
        ("2026-08-21 做多", sl_entry, sl_exit),
        ("2026-08-24 做空跨越零",
         _by_book(cz, CZ_ENTRY_BOOK), _by_book(cz, CZ_EXIT_BOOK)),
    ]
    for label, entry, exit_ in pairs:
        e = entry.split(",")[POSITION_TYPE_IN_FULFILL]
        x = exit_.split(",")[POSITION_TYPE_IN_FULFILL]
        assert {e, x} == {"N", "O"}, f"{label}：進場 {e!r}、出場 {x!r}，不是一對相反"


def test_a_rejected_order_never_reaches_the_fulfill_report():
    """**退單的委託不出現在成交回報裡**，所以後備管道不可能把它算成成交。

    08:51 那筆用「新倉」送出、被 980 退單。它在委託回報裡有一列
    （狀態 6、已成交量 0、旗標 Y、沒有配到委託書號），但成交回報裡沒有。

    ticket 09 的後備管道是拿委託書號去比對成交回報——退單的單既然不在
    那份資料裡，就不可能被誤算。這是設計上的保護，但在 2026-08-24
    之前沒有真實樣本佐證。
    """
    order, fulfill = _crossing_zero()
    rejected = _by_book(order, CZ_REJECTED_BOOK)
    assert rejected.split(",")[31] == "0", "退單的已成交量不是 0"
    assert CZ_REJECTED_BOOK not in [r.split(",")[7] for r in fulfill.splitlines()], (
        "退單的委託出現在成交回報裡，後備管道有被誤算的風險"
    )