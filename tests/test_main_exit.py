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
    text = notifier.text
    assert "MTX08" in text, "要講出商品"
    assert "2" in text, "要講出口數"
    assert "賣" in text or SELL in text, "要講出該送哪個方向"


def test_a_failed_exit_is_not_recorded_as_exited():
    """標成已出場的話，隔日對帳會以為一切正常，而部位還在。"""
    from broker import OrderFailed
    broker = FakeBroker(order_error=OrderFailed("代碼 1038"))
    _run(_record(), broker=broker)
    assert read_position(path=state_path()).exited is False


def test_settlement_day_does_not_retry():
    """結算日 13:30 後合約已停止交易，重試必然失敗且拖延告警。

    未平倉部位會被交易所現金結算，所以重試沒有意義——
    重點是**趕快告訴使用者**，不是多試兩次。
    """
    from broker import OrderFailed
    broker = FakeBroker(order_error=OrderFailed("合約已停止交易"))
    _run(_record(trading_day=20260819), broker=broker, today=SETTLEMENT)
    assert len(broker.orders) == 1, "結算日只送一次"


def test_an_ordinary_day_does_retry():
    """對照組：非結算日會用完設定的重試次數。"""
    from broker import OrderFailed
    broker = FakeBroker(order_error=OrderFailed("暫時性失敗"))
    _run(_record(), broker=broker)
    assert len(broker.orders) > 1


# --- 部分成交：從 ticket 05 接手 ---


def test_a_partial_exit_is_not_success():
    """要平 2 口只平掉 1 口 → 還有 1 口會過夜。不可以標記為已出場。"""
    outcome, _, _ = _run(_record(lots=2), fills=[1])
    assert read_position(path=state_path()).exited is False
    assert outcome.exit_code != 0


def test_a_partial_exit_says_how_many_lots_are_left():
    """訊息要能讓使用者直接手動處理，所以要講**剩幾口**，不是只說失敗。"""
    _, notifier, _ = _run(_record(lots=2), fills=[1])
    assert "1" in notifier.text, "殘留 1 口要講出來"
    assert "MTX08" in notifier.text


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


def test_settlement_day_entry_then_exit_does_not_retry():
    """端到端：結算日進場 → 結算日出場失敗 → **只送一次**。

    這條走的是真正的資料流（進場寫、出場讀），不是手工組的記錄。
    """
    from broker import OpenPrices, OrderFailed
    from conftest import CONTRACTS
    from main import run_entry

    run_entry(
        make_config(auto_order_enabled=True),
        today=SETTLEMENT,
        broker=FakeBroker(script=[OpenPrices(tx=42331, mtx=42298, tmf=42265)],
                          contracts=CONTRACTS),
        notify=RecordingNotifier(),
        sleep=lambda _s: None,
        state_path=state_path(),
    )
    broker = FakeBroker(order_error=OrderFailed("合約已停止交易"))
    _run(record=None, broker=broker, today=SETTLEMENT)
    assert len(broker.orders) == 1, "結算日不重試"


def test_a_zero_fill_exit_leaves_the_whole_position():
    from broker import OrderFailed  # noqa: F401
    _run(_record(lots=2), fills=[0])
    record = read_position(path=state_path())
    assert record.exited is False
    assert record.lots == 2
