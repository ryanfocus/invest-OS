"""成交查詢後備 —— 把「不確定」從常見變罕見。

推播（`OnNewData`）是**一次性**的：訊息送出來的那一瞬間沒接到就永遠消失。
它需要 Solace 回報連線正常、事件註冊成功、訊息幫浦持續在跑——任何一環出事，
今天就變成「不確定」：發 Discord 叫人去看帳戶、下午拒絕自動平倉、**整天癱瘓**。

查詢走完全不同的路（不同元件、不同連線、請求／回應），失效原因幾乎不重疊。
所以推播沒來時**先問一次**再舉手投降。

## 這個檔案守的兩件事

1. **查到了就自動復原**——使用者完全不必介入
2. **查不到就維持原樣**——絕不可以因為「查了」就假裝知道

第 2 點比第 1 點重要。查詢回 `None` 的意思是「還是不知道」，
把它當成 0 的話，實際成交的部位就沒有人知道、13:40 不會去平，直接進夜盤。
"""

from datetime import date

import pytest

from broker import BUY, FillUnknown, MTX_CODE, OpenPrices, SELL
from broker.fake import FakeBroker
from conftest import CONTRACTS, RecordingNotifier, make_config, observations_path, state_path
from main import run_entry, run_exit
from state import CONFIRMED, UNCERTAIN, PositionRecord, read_position, write_position, UNCERTAIN_ENTRY

D = date(2026, 8, 18)
OPENS = OpenPrices(tx=42331, mtx=42298, tmf=42265)


def _entry(*, query_result, lots=2, **kw):
    """跑一次進場，其中送單會拋 FillUnknown（推播沒來）。"""
    broker = FakeBroker(
        script=[OPENS], contracts=CONTRACTS,
        order_error=FillUnknown("逾時未收到回報", order_seq="SEQ0000000001"),
        query_result=query_result,
    )
    notifier = RecordingNotifier()
    outcome = run_entry(
        make_config(auto_order_enabled=True, order_lots=lots, **kw),
        today=D, broker=broker, notify=notifier, sleep=lambda _s: None,
        state_path=state_path(), observations_path=observations_path(),
        fetch_official=lambda day: None,
    )
    return outcome, notifier, broker


def _uncertain_record(**over):
    base = dict(
        trading_day=20260818, product=MTX_CODE, order_code="MTX08",
        contract_month="202608", last_trading_day=20260916,
        side=BUY, lots=None, requested_lots=2,
        status=UNCERTAIN, uncertain_stage=UNCERTAIN_ENTRY, order_seq="SEQ0000000001",
    )
    base.update(over)
    return PositionRecord(**base)


def _exit(*, query_result, record=None, **kw):
    write_position(record or _uncertain_record(), path=state_path())
    broker = FakeBroker(query_result=query_result, **kw)
    notifier = RecordingNotifier()
    outcome = run_exit(
        make_config(auto_order_enabled=True),
        today=D, broker=broker, notify=notifier, sleep=lambda _s: None,
        state_path=state_path(),
    )
    return outcome, notifier, broker


# ─────────────────────────────────────────────────────────
# 進場側（08:50）：推播沒來 → 查一次
# ─────────────────────────────────────────────────────────


def test_a_successful_query_records_a_confirmed_position():
    """推播沒來但查得到 → **狀態是已確認，不是不確定**。

    這是本張票的全部意義：使用者不必介入，下午照常自動平倉。
    """
    _entry(query_result=2)
    record = read_position(path=state_path())
    assert record.status == CONFIRMED
    assert record.lots == 2


def test_the_query_result_is_the_lot_count_not_the_request():
    """委託 3 口只成交 1 口 → 記 1 口。記 3 口的話下午會多平 2 口，
    而那 2 口變成方向相反、沒人管的新倉。"""
    _entry(query_result=1, lots=3)
    assert read_position(path=state_path()).lots == 1


def test_a_recovered_entry_does_not_alert():
    """查到了就沒事發生過。還發告警的話，使用者會白跑一趟去看帳戶——
    而狼來了喊多了，真正需要他看的那次就會被忽略。"""
    _, notifier, _ = _entry(query_result=2)
    assert "收不到成交回報" not in notifier.text


def test_a_recovered_entry_finishes_cleanly():
    _outcome, _, _ = _entry(query_result=2)
    assert _outcome.exit_code == 0


