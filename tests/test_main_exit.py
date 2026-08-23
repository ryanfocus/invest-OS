"""出場流程 —— 把早上開的部位平掉。

這個檔案守的是「部位不會過夜」。失敗的代價不對稱：

  多平了   → 開出一個方向相反、沒人管的新倉
  少平了   → 剩下的口數進夜盤（15:00–05:00），使用者不知情

所以斷言集中在**平了幾口、什麼方向**，以及**什麼情況下一口都不准平**。

出場刻意**不查商品清單**：下單代碼與最後交易日在 08:50 就知道了，
都寫在狀態檔裡。13:40 再去連一次報價主機只是多一個會失敗的地方，
而那時候失敗的代價是部位過夜。
"""

import io
from dataclasses import replace
from datetime import date

import pytest

from broker import BUY, EXIT, MTX_CODE, SELL
from broker.fake import FakeBroker
from conftest import RecordingNotifier, make_config, observations_path, state_path
from main import run_exit
from state import UNCERTAIN, PositionRecord, read_position, write_position, UNCERTAIN_ENTRY

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


def _run(record=None, cfg=None, broker=None, today=D, fills=None, order_error=None,
         raw_state=None):
    if raw_state is not None:
        # 讀不懂的狀態檔只能用寫壞的檔案做出來——`write_position` 會擋下
        # 所有違反不變量的記錄，正是它擋不住的東西（截斷、亂碼）才是這條路。
        io.open(state_path(), 'w', encoding='utf-8').write(raw_state)
    elif record is not None:
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
    _, _, broker = _run(_record(lots=None, status=UNCERTAIN, uncertain_stage=UNCERTAIN_ENTRY))
    assert broker.orders == []


def test_an_uncertain_record_asks_for_human_help():
    outcome, notifier, _ = _run(_record(lots=None, status=UNCERTAIN, uncertain_stage=UNCERTAIN_ENTRY))
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


def test_a_settled_position_does_not_notify():
    """**結算日不發 Discord。**

    2026-08-21 使用者定案：出場那班**只有失敗才推播**，沒有例外。
    結算日什麼事都沒發生錯——合約到期、部位由交易所平掉，不需要人做任何事。

    初版會發一則「告知」，理由是「當日損益會與回測對不起來，不講的話
    日後查帳會以為程式算錯」。那個理由仍然成立，但改用不打擾人的方式記錄：
    log、狀態檔的 `close_reason=SETTLEMENT`、以及 SPEC 的〈結算日的現金結算價〉。
    """
    _, notifier, _ = _run(_record(trading_day=20260819), today=SETTLEMENT)
    assert notifier.sent == []


def test_a_settled_position_still_finishes_cleanly():
    """不發訊息不代表出事——結束狀態要是正常的，排程才不會誤報。"""
    outcome, _, _ = _run(_record(trading_day=20260819), today=SETTLEMENT)
    assert outcome.exit_code == 0


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


def test_settlement_day_stays_silent_even_when_the_switch_is_off():
    """開關關著＋結算日：舊路徑會發「部位過夜沒人知道」的告警，但它不會過夜。

    走到開關那條的訊息會叫使用者去手動平倉——那是做不到的事（合約已停止交易）。
    現在兩條都不發：沒事發生錯，就不打擾人。
    """
    _, notifier, broker = _run(_record(trading_day=20260819),
                               cfg=make_config(auto_order_enabled=False),
                               today=SETTLEMENT)
    assert broker.orders == []
    assert notifier.sent == []


def test_settlement_day_with_an_uncertain_record_does_alert():
    """**這一條是例外，而且是對的例外。**

    「只有失敗才推播」裡的「失敗」不是指「送單失敗」，是指**有事不對勁**。
    部位會照樣被結算沒錯，但**早上不知道成交幾口這件事，結算不會補上**——
    使用者仍然需要去對那筆帳，否則那天的損益永遠是個謎。

    與上面那條的差別：那條沒有任何事情不對，這條有。
    """
    _, notifier, broker = _run(
        _record(trading_day=20260819, lots=None, status=UNCERTAIN,
                uncertain_stage=UNCERTAIN_ENTRY),
        today=SETTLEMENT,
    )
    assert broker.orders == [], "不確定持有幾口，更不能送單"
    assert len(notifier.sent) == 1
    text = notifier.sent[0]["content"]
    assert "不知道" in text and "對帳" in text
    assert "手動" not in text, "合約已停止交易，叫人手動平倉是做不到的事"


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
        observations_path=observations_path(),
        fetch_official=lambda day: None,
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
        observations_path=observations_path(),
        fetch_official=lambda day: None,
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


