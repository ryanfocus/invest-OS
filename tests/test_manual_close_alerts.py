"""要人手動處理的告警 —— 每一則都要寫明「買進／賣出 幾口」。

2026-11-15 起 PTS 和 OS 在同一個帳戶交易同一個微台合約，使用者自己也長期持有微台。
券商 APP 上看到的是三者加起來的淨部位，**看不出哪幾口是 OS 的**。所以「請確認帳戶
實際部位並自行平倉」這種話沒辦法照做：照 APP 全平會把使用者自己的部位與 PTS 的口數
一起平掉。使用者的原話：「只要明確的寫要平幾口，手動處理就不會報錯」。

另一個要守的是**時間順序**：早上「收不到成交回報」那則原本寫「下午不會自動平倉——
需要你自己處理」，但 ticket 09 之後 13:40 會先用委託序號查，查到就照常平倉。
照早上那則先手動平的話，13:40 會再平一次，開出一個反向新倉。
"""

import io
import logging
from datetime import date

import pytest

from broker import BUY, MTX_CODE, SELL, FillUnknown, OpenPrices
from broker.fake import FakeBroker
from conftest import CONTRACTS, ON_TIME, RecordingNotifier, make_config, observations_path, state_path
from main import run_entry, run_exit
from state import UNCERTAIN, UNCERTAIN_ENTRY, PositionRecord, read_position, write_position

D = date(2026, 8, 10)
NEXT_DAY = date(2026, 8, 11)
LONG_OPENS = OpenPrices(tx=42331, mtx=42298, tmf=42265)   # 做多 → 進場買、出場賣
ENTRY_SEQ = "SEQ0000000001"
ONLY_THESE_LOTS = "只平這個口數"


def _entry(broker, lots=2):
    notifier = RecordingNotifier()
    run_entry(
        make_config(auto_order_enabled=True, order_lots=lots), today=D, broker=broker,
        notify=notifier, sleep=lambda _s: None, state_path=state_path(),
        observations_path=observations_path(), fetch_official=lambda day: None, now=ON_TIME,
    )
    return notifier


def _exit(broker, record=None, raw_state=None, auto=True, today=D):
    if raw_state is not None:
        io.open(state_path(), "w", encoding="utf-8").write(raw_state)
    elif record is not None:
        write_position(record, path=state_path())
    notifier = RecordingNotifier()
    outcome = run_exit(
        make_config(auto_order_enabled=auto), today=today, broker=broker, notify=notifier,
        sleep=lambda _s: None, state_path=state_path(),
    )
    return outcome, notifier


def _record(**overrides) -> PositionRecord:
    """早上做多 2 口、已確認。"""
    base = dict(
        trading_day=20260810, product=MTX_CODE, order_code="MTX08", contract_month="202608",
        last_trading_day=20260819, side=BUY, lots=2, requested_lots=2, order_seq=ENTRY_SEQ,
    )
    base.update(overrides)
    return PositionRecord(**base)


def _uncertain_entry(**overrides) -> PositionRecord:
    return _record(**{"lots": None, "status": UNCERTAIN,
                      "uncertain_stage": UNCERTAIN_ENTRY, **overrides})


def _entry_broker(order_seq=ENTRY_SEQ, query_result=None):
    return FakeBroker(
        script=[LONG_OPENS], contracts=CONTRACTS,
        order_error=FillUnknown("10 秒內委託仍未結束", order_seq=order_seq),
        query_result=query_result,
    )


# --- 早上：不要現在平，13:40 會先查 ---


def test_the_morning_alert_says_not_to_close_because_1340_looks_it_up_first():
    text = _entry(_entry_broker()).text
    assert "不要自己平" in text, text
    assert ENTRY_SEQ in text and "13:40" in text, text
    assert "需要你自己處理" not in text and "不會自動平倉" not in text, (
        f"13:40 會查、查到就平；叫人先自己平，13:40 會再平一次：{text}")