def test_the_query_uses_the_sequence_number_from_the_failed_order():
    """**查錯序號比查不到更糟**——查回來的會是別人的成交。"""
    _, _, broker = _entry(query_result=2)
    assert broker.query_calls == [
        {"order_seq": "SEQ0000000001", "trading_day": 20260818, "requested_lots": 2}
    ]


def test_a_query_that_finds_nothing_still_records_uncertain():
    """**查不到就維持原樣。** 查了不代表知道了。"""
    _entry(query_result=None)
    record = read_position(path=state_path())
    assert record.status == UNCERTAIN
    assert record.lots is None


def test_a_query_that_finds_nothing_still_alerts():
    _, notifier, _ = _entry(query_result=None)
    assert "收不到成交回報" in notifier.text


def test_a_query_that_finds_nothing_still_exits_with_an_error():
    outcome, _, _ = _entry(query_result=None)
    assert outcome.exit_code == 1


def test_a_zero_fill_from_the_query_means_no_position():
    """查到「確定 0 口」是個明確的答案——沒有部位，不必寫記錄。

    ⚠️ 與「查不到」（`None`）完全不同。`None` 要記成不確定並告警，
    0 則是今天什麼都沒發生。兩者若混為一談，其中一種一定會被錯待。
    """
    _entry(query_result=0)
    assert read_position(path=state_path()) is None


def test_the_query_is_only_asked_once():
    """文件限制每次查詢間需間隔五秒，而 08:50 沒有重試的餘裕。"""
    _, _, broker = _entry(query_result=None)
    assert len(broker.query_calls) == 1


def test_a_successful_order_never_queries():
    """推播正常時不該多問——查詢是後備，不是主要管道。

    ⚠️ `query_result` 沒設定時假 broker 會主動 AssertionError，
    所以這條若不小心走到查詢會失敗而不是靜靜通過。
    """
    broker = FakeBroker(script=[OPENS], contracts=CONTRACTS)
    run_entry(
        make_config(auto_order_enabled=True), today=D, broker=broker,
        notify=RecordingNotifier(), sleep=lambda _s: None,
        state_path=state_path(), observations_path=observations_path(),
        fetch_official=lambda day: None,
    )
    assert broker.query_calls == []


# ─────────────────────────────────────────────────────────
# 出場側（13:40）：不確定的記錄 → 先查再決定
# ─────────────────────────────────────────────────────────


def test_an_uncertain_record_that_resolves_is_closed_automatically():
    """早上不確定、下午查到了 → **照常平倉，使用者完全不必介入**。

    這是 ticket 09 對使用者最有感的一條：原本整天癱瘓的情況變成自動復原。
    """
    _, _, broker = _exit(query_result=2)
    assert len(broker.orders) == 1
    assert broker.orders[0].side == SELL
    assert broker.orders[0].lots == 2


def test_a_resolved_record_is_marked_exited():
    _exit(query_result=2)
    record = read_position(path=state_path())
    assert record.exited is True
    assert record.status == CONFIRMED


def test_an_uncertain_record_that_stays_unknown_sends_nothing():
    """查不到 → 走 ticket 06 的老路：**一張單都不送**，發 Discord 要人處理。

    不知道持有幾口就下單，可能開出反向新倉，比什麼都不做更糟。
    """
    _, notifier, broker = _exit(query_result=None)
    assert broker.orders == []
    assert "不確定持有幾口" in notifier.text


def test_the_exit_query_uses_the_recorded_sequence_number():
    _, _, broker = _exit(query_result=2)
    assert broker.query_calls[0]["order_seq"] == "SEQ0000000001"
    assert broker.query_calls[0]["requested_lots"] == 2


def test_a_query_returning_zero_means_there_is_nothing_to_close():
    """早上確定沒成交 → 沒有部位 → 什麼都不用做，也不必告警。"""
    _, notifier, broker = _exit(query_result=0)
    assert broker.orders == []
    assert "不確定持有幾口" not in notifier.text


def test_a_confirmed_record_never_queries():
    """狀態已確認就沒有東西要查。多查一次要多花 5 秒（文件規定的間隔），
    而 13:40 的每一秒都在逼近收盤。"""
    _, _, broker = _exit(query_result=None, record=_uncertain_record(
        lots=2, status=CONFIRMED, uncertain_stage=""))
    assert broker.query_calls == []
    assert len(broker.orders) == 1


