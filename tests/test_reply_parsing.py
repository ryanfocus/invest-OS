"""委託回報的解析。

## 期望值的來源（2026-08-17 起改變了）

先前這個檔案的註解寫著：

> 不寫「第 20 欄是成交量」那種測試——期望值會跟程式碼來自同一個推導，
> 同義反覆，推錯了測試也不會紅。

那是誠實的，但也代表**最重要的東西沒有被測到**。欄位位置推錯的後果是
記下錯的成交口數 → 下午平錯量 → 開出反向部位，而它不會報錯。

2026-08-17 用 `tools/verify_order_path.py` 監聽整個交易日，抓到使用者手動
下的一筆微台單所產生的**兩則真實回報**。那兩列存在 `fixtures/` 裡，
成為**獨立於我的推導之外**的事實來源——期望值來自交易所，不是來自我。

於是本檔案分成兩半：

  1. 對著真實資料斷言欄位位置（新增，能真的失敗）
  2. 形狀檢查：欄位對不上時要承認看不懂，不可以硬轉成數字（原有）

第 2 部分沒有因為第 1 部分而變得多餘。真實樣本只有一筆商品、一種情境；
形狀檢查守的是「遇到沒見過的格式時不要亂猜」。
"""

import os

from broker.capital_wire import parse_reply_row, summarize_fills

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "onnewdata-real-2026-08-17.txt")


