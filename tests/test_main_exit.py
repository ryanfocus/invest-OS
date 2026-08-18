"""出場流程 —— 把早上開的部位平掉。

這個檔案守的是「部位不會過夜」。失敗的代價不對稱：

  多平了   → 開出一個方向相反、沒人管的新倉
  少平了   → 剩下的口數進夜盤（15:00–05:00），使用者不知情

所以斷言集中在**平了幾口、什麼方向**，以及**什麼情況下一口都不准平**。

出場刻意**不查商品清單**：下單代碼與最後交易日在 08:50 就知道了，
都寫在狀態檔裡。13:40 再去連一次報價主機只是多一個會失敗的地方，
而那時候失敗的代價是部位過夜。
"""

from datetime import date

import pytest

from broker import BUY, EXIT, MTX_CODE, SELL
from broker.fake import FakeBroker
from conftest import RecordingNotifier, make_config, state_path
from main import run_exit
from state import UNCERTAIN, PositionRecord, read_position, write_position

D = date(2026, 8, 10)            # 一般交易日（週一）
SETTLEMENT = date(2026, 8, 19)   # 狀態檔記的最後交易日


def _record(**overrides) -> PositionRecord:
    """早上進場後留下的記錄。預設是「做多 2 口、已確認」。"""
    base = dict(
        trading_day=20260810,
        product=MTX_CODE,
        order_code="MTX08",
        contract_month="202608",
        last_trading_day=20260819,
        side=BUY,
        lots=2,
        requested_lots=2,
        order_seq="SEQ0000000001",
    )
    base.update(overrides)
    return PositionRecord(**base)


def _run(record=None, cfg=None, broker=None, today=D, fills=None, order_error=None):
    if record is not None:
        write_position(record, path=state_path())
    broker = broker or FakeBroker(fills=fills, order_error=order_error)
    notifier = RecordingNotifier()
    outcome = run_exit(
        cfg or make_config(auto_order_enabled=True),
        today=today,
        broker=broker,
        notify=notifier,
        sleep=lambda _s: None,
        state_path=state_path(),
    )
    return outcome, notifier, broker


# --- 正常出場 ---


def test_exit_sends_the_opposite_side():
    """進場買，出場就賣。方向寫反等於部位加倍，不是平倉。"""
    _, _, broker = _run(_record(side=BUY))
    assert broker.orders[0].side == SELL


def test_exit_of_a_short_entry_buys_back():
    _, _, broker = _run(_record(side=SELL))
    assert broker.orders[0].side == BUY


def test_exit_lots_come_from_the_actual_fill_not_the_request():
    """早上委託 3 口只成交 2 口 → 下午平 2 口。

    平 3 口的話：手上那 2 口被平掉，多出來的 1 口變成方向相反的**新倉**，
    而且沒有人會去平它。
    """
    _, _, broker = _run(_record(lots=2, requested_lots=3))
    assert broker.orders[0].lots == 2


def test_exit_uses_the_order_code_from_the_state_file():
    """出場不查商品清單——代碼在 08:50 就記下來了。"""
    _, _, broker = _run(_record())
    assert broker.orders[0].order_code == "MTX08"
    assert broker.orders[0].contract_month == "202608"


def test_exit_order_is_marked_as_an_exit():
    """倉別由 intent 決定，而進出場的正確值不一樣（見 test_order_fields.py）。"""
    _, _, broker = _run(_record())
    assert broker.orders[0].intent == EXIT


def test_a_successful_exit_is_recorded():
    """狀態檔要留下「已經平掉了」，否則隔日對帳分不出「沒平」與「平了但沒記」。"""
    _run(_record())
    assert read_position(path=state_path()).exited is True


def test_a_routine_exit_sends_no_discord():
    """使用者要求每天只有一則訊息（早上那則訊號）。例行出場不打擾。"""
    _, notifier, _ = _run(_record())
    assert notifier.sent == []


def test_a_successful_exit_ends_normally():
    outcome, _, _ = _run(_record())
    assert outcome.exit_code == 0


# --- 什麼都不做的情況 ---


def test_no_record_means_no_order():
    """今天沒進場（或訊號是不動作）→ 一口都不准平，否則憑空開出新部位。"""
    _, notifier, broker = _run(record=None)
    assert broker.orders == []
    assert notifier.sent == []


def test_yesterdays_record_is_not_todays_position():
    """隔夜殘留的舊記錄不是今天的部位。拿它去平會開出反向新倉。"""
    _, _, broker = _run(_record(trading_day=20260807))
    assert broker.orders == []


def test_an_already_exited_record_is_not_exited_again():
    """排程重試或人工重跑，不可以平第二次。"""
    _run(_record())
    _, _, second = _run(record=None)      # 狀態檔還在，已標記 exited
    assert second.orders == []