def test_the_switch_being_off_stops_the_query_touching_the_broker():
    """**開關關著就不查。**

    查詢會登入並初始化下單元件（`_ensure_order_ready` → SKOrderLib 初始化、
    `SKReplyLib_ConnectByID`、`GetUserAccount`），而 SPEC 使用者故事 38 要求
    「只發 Discord、不下單」的模式下**完全不呼叫任何下單 API**——
    使用者關掉開關通常正是想切斷程式與券商的連線。

    ⚠️ 這條測試的初版寫反了（斷言「開關關著仍然要查」），而且它**根本沒把
    開關關掉**——`_exit()` 把 `auto_order_enabled=True` 寫死，`**kw` 是轉給
    FakeBroker 的。所以那條斷言恆真，什麼都沒守到。
    """
    write_position(_uncertain_record(), path=state_path())
    broker = FakeBroker(query_result=2)
    notifier = RecordingNotifier()
    run_exit(
        make_config(auto_order_enabled=False),
        today=D, broker=broker, notify=notifier, sleep=lambda _s: None,
        state_path=state_path(),
    )
    assert broker.query_calls == [], "開關關著卻仍然查詢＝碰了下單元件"
    assert broker.login_calls == 0, "開關關著卻仍然登入"
    assert broker.orders == []
    assert "不確定持有幾口" in notifier.text, "仍然要講清楚帳上可能有部位"


# ─────────────────────────────────────────────────────────
# 🔴 出場單的不確定：**一張單都不准再送**
# ─────────────────────────────────────────────────────────
#
# 2026-08-19 code-review 抓到的迴歸，實測重現過：
#
#   13:40 送出場單 → FillUnknown → 記錄變成 UNCERTAIN，
#   而 `order_seq` 被換成**出場單**的序號
#   → 重跑時拿它去查，查到出場單成交的 N 口
#   → 程式當成「早上成交 N 口」→ 再送一筆等量反向委託
#   → 帳上早就平掉了，第二筆是裸露的反向新倉，沒人管地進夜盤
#   → 而狀態檔還說「已了結」
#
# ticket 09 之前這條路徑是直接拒絕送單並發 Discord，是安全的。


def _exit_uncertain_record(**over):
    from state import UNCERTAIN_EXIT
    base = dict(lots=None, status=UNCERTAIN, uncertain_stage=UNCERTAIN_EXIT,
                order_seq="EXIT_SEQ_999")
    base.update(over)
    return _uncertain_record(**base)


def test_an_uncertain_exit_never_sends_another_order():
    """**本組最重要的一條。** 那筆可能已經成交了。"""
    _, _, broker = _exit(query_result=2, record=_exit_uncertain_record())
    assert broker.orders == []


def test_an_uncertain_exit_does_not_even_query():
    """查詢在這裡幫不上忙，而且會誤導——`order_seq` 是**出場單**的序號，
    查到的成交是出場單自己的，看起來卻像「早上成交了 N 口」。"""
    _, _, broker = _exit(query_result=2, record=_exit_uncertain_record())
    assert broker.query_calls == []


def test_an_uncertain_exit_asks_a_human_to_check_the_account():
    """訊息要講「先確認帳戶再決定要不要補單」，不是「請照著送這張單」——
    盲送的後果就是反向新倉。"""
    _, notifier, _ = _exit(query_result=2, record=_exit_uncertain_record())
    assert "確認帳戶實際部位" in notifier.text


def test_an_uncertain_exit_exits_with_an_error():
    outcome, _, _ = _exit(query_result=2, record=_exit_uncertain_record())
    assert outcome.exit_code == 1


def test_a_full_exit_failure_cycle_does_not_double_up():
    """端到端：13:40 出場回報沒到 → 重跑 → **第二次一張單都不送**。

    走的是真正的資料流（第一輪自己寫的記錄，第二輪自己讀回來），
    不是手工組的記錄——手工組的話就測不到 `uncertain_stage` 有沒有真的被寫進去。
    """
    write_position(_uncertain_record(lots=2, status=CONFIRMED, uncertain_stage=""),
                   path=state_path())
    first = FakeBroker(order_error=FillUnknown("逾時", order_seq="EXIT_SEQ_999"))
    run_exit(make_config(auto_order_enabled=True), today=D, broker=first,
             notify=RecordingNotifier(), sleep=lambda _s: None, state_path=state_path())
    assert len(first.orders) == 1, "第一輪應該有送單"

    second = FakeBroker(query_result=2)
    run_exit(make_config(auto_order_enabled=True), today=D, broker=second,
             notify=RecordingNotifier(), sleep=lambda _s: None, state_path=state_path())
    assert second.orders == [], "重跑不可以再送一次——帳上可能早就平掉了"
    assert read_position(path=state_path()).exited is False,         "沒有證據就不可以標成已了結"