def test_the_morning_alert_without_an_order_number_still_says_to_wait_for_1340():
    """沒有序號時 13:40 查不到，但它會發一則寫明怎麼算口數的告警——等那則，
    兩則都叫人平的話，照做兩次就是反向新倉。"""
    text = _entry(_entry_broker(order_seq="")).text
    assert "不要自己平" in text, text
    assert "沒有拿到委託序號" in text, text
    assert "請人工確認帳戶實際部位" not in text, text


def test_the_morning_alert_on_settlement_day_says_the_exchange_closes_it():
    """結算日 13:40 不查也不送單，部位由交易所結算——不可以講「13:40 會先查」。"""
    from broker import ContractInfo
    contracts = {code: ContractInfo(code=c.code, last_trading_day=20260810,
                                    order_code=c.order_code)
                 for code, c in CONTRACTS.items()}
    broker = FakeBroker(script=[LONG_OPENS], contracts=contracts,
                        order_error=FillUnknown("逾時", order_seq=ENTRY_SEQ), query_result=None)
    text = _entry(broker).text
    assert "結算日" in text and "不必送任何委託" in text, text
    assert "13:40 出場時會先用委託序號" not in text, text


def test_what_the_morning_alert_promises_happens_at_1340():
    """早上那則說「13:40 會先查，查到就照常平倉（不發訊息）」——照流程跑一次。"""
    _entry(_entry_broker(query_result=None))
    broker = FakeBroker(query_result=2)
    outcome, notifier = _exit(broker)
    assert [(o.side, o.lots) for o in broker.orders] == [(SELL, 2)]
    assert notifier.sent == [] and outcome.exited


# --- 13:40：每一則要人動手的，都寫明買賣別與口數 ---


def test_an_unresolved_entry_says_which_side_and_up_to_how_many_lots():
    _, notifier = _exit(FakeBroker(query_result=None), _uncertain_entry(requested_lots=2))
    text = notifier.text
    assert "賣出" in text and "0～2 口" in text and ENTRY_SEQ in text, text
    assert ONLY_THESE_LOTS in text, text
    assert "確認帳戶實際部位" not in text, text


def test_an_unresolved_entry_without_an_order_number_says_how_to_find_it():
    _, notifier = _exit(FakeBroker(query_result=None), _uncertain_entry(order_seq=""))
    text = notifier.text
    assert "沒有拿到委託序號" in text and "08:50" in text, text
    assert "賣出" in text and "0～2 口" in text, text


def test_switching_off_after_entry_names_the_lots_still_held():
    """開關盤中被關掉：部位**確定**還在。原本這裡借用「不確定」那則，
    講的是「沒有收到成交回報」——那不是事實，口數其實知道。"""
    broker = FakeBroker()
    _, notifier = _exit(broker, _record(lots=2), auto=False)
    text = notifier.text
    assert broker.orders == []
    assert "賣出 2 口 MTX08" in text, text
    assert "成交回報" not in text, f"口數是確定的，不是收不到回報：{text}"
    assert ONLY_THESE_LOTS in text, text


def test_an_unknown_exit_says_how_many_lots_may_be_left_and_which_order_to_look_up():
    broker = FakeBroker(order_error=FillUnknown("逾時", order_seq="EXIT0001", known_filled=1))
    _, notifier = _exit(broker, _record(lots=2))
    text = notifier.text
    assert "EXIT0001" in text and "0～1 口" in text and "賣出" in text, text
    assert ENTRY_SEQ not in text, f"進場單的序號拿去查，會查到早上成交的 2 口：{text}"
    assert ONLY_THESE_LOTS in text, text


def test_an_exit_without_an_order_number_does_not_borrow_the_entrys():
    """出場單沒拿到序號時，狀態檔原本會留著**進場單**的序號。重跑時那則告警就叫人
    拿進場單去查「委託減成交」——進場單全部成交，算出 0 口，部位就這樣留著過夜。"""
    broker = FakeBroker(order_error=FillUnknown("沒有序號", order_seq=""))
    _exit(broker, _record(lots=2))
    assert read_position(path=state_path()).order_seq == ""

    _, notifier = _exit(FakeBroker())
    text = notifier.text
    assert ENTRY_SEQ not in text and "沒有拿到委託序號" in text, text