def test_a_non_trading_day_does_nothing():
    _, notifier, broker = _run(_record(), today=date(2026, 8, 8))   # 週六
    assert broker.orders == []
    assert notifier.sent == []
    assert broker.login_calls == 0


def test_the_switch_being_off_places_no_exit_order():
    """開關關閉時整條下單路徑都不該被走到——出場也一樣。"""
    cfg = make_config(auto_order_enabled=False)
    _, _, broker = _run(_record(), cfg=cfg)
    assert broker.orders == []


# --- 「不確定」：從 ticket 05 接手 ---


def test_an_uncertain_record_places_no_order():
    """**不知道持有幾口就下單，可能開出反向新倉，比什麼都不做更糟。**"""
    _, _, broker = _run(_record(lots=None, status=UNCERTAIN))
    assert broker.orders == []


def test_an_uncertain_record_asks_for_human_help():
    outcome, notifier, _ = _run(_record(lots=None, status=UNCERTAIN))
    assert notifier.sent != [], "不確定時絕不可以靜默結束"
    assert "MTX08" in notifier.text
    assert outcome.exit_code != 0


# --- 重試 ---


def test_it_recovers_when_a_later_attempt_succeeds():
    """前兩次失敗、第三次成功 → 正常完成，**不發告警**。

    不斷言間隔或呼叫次數——那是實作細節。只斷言「會再試」與「試完的結果」。
    """
    from broker import OrderFailed
    broker = FakeBroker(order_error=[OrderFailed("暫時性失敗"),
                                     OrderFailed("暫時性失敗"), None])
    outcome, notifier, _ = _run(_record(), broker=broker)
    assert outcome.exit_code == 0
    assert notifier.sent == [], "自己復原了就不必打擾使用者"
    assert read_position(path=state_path()).exited is True


def test_giving_up_alerts_with_enough_detail_to_act_on():
    """三次都失敗 → 使用者要能直接照著訊息手動平倉。"""
    from broker import OrderFailed
    broker = FakeBroker(order_error=OrderFailed("代碼 1038：保證金不足"))
    outcome, notifier, _ = _run(_record(side=BUY, lots=2), broker=broker)
    assert outcome.exit_code != 0
    # 斷言的是**整句指示**，不是零散的字元。只比 "2" in text 的話，
    # 日期 2026/08/10 本身就含 0、1、2——那條斷言不管口數多少都會過。
    assert "賣出 2 口 MTX08" in notifier.text


def test_a_failed_exit_is_not_recorded_as_exited():
    """標成已出場的話，隔日對帳會以為一切正常，而部位還在。"""
    from broker import OrderFailed
    broker = FakeBroker(order_error=OrderFailed("代碼 1038"))
    _run(_record(), broker=broker)
    assert read_position(path=state_path()).exited is False


# --- 結算日：**一張單都不送** ---
#
# 合約 13:30 停止交易，13:40 這一班送什麼都會被拒；而未平倉部位本來就會由
# 交易所以最後結算價現金交割，部位一定會平掉。所以正確行為不是「少重試幾次」，
# 是**根本不送**。策略作者的決定：「結算就讓他進結算吧」。
#
# 代價是當日出場價變成現貨指數的結算價，與用期貨價的回測有落差——已知且有界。


def test_settlement_day_sends_no_exit_order_at_all():
    """不是「少試幾次」，是一次都不試。

    用**下單紀錄**斷言而不是用結果：安排「第一次失敗、第二次會成功」時，
    舊的「只送一次」規則同樣會失敗收場，光看 exit_code 分不出兩者。
    """
    from broker import OrderFailed
    broker = FakeBroker(order_error=[OrderFailed("合約已停止交易"), None])
    outcome, _, _ = _run(_record(trading_day=20260819),
                         broker=broker, today=SETTLEMENT)
    assert broker.orders == [], "結算日不送出場委託"
    assert outcome.exit_code == 0, "交易所會結算，這不是失敗"


def test_settlement_day_still_tells_the_user_once():
    """一則告知。不發的話，使用者會以為 13:40 那班掛了。"""
    _, notifier, _ = _run(_record(trading_day=20260819), today=SETTLEMENT)
    assert len(notifier.sent) == 1
    text = notifier.sent[0]["content"]
    assert "結算" in text


def test_the_settlement_notice_does_not_ask_for_manual_action():
    """**這一則不能長得像告警。**

    舊行為在結算日會發出「請立刻手動送出：賣出 2 口 MTX08」——
    那是一件做不到的事，合約已經停止交易了。照著做只會在別的月份開新倉。
    """
    _, notifier, _ = _run(_record(trading_day=20260819), today=SETTLEMENT)
    text = notifier.sent[0]["content"]
    assert "手動" not in text
    assert "🚨" not in text


