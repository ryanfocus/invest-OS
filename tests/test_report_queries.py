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
（日期、口數不得超過委託量、不得是價差列）。里程碑 1 送出第一筆真單後，
要拿真實的單腳回傳回來把這些期望值釘死。
"""

import os

import pytest

from broker.capital import parse_filled_lots, parse_order_book_no

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
    from broker.capital import _query_rows

    with caplog.at_level(logging.INFO, logger="broker.capital"):
        _query_rows("M003")
    levels_for_no_data = {r.levelno for r in caplog.records}

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="broker.capital"):
        _query_rows("M999: 查詢錯誤")
    levels_for_error = {r.levelno for r in caplog.records}

    assert levels_for_no_data == {logging.INFO}, "查無資料是正常的，不該是 ERROR"
    assert levels_for_error == {logging.ERROR}, "查詢故障要看得見，不能只是 INFO"


def test_a_query_error_row_is_never_parsed_as_data():
    """萬一 M999 的訊息剛好有夠多逗號，也不可以被當成資料列。"""
    from broker.capital import _query_rows
    assert _query_rows("M999," + ",".join(["x"] * 40)) == []


def test_a_no_data_marker_is_never_parsed_as_data():
    from broker.capital import _query_rows
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
