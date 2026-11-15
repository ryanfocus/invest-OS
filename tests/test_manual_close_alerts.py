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
from datetime import date, time

import pytest

from broker import BUY, MTX_CODE, SELL, FillUnknown, OpenPrices, OrderFailed
from broker.fake import FakeBroker
from conftest import CONTRACTS, ON_TIME, RecordingNotifier, make_config, observations_path, state_path
from main import run_entry, run_exit
from state import UNCERTAIN, UNCERTAIN_ENTRY, PositionRecord, read_position, write_position

D = date(2026, 8, 10)
NEXT_DAY = date(2026, 8, 11)
LONG_OPENS = OpenPrices(tx=42331, mtx=42298, tmf=42265)   # 做多 → 進場買、出場賣
NO_TRADE_OPENS = OpenPrices(tx=44177, mtx=44142, tmf=44258)
ENTRY_SEQ = "SEQ0000000001"
ONLY_THESE_LOTS = "只平這個口數"


class DiscordDown(RecordingNotifier):
    """Discord 沒送出去：記下要送什麼，但回報失敗。"""

    def __call__(self, payload) -> bool:
        super().__call__(payload)
        return False


def _entry(broker, lots=2, now=ON_TIME, today=D, notifier=None):
    notifier = notifier or RecordingNotifier()
    run_entry(
        make_config(auto_order_enabled=True, order_lots=lots), today=today, broker=broker,
        notify=notifier, sleep=lambda _s: None, state_path=state_path(),
        observations_path=observations_path(), fetch_official=lambda day: None, now=now,
    )
    return notifier


def _exit(broker, record=None, raw_state=None, auto=True, today=D, notifier=None):
    if raw_state is not None:
        io.open(state_path(), "w", encoding="utf-8").write(raw_state)
    elif record is not None:
        write_position(record, path=state_path())
    notifier = notifier or RecordingNotifier()
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
    """早上那則說「13:40 會先查，查到就照常平倉，並回一則確認」——照流程跑一次。"""
    _entry(_entry_broker(query_result=None))
    broker = FakeBroker(query_result=2)
    outcome, notifier = _exit(broker)
    assert [(o.side, o.lots) for o in broker.orders] == [(SELL, 2)]
    assert outcome.exited and outcome.exit_code == 0
    assert len(notifier.sent) == 1 and "✅" in notifier.text and "成交 2 口" in notifier.text


def test_the_1340_lookup_finding_no_fill_says_so():
    """查到「沒有成交」也要回一句：早上那則叫人等，沉默分不出「沒事」與「出場沒跑」。"""
    _entry(_entry_broker(query_result=None))
    outcome, notifier = _exit(FakeBroker(query_result=0))
    assert "✅" in notifier.text and "沒有成交" in notifier.text, notifier.text
    assert outcome.exit_code == 0


def test_an_entry_rerun_after_1340_says_to_run_the_exit_now():
    """重開機補跑的早班落在 13:40 之後：「13:40 會先查」已經不對了。"""
    _entry(_entry_broker())
    text = _entry(FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS), now=time(14, 0)).text
    assert "已經過了 13:40" in text and "run_stage.cmd exit" in text, text
    assert "13:40 出場時會先用委託序號" not in text, text


def test_an_entry_rerun_after_the_hand_over_says_nothing_more():
    """13:40 已經發了「請自行賣出 0～2 口」。重跑早班再叫人「不要自己平」，
    就是叫人別照 13:40 那則做——部位留著過夜。"""
    _entry(_entry_broker())
    _exit(FakeBroker(query_result=None))
    text = _entry(FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS), now=time(14, 0)).text
    assert "收不到成交回報" not in text, text


# --- 13:40：每一則要人動手的，都寫明買賣別與口數 ---


def test_an_unresolved_entry_says_which_side_and_up_to_how_many_lots():
    _, notifier = _exit(FakeBroker(query_result=None), _uncertain_entry(requested_lots=2))
    text = notifier.text
    assert "賣出" in text and "0～2 口" in text and ENTRY_SEQ in text, text
    assert ONLY_THESE_LOTS in text, text
    assert "確認帳戶實際部位" not in text, text


