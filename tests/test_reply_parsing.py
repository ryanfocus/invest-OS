"""委託回報的解析 —— 重點在「看不懂的時候要承認看不懂」。

⚠️ **關於這些測試證明了什麼，要說清楚。**

`OnNewData` 的欄位**位置**是我從官方文件的欄位排列推導出來的，沒有實機驗證過。
所以這裡不寫「第 20 欄是成交量」那種測試——期望值會跟程式碼來自同一個推導，
同義反覆，推錯了測試也不會紅。

這些測試守的是另一件事：**當那個推導是錯的時候，程式會不會安靜地記下一個錯數字。**
答案必須是不會。形狀對不上就回 None，讓上層當成「沒收到回報」處理，
而不是把某個剛好是數字的欄位當成成交口數寫進狀態檔。

真正驗證欄位位置的方法只有一個：在測試環境實打一筆，把 raw 回報印出來對。
那是 ticket 04 最後一條驗收條件的一部分。
"""

from broker.capital import parse_reply_row


def _row(**overrides):
    """組一列長度足夠、市場別與型態都合法的回報。

    內容不代表真實資料——這些測試問的是「形狀不對時會怎樣」，不是「欄位在哪」。
    """
    fields = [""] * 49
    fields[0] = "SEQ0000000001"     # KeyNo
    fields[1] = "TF"                # MarketType
    fields[2] = "D"                 # Type：成交
    fields[3] = "N"                 # OrderErr：正常
    fields[20] = "2"                # Qty
    for index, value in overrides.items():
        fields[int(index)] = value
    return ",".join(fields)


# --- 認得的情況 ---


def test_a_futures_fill_row_is_parsed():
    parsed = parse_reply_row(_row())
    assert parsed.type == "D"
    assert parsed.qty == 2
    assert parsed.failed is False


def test_a_rejected_order_is_flagged():
    """OrderErr = Y 代表這筆委託失敗，不可以被當成成交。"""
    assert parse_reply_row(_row(**{"3": "Y"})).failed is True


# --- 看不懂就回 None，絕不猜 ---


def test_a_row_from_another_market_is_ignored():
    """證券（TS）與海期（OF）的回報也會進到同一個事件，欄位定義卻不同。"""
    assert parse_reply_row(_row(**{"1": "TS"})) is None


def test_a_truncated_row_is_ignored():
    """欄位不夠長時，索引會抓到不存在的位置或錯位的內容。"""
    assert parse_reply_row("SEQ1,TF,D,N") is None


def test_a_row_whose_quantity_is_not_a_number_is_ignored():
    """這是**欄位位置推錯時最可能出現的徵兆**。

    若第 20 欄其實不是數量，多半會撈到日期、代碼或空字串。
    這時候唯一安全的行為是承認看不懂——把它硬轉成數字才是災難的開始。
    """
    assert parse_reply_row(_row(**{"20": "13:45:01"})) is None
    assert parse_reply_row(_row(**{"20": ""})) is None
    assert parse_reply_row(_row(**{"20": "TXFH6"})) is None


def test_an_unknown_row_type_is_ignored():
    """Type 只有 N/C/U/P/D/B/S 七種。撈到別的字元就代表欄位對錯位了。"""
    assert parse_reply_row(_row(**{"2": "X"})) is None
    assert parse_reply_row(_row(**{"2": "20260810"})) is None


def test_an_empty_row_is_ignored():
    assert parse_reply_row("") is None