def _real_rows():
    """2026-08-17 實際收到的兩則回報（已去識別化，欄位位置與結構未動）。

    來源：使用者 08:53 在群益 APP 手動下的 1 口微台。
    第一列是委託回報（`N`），第二列是成交回報（`D`）。
    """
    with open(_FIXTURE, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


_CROSSING_ZERO = os.path.join(os.path.dirname(__file__), "fixtures",
                              "onnewdata-crossing-zero-2026-08-24.txt")


def _crossing_zero_rows():
    """2026-08-24 跨越零那天 OS 自己送出的四列（進場、出場各一對）。"""
    with open(_CROSSING_ZERO, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh
                if ln.strip() and not ln.startswith("#")]


# --- 欄位 [6]：買賣別 ＋ 倉別 ＋ 委託條件的三合一編碼 ---
#
# 2026-08-24 發現。它推翻了 2026-08-21 記下的判斷（「要知道券商實際用了
# 哪種倉別，得在出場後再查一次回報」）——那個資訊一直就在推播裡，
# 而且在**委託**那一列就到了，不必等成交、不必再查。
#
# ⚠️ 這些斷言的期望值來自**交易所回來的資料**，不是我的推導。
#    七個樣本跨四天、跨「使用者自己下的單」與「OS 送的單」。

_POSITION_CODE = 6


def _code(row: str) -> str:
    return row.split(",")[_POSITION_CODE]


def test_the_position_code_encodes_side_then_new_or_offset_then_order_condition():
    """`SOI10` = 賣出（S）＋ 平倉（O）＋ IOC（I）。"""
    entry, _, exit_, _ = _crossing_zero_rows()
    assert _code(entry) == "SOI10", "進場那筆是賣出、平倉（跨越零）、IOC"
    assert _code(exit_) == "BNI10", "出場那筆是買進、新倉（帳上空手）、IOC"


def test_the_position_code_is_the_brokers_answer_not_our_request():
    """**兩筆送出的都是 `sNewClose=2`（自動），編碼卻不同。**

    我們從來沒有請求過「平倉」——`O` 是券商解析淨部位之後的結論。
    這是整組樣本裡唯一能排除「回報的差異來自請求的差異」的一組：
    2026-08-21 那天送出的 `sNewClose` 本來就不同（0 與 2）。
    """
    entry, _, exit_, _ = _crossing_zero_rows()
    assert _code(entry)[1] == "O"
    assert _code(exit_)[1] == "N"
    assert _code(entry)[1] != _code(exit_)[1], (
        "同一個請求值卻得到相同的倉別，那就沒有證據說它是券商的判斷"
    )


def test_the_position_code_arrives_on_the_acknowledgement_not_only_the_fill():
    """**委託那一列就有倉別**，不必等成交。

    這是「不必再查一次回報」的根據：程式在收到委託回報的那一刻
    就知道券商把這筆當成新倉還是平倉了。
    """
    ack, fill = _crossing_zero_rows()[:2]
    assert ack.split(",")[2] == "N", "第一列應該是委託回報"
    assert fill.split(",")[2] == "D", "第二列應該是成交回報"
    assert _code(ack) == _code(fill), "委託與成交兩列的編碼應該一致"


def test_the_users_own_app_order_uses_rod_while_ours_uses_ioc():
    """第 3 字分得出 ROD 與 IOC —— 免費驗證了 ADR-0003 真的送出 IOC。

    2026-08-17 那筆是使用者在 APP 下的（`BNR20`，R=ROD），
    我們送的一律是 `I`。這一格哪天變成 R，代表 `sTradeType` 被改掉了，
    而市價單配 ROD 在群益是不合法的組合。
    """
    assert _code(_real_rows()[0])[2] == "R", "使用者 APP 的單是 ROD"
    for row in _crossing_zero_rows():
        assert _code(row)[2] == "I", f"OS 送出的單必須是 IOC：{_code(row)}"


def test_the_push_and_the_query_agree_on_the_position_type():
    """**跨格式交叉驗證**：推播 `[6]` 的第 2 字 ⟺ 查詢回報的 `[27]`。

    兩條完全獨立的管道（Solace 推播 vs 請求／回應），格式互不相容，
    卻對同一筆委託給出同樣的倉別。這是這個編碼含義最強的證據——
    也是唯一不靠我的推導的證據。
    """
    reports = os.path.join(os.path.dirname(__file__), "fixtures",
                           "reports-crossing-zero-2026-08-24.txt")
    with open(reports, encoding="utf-8") as fh:
        fills = [ln.strip() for ln in fh
                 if ln.startswith("TF,") and len(ln.split(",")) <= 55]
    by_book = {r.split(",")[7]: r.split(",")[27] for r in fills}

    for row in _crossing_zero_rows():
        book = row.split(",")[10]
        if book not in by_book:
            continue
        assert _code(row)[1] == by_book[book], (
            f"{book}：推播說 {_code(row)[1]!r}，查詢回報說 {by_book[book]!r}"
        )


# --- 解析器把倉別讀出來（2026-08-25 新增）---


def test_the_parser_exposes_the_position_type():
    """`parse_reply_row` 讀得出券商說它做了什麼：開倉（N）或平倉（O）。

    這是「進出場的倉別必須相反」那個檢查的原料。在它之前這個欄位只被
    fixture 的說明記著，沒有任何程式讀它。
    """
    entry, _, exit_, _ = _crossing_zero_rows()
    assert parse_reply_row(entry).position_type == "O"
    assert parse_reply_row(exit_).position_type == "N"


def test_an_unreadable_position_code_becomes_empty_not_a_guess():
    """看不懂就回空字串，**不可以猜**。

    空字串的意思是「不知道」，而上層對「不知道」的處理是**跳過檢查**——
    猜一個值的話，那個檢查會拿假資料去比對，然後發出假警報或漏掉真的問題。
    兩種都比不檢查更糟。
    """
    entry = _crossing_zero_rows()[0].split(",")
    for broken in ("", "S", "XYZ10", "SQI10"):
        entry[6] = broken
        assert parse_reply_row(",".join(entry)).position_type == "", broken


def test_the_summary_carries_the_position_type_of_the_fill():
    """彙整要把倉別帶出來——上層拿到的是 `FillSummary`，不是原始列。"""
    rows = _crossing_zero_rows()
    seq = rows[0].split(",")[0]
    assert summarize_fills(rows, seq).position_type == "O"


# --- 對著真實資料：欄位位置 ---


def test_a_real_order_acknowledgement_is_parsed():
    """委託回報。期望值直接讀自交易所回來的那一列，不是我數出來的。"""
    ack = parse_reply_row(_real_rows()[0])
    assert ack is not None, "真實的委託回報必須認得出來"
    assert ack.type == "N"
    assert ack.failed is False
    assert ack.qty == 1, "使用者下的是 1 口"


def test_a_real_fill_is_parsed_with_the_right_quantity():
    """成交回報——**這是整個系統最貴的一個欄位**。

    抓錯的話：記下錯的成交口數 → 13:40 平錯量 → 多平的部分變成反向新倉。
    而且不會報錯。
    """
    fill = parse_reply_row(_real_rows()[1])
    assert fill is not None
    assert fill.type == "D"
    assert fill.qty == 1, "實際成交 1 口"


def test_a_real_fill_row_has_no_keyno_and_falls_back_to_the_trailing_seqno():
    """**成交回報的第一欄是空的。**

    官方文件寫著「國內期選、海外市場：成交單無此欄，可使用新增的 SeqNo 比對」，
    真實資料證實了：委託回報 `[0]` 有 13 碼序號，成交回報 `[0]` 是空字串，
    序號改出現在最後一欄。

    `parse_reply_row` 的 `fields[0] or fields[-1]` 正好接住這件事——
    但那是**推導時猜對的**，直到現在才有證據。
    """
    ack, fill = (parse_reply_row(r) for r in _real_rows())
    assert _real_rows()[1].split(",")[0] == "", "成交列的 KeyNo 確實是空的"
    assert ack.seq == fill.seq, "兩列必須配得起來，否則 summarize_fills 加總不到一起"
    assert ack.seq, "序號不可以是空的"


def test_both_real_rows_belong_to_the_same_order():
    """`summarize_fills` 用整列子字串比對序號。真實資料要能通過那個比對。"""
    from broker.capital_wire import summarize_fills

    rows = _real_rows()
    seq = parse_reply_row(rows[0]).seq
    summary = summarize_fills(rows, seq)
    assert summary.matched_rows == 2, "兩列都該被認成同一筆委託"
    assert summary.filled_lots == 1


# --- 形狀檢查：看不懂就回 None，絕不猜 ---
#
# 有了真實樣本之後這一部分仍然必要：樣本只涵蓋一種商品、一種情境，
# 而回報事件裡會混進證券、海期等格式完全不同的東西。


def _row(row_type, err="N", qty="2", seq="SEQ0000000001"):
    """組一列格式合法的 TF 回報。欄位位置比照真實資料。"""
    fields = [""] * 48
    fields[0] = seq
    fields[1] = "TF"
    fields[2] = row_type
    fields[3] = err
    fields[20] = qty
    fields[47] = seq
    return ",".join(fields)


def test_a_row_from_another_market_is_ignored():
    """證券（TS）與海期（OF）的回報也會進到同一個事件，欄位定義卻不同。"""
    assert parse_reply_row(_row("D", **{})) is not None      # 對照：TF 認得
    assert parse_reply_row(_row("D").replace(",TF,", ",TS,", 1)) is None


def test_a_truncated_row_is_ignored():
    """欄位不夠長時，索引會抓到不存在的位置或錯位的內容。"""
    assert parse_reply_row("SEQ1,TF,D,N") is None


def test_a_row_whose_quantity_is_not_a_number_is_ignored():
    """這是**欄位位置若推錯時最可能出現的徵兆**。

    真實資料已經證實第 20 欄是數量，但格式日後可能改版。
    撈到日期、代碼或空字串時，唯一安全的行為是承認看不懂。
    """
    assert parse_reply_row(_row("D", qty="13:45:01")) is None
    assert parse_reply_row(_row("D", qty="")) is None
    assert parse_reply_row(_row("D", qty="TM2608")) is None


def test_an_unknown_row_type_is_ignored():
    """Type 只有 N/C/U/P/D/B/S 七種。撈到別的字元就代表欄位對錯位了。"""
    assert parse_reply_row(_row("X")) is None
    assert parse_reply_row(_row("20260817")) is None


def test_a_rejected_order_is_flagged():
    """OrderErr = Y 代表這筆委託失敗，不可以被當成成交。"""
    assert parse_reply_row(_row("N", err="Y")).failed is True


def test_an_empty_row_is_ignored():
    assert parse_reply_row("") is None