def test_an_old_format_state_file_alerts_instead_of_crashing(tmp_path):
    """**升級後第一天最危險的一種檔案。**

    c18bb3e 之前寫下的「不確定」記錄沒有 `uncertain_stage`。修正之前，
    那個檔案會讓 13:40 這一班以 traceback 結束而**一則 Discord 都不發**——
    使用者完全不知道帳上可能還有部位。

    現在它是「狀態檔異常」告警，人看得到、也知道要去查什麼。
    """
    import json
    from broker import BUY, MTX_CODE
    path = state_path()
    old = {
        "trading_day": 20260810, "product": MTX_CODE, "order_code": "MTX08",
        "contract_month": "202608", "side": BUY, "lots": None, "requested_lots": 2,
        "last_trading_day": 20260819, "status": "UNCERTAIN", "order_seq": "SEQ001",
        "exited": False, "close_reason": "",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(old, fh)

    broker = FakeBroker()
    notifier = RecordingNotifier()
    outcome = run_exit(
        make_config(auto_order_enabled=True), today=D, broker=broker,
        notify=notifier, sleep=lambda _s: None, state_path=path,
    )
    assert broker.orders == [], "讀不懂就不准送單"
    assert notifier.sent != [], "而且一定要講出來"
    assert "狀態檔" in notifier.text
    assert outcome.exit_code == 1


# --- 「收不到回報」那則訊息裡的口數 ---


def test_the_unknown_exit_message_reports_the_lots_actually_sent():
    """出場單送的是**早上實際成交**的口數，訊息就要印那個數字。

    早上委託 3 口只成交 2 口 → 下午送 2 口。這則訊息的用途是叫人
    「先確認帳戶實際部位再決定要不要補單」，印成 3 的話，人會拿著
    多出來的 1 口去補一筆錯的單——那是開新倉，不是平倉。
    """
    from broker import FillUnknown
    _, notifier, broker = _run(
        _record(lots=2, requested_lots=3),
        broker=FakeBroker(order_error=FillUnknown("逾時", order_seq="S")),
    )
    text = notifier.sent[0]["content"]
    # 與 test_discord.py 那條的分工：那一條問「訊息本身印對了嗎」，
    # 這一條問「**送到訊息裡的是不是同一筆委託**」——把 replace 之後
    # （口數已被抹掉）的記錄交給訊息，只有從流程打進來才看得出來。
    sent_lots = broker.orders[0].lots
    assert sent_lots == 2, "出場單送的是早上實際成交的 2 口"
    assert f"{sent_lots} 口" in text, f"訊息裡的口數與實際送出的不符：{text}"
    assert "3 口" not in text, f"3 口是早上的委託量，不是下午送出去的：{text}"


def test_the_unknown_exit_message_never_prints_a_placeholder_for_missing_lots():
    """重跑時狀態檔裡沒有口數——那就明講，不可以印出 `None`。

    記成「不確定」的那一刻，`PositionRecord` 的規定就把口數抹掉了
    （`UNCERTAIN ⇒ lots is None`）。所以這則訊息第一次發有數字、
    重跑就沒有。印 `None` 會讓一則「請去對帳」的訊息看起來像故障，
    而使用者手上唯一的數字其實在第一次那則通知裡。
    """
    from state import UNCERTAIN_EXIT
    _, notifier, broker = _run(
        _record(lots=None, status=UNCERTAIN, uncertain_stage=UNCERTAIN_EXIT))
    text = notifier.sent[0]["content"]
    assert "None" not in text, f"訊息裡印出了 None：\n{text}"
    assert "第一次" in text, "沒有告訴使用者去哪裡找那個數字"
    assert broker.orders == [], "不確定的出場單絕不可以再送一次"


# ─────────────────────────────────────────────────────────
# 出場那班的結局規則：**有東西要講 ⟺ 非零結束碼**
# ─────────────────────────────────────────────────────────
#
# 15 個出口全部滿足這條規則，但 2026-08-23 架構檢視之前，
# 它在程式裡**沒有任何一處代表它**——`ExitOutcome` 被直接建了 12 次。
# commit b6187c0／fd4bd04 兩次改動落在這裡不是巧合。
#
# ⚠️ 精確一點：不變量是「**有東西要講**」而不是「講成功了」。
#    Discord 關閉時 `notified` 是 False，但 `exit_code` 仍然是 1——
#    那一天仍然有事情不對勁，只是使用者沒被通知到。


def _exit_scenarios():
    """涵蓋 run_exit 每一種結局的最小安排。"""
    from broker import FillUnknown, OrderFailed
    from state import UNCERTAIN_EXIT
    settle = dict(trading_day=20260819, last_trading_day=20260819)
    return [
        ("非交易日",        dict(record=_record(), today=date(2026, 8, 16))),
        ("今日無記錄",      dict(record=None)),
        ("已出場",          dict(record=_record(exited=True, close_reason="EXIT"))),
        ("結算日正常",      dict(record=_record(**settle), today=SETTLEMENT)),
        ("結算日不確定",    dict(record=_record(lots=None, status=UNCERTAIN,
                                              uncertain_stage=UNCERTAIN_ENTRY, **settle),
                                today=SETTLEMENT)),
        ("出場不確定",      dict(record=_record(lots=None, status=UNCERTAIN,
                                              uncertain_stage=UNCERTAIN_EXIT))),
        ("不確定持有幾口",  dict(record=_record(lots=None, status=UNCERTAIN,
                                              uncertain_stage=UNCERTAIN_ENTRY),
                                broker=FakeBroker(query_result=None))),
        ("開關關著有部位",  dict(record=_record(),
                                cfg=make_config(auto_order_enabled=False))),
        ("出場失敗",        dict(record=_record(),
                                broker=FakeBroker(order_error=OrderFailed("代碼 1038")))),
        ("出場收不到回報",  dict(record=_record(),
                                broker=FakeBroker(order_error=FillUnknown("逾時", order_seq="S")))),
        ("部分成交",        dict(record=_record(lots=2), fills=[1])),
        ("出場成功",        dict(record=_record())),
        # 以下三個是 2026-08-23 用執行軌跡量出來補的——在那之前這張表
        # 自稱涵蓋全部結局，實際上漏了它們。
        ("狀態檔讀不懂",    dict(raw_state='{"trading_day": 202608')),
        ("查出早上沒成交",  dict(record=_record(lots=None, status=UNCERTAIN,
                                              uncertain_stage=UNCERTAIN_ENTRY),
                                broker=FakeBroker(query_result=0))),
        ("出場登入失敗",    dict(record=_record(),
                                broker=FakeBroker(login_error=RuntimeError("斷線")))),
    ]


def _scenario(label: str) -> dict:
    """依名字取一份**全新**的安排——每次呼叫都重建裡面的假 broker。"""
    return dict(_exit_scenarios())[label]


@pytest.mark.parametrize("label", [s[0] for s in _exit_scenarios()])
def test_saying_something_and_a_non_zero_exit_code_go_together(label):
    """**每一種結局都要滿足：發了訊息 ⟺ exit_code 非零。**

    下面那張表涵蓋 `run_exit` 的**每一個出口**——2026-08-23 用執行軌跡
    實際量過，15 個全中。

    ⚠️ **沒有任何機制強迫它保持完整。** 加一種新結局時要自己來補一列，
    漏了不會變紅，只會靜靜地少測一個出口。這段原本寫「忘了同步這裡就會紅」，
    那是假的——而且當時那張表自稱涵蓋全部，實際上漏了三個
    （狀態檔讀不懂、查出早上沒成交、出場登入失敗）。
    """
    outcome, notifier, _broker = _run(**_scenario(label))
    said_something = notifier.sent != []
    assert said_something == (outcome.exit_code != 0), (
        f"「{label}」違反規則：發了 {len(notifier.sent)} 則訊息，"
        f"但 exit_code={outcome.exit_code}"
    )


@pytest.mark.parametrize("label", [s[0] for s in _exit_scenarios()])
def test_the_rule_holds_even_with_discord_switched_off(label):
    """**Discord 關著時，`notified` 是 False 但 `exit_code` 不變。**

    不變量講的是「有沒有東西要講」，不是「講成功了沒」。
    寫成「notified ⟺ exit_code」的話，關掉 Discord 會讓所有異常的一天
    看起來都正常——那正是最危險的一種靜默。
    """
    def run(discord: bool):
        # ⚠️ 每次都重新取一份情境。共用同一個 FakeBroker 實例的話，
        #    第二次跑會拿到被第一次動過的假 broker——現在的情境剛好都
        #    無狀態所以看不出來，但那是巧合，不是保證。
        kwargs = _scenario(label)
        cfg = kwargs.get("cfg") or make_config(auto_order_enabled=True)
        return _run(**{**kwargs, "cfg": replace(cfg, discord_enabled=discord)})

    outcome, notifier, _broker = run(discord=False)
    assert notifier.sent == [], "Discord 關著就不該真的送出去"
    baseline, _, _ = run(discord=True)
    assert outcome.exit_code == baseline.exit_code, (
        f"「{label}」的結束狀態不該受 Discord 開關影響"
    )