def test_the_settlement_notice_says_the_price_comes_from_the_spot_index():
    """結算價是現貨指數的平均，不是期貨價，所以當日損益跟回測對不起來。

    不先講的話，日後查帳時那個落差看起來會像程式算錯。
    """
    _, notifier, _ = _run(_record(trading_day=20260819), today=SETTLEMENT)
    text = notifier.sent[0]["content"]
    assert "現貨" in text


def test_settlement_marks_the_record_closed_by_settlement():
    """不標記的話，隔日對帳會看到一筆「還有 2 口沒出場」的記錄——
    那是個看起來很確定的錯誤，帳上其實已經被結算掉了。

    而且要與「我們自己平掉的」分得出來：兩者的出場價來源不同。
    """
    from state import BY_SETTLEMENT
    _run(_record(trading_day=20260819), today=SETTLEMENT)
    record = read_position(path=state_path())
    assert record.exited is True
    assert record.close_reason == BY_SETTLEMENT


def test_an_ordinary_exit_is_marked_as_closed_by_us():
    """對照組：一般日平掉的記錄，出場價是期貨價。"""
    from state import BY_EXIT
    _run(_record())
    assert read_position(path=state_path()).close_reason == BY_EXIT


def test_settlement_day_does_not_send_even_when_the_switch_is_off():
    """開關關著＋結算日：舊路徑會發「部位過夜沒人知道」的告警，但它不會過夜。

    走到開關那條的訊息會叫使用者去手動平倉——同樣是做不到的事。
    """
    _, notifier, broker = _run(_record(trading_day=20260819),
                               cfg=make_config(auto_order_enabled=False),
                               today=SETTLEMENT)
    assert broker.orders == []
    assert "手動" not in notifier.sent[0]["content"]


def test_settlement_day_with_an_uncertain_record_still_flags_the_uncertainty():
    """部位會照樣被結算，但**早上不知道成交幾口這件事，結算不會補上**。

    只發一則平靜的「已結算」而不提這個，使用者就永遠不會去對這筆帳。
    """
    _, notifier, broker = _run(
        _record(trading_day=20260819, lots=None, status=UNCERTAIN),
        today=SETTLEMENT,
    )
    assert broker.orders == [], "不確定持有幾口，更不能送單"
    text = notifier.sent[0]["content"]
    assert "不知道" in text and "對帳" in text


def test_an_ordinary_day_does_retry():
    """對照組：同一組安排，非結算日會再試一次而成功。"""
    from broker import OrderFailed
    broker = FakeBroker(order_error=[OrderFailed("暫時性失敗"), None])
    outcome, notifier, _ = _run(_record(), broker=broker)
    assert outcome.exit_code == 0
    assert notifier.sent == []


# --- 收不到出場回報：**絕不重試**（code-review 2026-08-16 發現的真實漏洞）---
#
# `FillUnknown` 的意思是「單送出去了，但不知道成交沒有」。出場時它特別危險：
# 第一筆若其實已經平掉了，重試就是再送一筆同樣大小的反向單——
# 平完之後繼續賣，於是開出一個**方向相反、沒人管的新倉**。
#
# 而出場的倉別是「自動」，券商不會擋——它會很樂意幫你開那個新倉。


def test_an_unconfirmed_exit_is_never_retried():
    """**這是本張最貴的一條。**

    broker/__init__.py 對 `FillUnknown` 的定義寫著「可能已經成交 → 絕不可以自動送單」。
    出場的重試迴圈原本把它跟 `OrderFailed` 一起攔下來重試，直接違反那條規則。
    """
    from broker import FillUnknown
    # 安排成「第一次不確定、第二次會成功」。有重試的話會拿到那個成功
    # 並標記為已出場——而那正是最危險的結果：帳上可能已經是反向新倉了。
    broker = FakeBroker(order_error=[FillUnknown("回報沒回來"), None])
    outcome, _, _ = _run(_record(lots=2), broker=broker)
    assert outcome.exited is False, "可能已經成交的單，一次都不准再送"
    assert read_position(path=state_path()).exited is False


def test_an_unconfirmed_exit_is_recorded_as_uncertain():
    """不知道平掉沒有 → 狀態要記成「不確定」，否則隔日對帳看不到這件事。

    維持 CONFIRMED 的話，記錄上會是「有 2 口、還沒出場」——
    那是一個**看起來很確定的錯誤**，而真相是「可能已經平了，也可能沒有」。
    """
    from broker import FillUnknown
    broker = FakeBroker(order_error=FillUnknown("回報沒回來"))
    _run(_record(lots=2), broker=broker)
    record = read_position(path=state_path())
    assert record.is_uncertain is True
    assert record.lots is None
    assert record.exited is False