def test_an_unresolved_entry_with_the_switch_off_does_not_claim_a_lookup():
    broker = FakeBroker()
    _, notifier = _exit(broker, _uncertain_entry(), auto=False)
    assert "沒有向券商查" in notifier.text and "沒能向券商查到" not in notifier.text, notifier.text
    assert broker.query_calls == []


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
    拿進場單去查「委託減成交」——進場單全部成交，算出 0 口，部位就這樣留著過夜。
    第一則沒送到，重跑才會再講一次——所以兩次都看。"""
    broker = FakeBroker(order_error=FillUnknown("沒有序號", order_seq=""))
    _, first = _exit(broker, _record(lots=2), notifier=DiscordDown())
    assert read_position(path=state_path()).order_seq == ""

    _, second = _exit(FakeBroker())
    for text in (first.text, second.text):
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
    assert "這則只發一次" in notifier.text, notifier.text


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
    """告警沒送到：剩下的 1 口還歸程式管，log 寫著要平幾口。"""
    caplog.set_level(logging.INFO)
    _exit(FakeBroker(fills=[1]), _record(lots=2), notifier=DiscordDown())
    assert "要平就賣出 1 口" in _last_position_line(caplog)


def test_the_log_says_the_lots_were_handed_over_once_the_alert_arrives(caplog):
    caplog.set_level(logging.INFO)
    _exit(FakeBroker(fills=[1]), _record(lots=2))
    assert "已交給你手動平" in _last_position_line(caplog)


def test_the_log_line_names_the_last_trading_day(caplog):
    """讀不到部位記錄的那天剛好是結算日：照那一行送單，會在下一個月份開新倉。"""
    caplog.set_level(logging.INFO)
    _entry(FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS))
    assert "最後交易日 20260819" in _last_position_line(caplog)
    _, notifier = _exit(FakeBroker(), raw_state="{ 寫到一半")
    assert "最後交易日」是 20260810 或更早" in notifier.text, notifier.text


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
    assert "賣出 2 口 MTX08" in text and "已經處理過了" in text, text
    assert ONLY_THESE_LOTS in text, text


def test_a_leftover_position_is_mentioned_only_once():
    """記錄要等 OS 下次成交才會被覆蓋。不記下「講過了」的話，沒成交的日子每天早上
    都會再叫人平一次——照做兩次就是反向新倉。"""
    write_position(_record(trading_day=20260807, lots=2), path=state_path())
    first = _entry(FakeBroker(script=[NO_TRADE_OPENS], contracts=CONTRACTS))
    second = _entry(FakeBroker(script=[NO_TRADE_OPENS], contracts=CONTRACTS), today=NEXT_DAY)
    assert "沒有收掉" in first.text
    assert "沒有收掉" not in second.text, second.text


def test_a_leftover_position_is_mentioned_again_if_discord_was_down():
    write_position(_record(trading_day=20260807, lots=2), path=state_path())
    _entry(FakeBroker(script=[NO_TRADE_OPENS], contracts=CONTRACTS), notifier=DiscordDown())
    second = _entry(FakeBroker(script=[NO_TRADE_OPENS], contracts=CONTRACTS), today=NEXT_DAY)
    assert "沒有收掉" in second.text


# --- 交給使用者之後：重跑出場不送單、也不再叫人平一次（SPEC 使用者故事 46）---


HAND_OVERS = [
    ("出場登入失敗", FakeBroker(login_error=RuntimeError("斷線")), _record(), True),
    ("委託送不出去", FakeBroker(order_error=OrderFailed("拒絕")), _record(), True),
    ("只成交一部分", FakeBroker(fills=[1]), _record(), True),
    ("不確定持有幾口", FakeBroker(query_result=None), _uncertain_entry(), True),
    ("出場收不到回報", FakeBroker(order_error=FillUnknown("逾時", order_seq="EXIT0001")),
     _record(), True),
    ("開關盤中被關掉", FakeBroker(), _record(), False),
]


@pytest.mark.parametrize("broker, record, auto", [h[1:] for h in HAND_OVERS],
                         ids=[h[0] for h in HAND_OVERS])
def test_a_rerun_after_a_manual_close_alert_sends_nothing_and_says_nothing(broker, record, auto):
    """使用者照告警平過，重跑出場（或把開關打開再跑）就不可以再平一次，也不可以再叫他平。
    `FakeBroker()` 沒安排查詢：重跑若去查，會直接紅。"""
    _, first = _exit(broker, record, auto=auto)
    assert first.sent, "第一輪應該要發告警"

    again = FakeBroker()
    outcome, second = _exit(again)
    assert again.orders == [] and second.sent == [], second.text
    assert outcome.exit_code == 0


def test_a_manual_close_alert_that_did_not_arrive_leaves_the_lots_to_the_rerun():
    """告警沒送到，使用者不知道這件事——重跑還要能自己平掉。"""
    _exit(FakeBroker(login_error=RuntimeError("斷線")), _record(lots=2), notifier=DiscordDown())
    again = FakeBroker()
    _exit(again)
    assert [(o.side, o.lots) for o in again.orders] == [(SELL, 2)]


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
