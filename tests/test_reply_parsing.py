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

from broker.capital_wire import parse_reply_row

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "onnewdata-real-2026-08-17.txt")


def _real_rows():
    """2026-08-17 實際收到的兩則回報（已去識別化，欄位位置與結構未動）。

    來源：使用者 08:53 在群益 APP 手動下的 1 口微台。
    第一列是委託回報（`N`），第二列是成交回報（`D`）。
    """
    with open(_FIXTURE, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


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