def test_an_unconfirmed_exit_alerts_loudly():
    from broker import FillUnknown
    broker = FakeBroker(order_error=FillUnknown("回報沒回來"))
    outcome, notifier, _ = _run(_record(), broker=broker)
    assert outcome.exit_code != 0
    assert notifier.sent != []
    assert "MTX08" in notifier.text


def test_a_plain_order_failure_is_still_retried():
    """對照組：`OrderFailed` 是「確定沒送出去」，重試安全且應該做。"""
    from broker import OrderFailed
    broker = FakeBroker(order_error=[OrderFailed("暫時性失敗"), None])
    outcome, notifier, _ = _run(_record(), broker=broker)
    assert outcome.exit_code == 0, "第二次成功就算完成"
    assert notifier.sent == []


# --- 開關關閉但帳上有部位 ---


def test_the_switch_being_off_with_a_live_position_is_not_silent():
    """開關關著時進場不會寫記錄，所以「有記錄 + 開關關著」代表有人中途關掉了。

    這時候靜默結束等於讓部位過夜而沒有人知道。程式不該自作主張送單
    （使用者剛把開關關掉），但**必須講出來**。
    """
    cfg = make_config(auto_order_enabled=False)
    outcome, notifier, broker = _run(_record(), cfg=cfg)
    assert broker.orders == [], "開關關著就不送單"
    assert notifier.sent != [], "但帳上有部位這件事必須講"
    assert outcome.exit_code != 0


# --- 部分成交：從 ticket 05 接手 ---


def test_a_partial_exit_is_not_success():
    """要平 2 口只平掉 1 口 → 還有 1 口會過夜。不可以標記為已出場。"""
    outcome, _, _ = _run(_record(lots=2), fills=[1])
    assert read_position(path=state_path()).exited is False
    assert outcome.exit_code != 0


def test_a_partial_exit_says_how_many_lots_are_left():
    """訊息要能讓使用者直接手動處理，所以要講**剩幾口**，不是只說失敗。"""
    _, notifier, _ = _run(_record(lots=2), fills=[1])
    # 同樣斷言整句指示——而且口數是**殘留的 1**，不是原本的 2。
    # 照原本口數再送一次會多平，那正是這則訊息存在的理由。
    assert "賣出 1 口 MTX08" in notifier.text


def test_a_partial_exit_records_what_is_still_held():
    """殘留的口數要留在狀態檔，否則隔日對帳看到的是錯的部位。"""
    _run(_record(lots=2), fills=[1])
    assert read_position(path=state_path()).lots == 1


# --- 進場與出場串起來（光測 run_exit 證明不了資料真的傳得過去）---


def test_entry_records_the_last_trading_day_for_the_exit_to_use():
    """出場靠這個欄位判斷結算日，而它**只能由進場寫進去**。

    只用手工組的記錄測 `run_exit` 是證明不了這件事的——突變測試確認過：
    進場不寫這個欄位時，24 條出場測試全綠，但結算日永遠判不出來、照常重試。
    """
    from broker import OpenPrices
    from conftest import CONTRACTS
    from main import run_entry

    run_entry(
        make_config(auto_order_enabled=True),
        today=D,
        broker=FakeBroker(script=[OpenPrices(tx=42331, mtx=42298, tmf=42265)],
                          contracts=CONTRACTS),
        notify=RecordingNotifier(),
        sleep=lambda _s: None,
        state_path=state_path(),
    )
    record = read_position(path=state_path())
    assert record.last_trading_day == 20260819
    assert record.is_settlement_day(SETTLEMENT) is True


def test_settlement_day_entry_then_exit_sends_nothing():
    """端到端：結算日進場 → 13:40 那班**一張單都不送**。

    這條走的是真正的資料流（進場寫 `last_trading_day`、出場讀），
    不是手工組的記錄——突變測試確認過，進場漏寫那個欄位時，
    只用手工記錄的出場測試會全綠而結算日永遠判不出來。
    """
    from broker import OpenPrices
    from conftest import CONTRACTS
    from main import run_entry
    from state import BY_SETTLEMENT

    run_entry(
        make_config(auto_order_enabled=True),
        today=SETTLEMENT,
        broker=FakeBroker(script=[OpenPrices(tx=42331, mtx=42298, tmf=42265)],
                          contracts=CONTRACTS),
        notify=RecordingNotifier(),
        sleep=lambda _s: None,
        state_path=state_path(),
    )
    broker = FakeBroker()
    _run(record=None, broker=broker, today=SETTLEMENT)
    assert broker.orders == [], "結算日不送出場委託"
    assert read_position(path=state_path()).close_reason == BY_SETTLEMENT


def test_a_zero_fill_exit_leaves_the_whole_position():
    _run(_record(lots=2), fills=[0])
    record = read_position(path=state_path())
    assert record.exited is False
    assert record.lots == 2
