"""成交回報的彙整 —— 「我到底成交了幾口」。

這個問題答錯的代價是不對稱的，所以規則不對稱：

  **只要有成交，就一定要記下來。** 即使同一批回報裡還有被拒絕的列。
  市價 IOC 部分成交是常態：成交 2 口、剩餘 1 口被取消，那是正常結果不是失敗。
  把它當成失敗而不寫狀態檔，帳上那 2 口就沒有人知道，13:40 不會去平，直接進夜盤。

  **完全沒成交才談失敗。** 那時候確實沒有部位產生，`OrderFailed` 才名副其實。

  **認不出是哪一筆時，不猜。** 回報事件是共用的：手動下的單、連線時沖進來的
  前一批回報，都會出現在同一個緩衝區。加總到別人的成交上，下午就會平錯口數。
"""

from broker.capital import summarize_fills


def _row(row_type, err="N", qty="2", seq="SEQ0000000001"):
    """組一列格式合法的 TF 回報。內容不代表真實資料，只用來測彙整規則。"""
    fields = [""] * 49
    fields[0] = seq
    fields[1] = "TF"
    fields[2] = row_type
    fields[3] = err
    fields[20] = qty
    fields[47] = seq
    return ",".join(fields)


SEQ = "SEQ0000000001"


# --- 有成交就要記下來 ---


def test_a_single_fill_is_counted():
    assert summarize_fills([_row("D", qty="2")], SEQ).filled_lots == 2


def test_multiple_fills_of_the_same_order_are_added_up():
    """市價單可能分批成交，每一批一列。"""
    rows = [_row("D", qty="1"), _row("D", qty="2")]
    assert summarize_fills(rows, SEQ).filled_lots == 3


def test_a_partial_fill_followed_by_a_cancellation_still_counts_the_fill():
    """**這是這個檔案存在的理由。**

    IOC 的正常結局就是「成交一部分、剩下的取消」。取消列在成交列之後才到，
    若因此判定整筆失敗，已經成交的部位就不會被寫進狀態檔——
    帳上有部位而程式不知道，是本系統最貴的失效模式。
    """
    rows = [_row("D", qty="2"), _row("C", err="Y", qty="1")]
    summary = summarize_fills(rows, SEQ)
    assert summary.filled_lots == 2
    assert summary.rejected is False, "有成交就不算失敗，剩餘量被取消是 IOC 的常態"


def test_order_of_rows_does_not_change_the_answer():
    """回報到達順序不保證，答案不可以取決於它。"""
    a = summarize_fills([_row("D", qty="2"), _row("C", err="Y")], SEQ)
    b = summarize_fills([_row("C", err="Y"), _row("D", qty="2")], SEQ)
    assert a.filled_lots == b.filled_lots == 2


# --- 完全沒成交才算失敗 ---


def test_a_rejection_with_no_fill_is_a_failure():
    """這時候確實沒有部位產生，OrderFailed 才名副其實。"""
    summary = summarize_fills([_row("N", err="Y", qty="3")], SEQ)
    assert summary.filled_lots == 0
    assert summary.rejected is True


def test_a_full_cancellation_with_no_fill_is_not_a_position():
    """IOC 完全沒成交（瞬間沒有對手價）：不是錯誤，但也沒有部位。"""
    summary = summarize_fills([_row("C", qty="3")], SEQ)
    assert summary.filled_lots == 0
    assert summary.rejected is False


# --- 認不出是哪一筆就不猜 ---


def test_fills_belonging_to_another_order_are_not_counted():
    """回報緩衝區是共用的。手動下的單、連線時沖進來的前一批回報都在裡面。"""
    rows = [_row("D", qty="5", seq="SOMEONE_ELSE")]
    summary = summarize_fills(rows, SEQ)
    assert summary.filled_lots == 0
    assert summary.matched_rows == 0, "認不出是我們的單，就一列都不算"


def test_only_the_matching_order_is_counted_when_both_are_present():
    rows = [_row("D", qty="5", seq="SOMEONE_ELSE"), _row("D", qty="2", seq=SEQ)]
    assert summarize_fills(rows, SEQ).filled_lots == 2


def test_unparsable_rows_are_skipped_without_affecting_the_total():
    rows = ["垃圾", "", _row("D", qty="2"), "TS,not,ours"]
    assert summarize_fills(rows, SEQ).filled_lots == 2


# --- 這筆委託結束了沒？（code-review 2026-08-15 發現的漏洞）---
#
# 「收到回報」不等於「這筆委託結束了」。IOC 的回報依序是：
#
#   N 委託回報  ← 交易所收下了，**單還在市場上**
#   D 成交回報  ← 成交一批（可能有好幾列）
#   C 取消回報  ← 剩餘量取消，到這裡才真的結束
#
# 原本的等待條件是「認出至少一列」，而 N 一到就滿足了——於是程式在單還可能
# 正在成交的時候就收工，回報 0 口、不寫狀態檔、不發通知。
# 那正是這張 ticket 第一句話要擋的事：絕不在不確定自己持有什麼的狀態下靜默結束。


def test_an_acknowledgement_alone_does_not_settle_the_order():
    """只收到委託回報時，這筆委託**還沒結束**——單還在市場上。"""
    summary = summarize_fills([_row("N", qty="0")], SEQ)
    assert summary.is_settled(requested_lots=1) is False


def test_a_cancellation_settles_the_order():
    """IOC 的剩餘量被取消 → 不會再有成交了 → 確定結束。"""
    summary = summarize_fills([_row("N", qty="1"), _row("C", qty="1")], SEQ)
    assert summary.is_settled(requested_lots=1) is True
    assert summary.filled_lots == 0, "確定沒成交，這是合法的結果"


def test_a_full_fill_settles_the_order_even_without_a_cancellation():
    """全部成交就沒有剩餘量可取消，等 C 會等到逾時。"""
    summary = summarize_fills([_row("N", qty="2"), _row("D", qty="2")], SEQ)
    assert summary.is_settled(requested_lots=2) is True
    assert summary.filled_lots == 2


def test_a_partial_fill_without_a_cancellation_is_not_settled():
    """成交 1 口、還有 1 口在市場上 → 還沒結束，不可以就這樣回報 1 口。"""
    summary = summarize_fills([_row("N", qty="2"), _row("D", qty="1")], SEQ)
    assert summary.is_settled(requested_lots=2) is False


def test_a_partial_fill_followed_by_a_cancellation_is_settled():
    """成交 1 口、剩餘 1 口取消 → 結束，實際持有 1 口。"""
    rows = [_row("N", qty="2"), _row("D", qty="1"), _row("C", qty="1")]
    summary = summarize_fills(rows, SEQ)
    assert summary.is_settled(requested_lots=2) is True
    assert summary.filled_lots == 1


def test_a_rejection_settles_the_order():
    """被拒就是結束，而且確定沒有部位。"""
    summary = summarize_fills([_row("N", err="Y", qty="1")], SEQ)
    assert summary.is_settled(requested_lots=1) is True
    assert summary.rejected is True


def test_nothing_at_all_is_not_settled():
    assert summarize_fills([], SEQ).is_settled(requested_lots=1) is False


def test_no_rows_at_all_means_nothing_is_known():
    """回報還沒到。**這不等於沒有成交**——委託可能已經成交、只是回報慢了。

    分辨的責任在呼叫端（ticket 05 會把它記為「不確定」並要求人工確認）；
    這裡只負責誠實回報「我一列都沒認出來」。
    """
    summary = summarize_fills([], SEQ)
    assert summary.matched_rows == 0
    assert summary.filled_lots == 0
    assert summary.rejected is False