def test_a_broken_state_file_points_to_the_log_lines_that_hold_the_lots():
    _, notifier = _exit(FakeBroker(), raw_state="{ 寫到一半")
    text = notifier.text
    assert "logs\\exit-20260810.log" in text and "logs\\entry-20260810.log" in text, text
    assert "OS 部位：" in text, text
    assert ONLY_THESE_LOTS in text, text


@pytest.mark.parametrize("broker, record", [
    (FakeBroker(order_error=FillUnknown("逾時", order_seq="EXIT0001")), _record()),
    (FakeBroker(fills=[1]), _record()),
    (FakeBroker(login_error=RuntimeError("斷線")), _record()),
], ids=["收不到回報", "只成交一部分", "出場失敗"])
def test_every_exit_alert_asking_for_a_manual_close_says_to_close_only_those_lots(broker, record):
    _, notifier = _exit(broker, record)
    assert ONLY_THESE_LOTS in notifier.text, notifier.text


# --- 讀不到部位記錄時，告警指向的那一行 log 真的寫著要平幾口 ---


def _last_position_line(caplog) -> str:
    lines = [r.getMessage() for r in caplog.records if "OS 部位：" in r.getMessage()]
    assert lines, "log 裡沒有任何「OS 部位：」"
    return lines[-1][lines[-1].index("OS 部位："):]


def test_the_log_says_what_to_close_after_a_normal_entry(caplog):
    caplog.set_level(logging.INFO)
    _entry(FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS))
    assert "要平就賣出 2 口" in _last_position_line(caplog)


def test_the_log_says_nothing_is_left_after_a_normal_exit(caplog):
    caplog.set_level(logging.INFO)
    _entry(FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS))
    _exit(FakeBroker())
    line = _last_position_line(caplog)
    assert "已了結" in line and "要平" not in line, line


def test_the_log_keeps_the_remainder_after_a_partial_exit(caplog):
    caplog.set_level(logging.INFO)
    _exit(FakeBroker(fills=[1]), _record(lots=2))
    assert "要平就賣出 1 口" in _last_position_line(caplog)


def test_the_log_names_the_order_to_look_up_when_the_entry_is_uncertain(caplog):
    caplog.set_level(logging.INFO)
    _entry(_entry_broker())
    line = _last_position_line(caplog)
    assert "不確定" in line and ENTRY_SEQ in line and "賣出" in line, line


def test_the_log_says_nothing_is_held_when_the_lookup_finds_no_fill(caplog):
    caplog.set_level(logging.INFO)
    _entry(_entry_broker(query_result=0))
    line = _last_position_line(caplog)
    assert "沒有" in line and "要平" not in line, line


# --- 隔天早上：前一天的部位沒有收掉 ---


def test_a_leftover_position_says_what_to_send_if_nothing_was_done_yet():
    """前一天 13:40 若發過告警，使用者可能已經照著平了——再叫他平一次就是反向新倉。
    所以只有「那天沒收到任何告警」時才叫他送單，而且寫明口數。"""
    write_position(_record(trading_day=20260807, lots=2), path=state_path())
    text = _entry(FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS)).text
    assert "沒有收掉" in text
    assert "賣出 2 口 MTX08" in text and "已經照著平了" in text, text
    assert ONLY_THESE_LOTS in text, text


def test_a_leftover_uncertain_entry_names_the_order_to_look_up():
    write_position(_uncertain_entry(trading_day=20260807), path=state_path())
    text = _entry(FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS)).text
    assert ENTRY_SEQ in text and "賣出" in text and "0～2 口" in text, text


def test_a_leftover_position_on_a_settled_contract_needs_no_order():
    """合約已經結算，交易所已經平掉了。叫人送單只會在下一個月份開出新倉。"""
    write_position(_record(trading_day=20260807, last_trading_day=20260807, lots=2),
                   path=state_path())
    text = _entry(FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS)).text
    assert "沒有收掉" in text
    assert "不用送任何委託" in text and "請自行" not in text, text
