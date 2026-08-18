"""進場那班留下的觀測記錄，以及掛在它後面的隔日對帳。

**這個檔案守的是「每個交易日都留得下一筆」。**

聽起來理所當然，但 2026-08-17 盤點時發現的洞正是這個：三個開盤價只活在
記憶體、Discord 的中文訊息與日誌裡，沒有任何程式讀得回來的形式；
而唯一寫成檔案的部位記錄**只在真的下單那天才寫**——訊號不動作不寫
（23.2% 的交易日），開關關著也不寫（目前的每一天）。
於是 SPEC 承諾的「第一階段只跑訊號、零金錢風險驗證資料正確性」
在機制上並不存在。

所以下面最重要的幾條，測的都是「**沒有下單的那些日子**」。

對帳的邏輯本身在 `test_reconcile.py`，這裡只驗**接線**：
它跑在下單之後、而且它出事不會改變當日的結果。
"""

import os

import pytest

from broker import MTX_CODE, OpenPrices, OrderFailed, TMF_CODE, TX_CODE
from broker.fake import FakeBroker
from conftest import CONTRACTS, RecordingNotifier, make_config, observations_path, state_path
from main import run_entry
from observations import Observation, read_observation_before
from strategy import LONG, NO_TRADE

from datetime import date

D = date(2026, 8, 18)          # 一般交易日（週二）
WEEKEND = date(2026, 8, 16)    # 週日

# 大台夾在中間 → 不動作。開盤價本身完全正常，仍然要記下來。
NO_TRADE_OPENS = OpenPrices(tx=42300, mtx=42331, tmf=42265)
LONG_OPENS = OpenPrices(tx=42331, mtx=42298, tmf=42265)


def _run(*, opens=LONG_OPENS, cfg=None, broker=None, today=D, fetch_official=None):
    broker = broker or FakeBroker(script=[opens], contracts=CONTRACTS)
    notifier = RecordingNotifier()
    outcome = run_entry(
        cfg or make_config(),
        today=today,
        broker=broker,
        notify=notifier,
        sleep=lambda _s: None,
        state_path=state_path(),
        observations_path=observations_path(),
        fetch_official=fetch_official or (lambda day: None),
    )
    return outcome, notifier


def _written():
    return read_observation_before(20260819, path=observations_path())


# --- 沒有下單的日子也要留下記錄（本張的核心）---


def test_a_no_trade_day_still_records_an_observation():
    """**不動作佔 23.2% 的交易日。** 不記的話，四分之一的樣本永遠不會被對帳。

    而且「不動作」本身就可能是錯誤造成的——開盤價取到錯誤盤別時，
    大台夾在中間的機率並不低。那些日子恰恰最需要被檢查。
    """
    _run(opens=NO_TRADE_OPENS)
    record = _written()
    assert record is not None
    assert record.signal == NO_TRADE


def test_the_switch_being_off_still_records_an_observation():
    """**這是目前的每一天。** 開關關著時完全不寫部位記錄，

    所以在里程碑 2 之前，觀測記錄是唯一會產生的東西——
    也是唯一能驗證「第一階段只跑訊號」那段期間資料是否正確的依據。
    """
    _run(cfg=make_config(auto_order_enabled=False))
    assert _written() is not None


def test_an_order_failure_still_records_the_observation():
    """訊號算出來了、也發出去了，只是單沒送成功。

    那三個開盤價是**既成事實**，跟後面下單成不成功無關。
    因為下單失敗就不記，等於在最需要排查的那天把證據丟掉。
    """
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS,
                        order_error=OrderFailed("代碼 1038"))
    outcome, _ = _run(cfg=make_config(auto_order_enabled=True), broker=broker)
    assert outcome.exit_code != 0
    assert _written() is not None


# --- 不該記的時候不記 ---


def test_a_non_trading_day_records_nothing():
    """非交易日連登入都不做，當然也沒有觀測。"""
    _run(today=WEEKEND)
    assert _written() is None


def test_a_day_with_no_usable_quote_records_nothing():
    """**「無法判斷」不是「不動作」。**

    報價沒就緒時根本沒有數字可記。硬記一筆 0 進去的話，
    隔天對帳會拿 0 去比、報一個假的不一致，而真正的問題被蓋掉。
    """
    from broker import QuoteNotReady
    broker = FakeBroker(script=[QuoteNotReady("尚未開盤")] * 3, contracts=CONTRACTS)
    outcome, _ = _run(broker=broker)
    assert outcome.signal is None
    assert _written() is None


def test_running_twice_in_a_day_records_one_line():
    """重複執行保護看的是**部位記錄**，而開關關著時不寫部位記錄——

    那條防線在這裡完全攔不住，所以觀測這一側要自己擋。
    """
    _run()
    _run()
    lines = [x for x in open(observations_path(), encoding="utf-8") if x.strip()]
    assert len(lines) == 1


def test_a_second_run_with_different_prices_raises_an_alert():
    """同一天跑兩次很正常，**數字不一樣才是訊號**——

    代表報價來源在同一天給了兩個答案，而這整個系統就建立在那三個數字上。
    留下的只有第一筆，所以隔日對帳看不到這件事；不在當下講，就沒有人會知道。
    """
    _run(opens=LONG_OPENS)
    _, notifier = _run(opens=OpenPrices(tx=42331, mtx=42298, tmf=99999))
    assert "兩組不同的開盤價" in notifier.text


def test_a_second_run_with_the_same_prices_stays_quiet():
    """對照組：數字一樣就只是重跑，不該打擾人。"""
    _run(opens=LONG_OPENS)
    _, notifier = _run(opens=LONG_OPENS)
    assert "兩組不同" not in notifier.text


def test_the_first_record_wins_a_conflict():
    """保留先寫入的那一筆——它比較接近 08:45。"""
    _run(opens=LONG_OPENS)
    _run(opens=OpenPrices(tx=42331, mtx=42298, tmf=99999))
    assert _written().tmf == 42265


# --- 記下來的內容 ---


def test_the_observation_carries_all_three_open_prices():
    """對帳要比的就是這三個數字。少一個就少驗一個商品。"""
    _run(opens=LONG_OPENS)
    record = _written()
    assert (record.tx, record.mtx, record.tmf) == (42331, 42298, 42265)


def test_the_observation_carries_the_signal():
    _run(opens=LONG_OPENS)
    assert _written().signal == LONG


def test_the_observation_carries_the_contract_month():
    """這一欄讓「結算日當天選到本月還是次月」**每個月自動回答一次**，

    不必再為那個問題單獨排一次驗證班（ticket 03 原本要那樣做）。
    """
    _run()
    assert _written().contract_month == CONTRACTS[TX_CODE].contract_month


def test_the_observation_day_is_the_trading_day_not_the_wall_clock():
    _run(today=D)
    assert _written().trading_day == 20260818


# --- 觀測寫不進去，不可以拖垮當天 ---


def test_a_failed_observation_write_does_not_stop_the_signal(tmp_path):
    """記錄寫不進去的代價是「少對一天的帳」，訊號發不出去的代價是「整天沒訊號」。

    後者嚴重得多，所以觀測那一步**不可以**擋在發報前面。
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("我是檔案不是目錄", encoding="utf-8")
    notifier = RecordingNotifier()
    outcome = run_entry(
        make_config(),
        today=D,
        broker=FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS),
        notify=notifier,
        sleep=lambda _s: None,
        state_path=state_path(),
        observations_path=str(blocked / "observations.jsonl"),
        fetch_official=lambda day: None,
    )
    assert outcome.signal == LONG
    assert notifier.sent != []
    assert outcome.exit_code == 0


def test_a_failed_observation_write_does_not_stop_the_order(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("我是檔案不是目錄", encoding="utf-8")
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS)
    run_entry(
        make_config(auto_order_enabled=True),
        today=D,
        broker=broker,
        notify=RecordingNotifier(),
        sleep=lambda _s: None,
        state_path=state_path(),
        observations_path=str(blocked / "observations.jsonl"),
        fetch_official=lambda day: None,
    )
    assert len(broker.orders) == 1


# --- 對帳的接線 ---


def test_reconciliation_runs_after_the_order_not_before():
    """驗收條件：對帳在訊號發報與下單**全部完成之後**才執行。

    寫法：讓對帳的取數函式在被呼叫時記下當時已經送出幾張單。
    若它跑在下單之前，看到的會是 0。

    ⚠️ 要先有**前一交易日**的觀測，對帳才會走到取數那一步——
    否則它在「沒東西可對」就返回了，這條測試會假通過。
    """
    from observations import append_observation
    append_observation(
        Observation(trading_day=20260817, tx=45850.0, mtx=45812.0, tmf=45863.0,
                    signal=LONG, contract_month="202608"),
        path=observations_path(),
    )
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS)
    seen = []
    _run(cfg=make_config(auto_order_enabled=True), broker=broker,
         fetch_official=lambda day: seen.append(len(broker.orders)) or None)
    assert seen == [1], "對帳跑的時候，單應該已經送出去了"


def test_reconciliation_compares_yesterday_and_alerts(tmp_path):
    """端到端：昨天寫下的觀測，今天被拿去跟官方資料比對。

    這條走的是真正的資料流（進場寫 → 對帳讀），不是手工組的記錄——
    只測 `run_reconciliation` 證明不了 `run_entry` 真的有把數字寫進去。
    """
    from datetime import date as _date
    _run(today=_date(2026, 8, 17), opens=OpenPrices(tx=45850, mtx=45812, tmf=45863))
    _, notifier = _run(today=D,
                       fetch_official=lambda day: {"tx": 45848.0, "mtx": 45812.0,
                                                   "tmf": 45863.0})
    text = notifier.text
    assert "對帳" in text and "大台" in text and "45848" in text


def test_a_reconciliation_failure_does_not_change_the_exit_code():
    """驗收條件：對帳的任何失敗都不改變程式的結束狀態。"""
    def boom(day):
        raise RuntimeError("期交所連線被拒")
    outcome, _ = _run(fetch_official=boom)
    assert outcome.exit_code == 0


def test_a_reconciliation_failure_does_not_hide_a_real_failure():
    """反過來也要顧：對帳順利完成，不可以把當天真正的失敗洗成成功。"""
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS,
                        order_error=OrderFailed("代碼 1038"))
    outcome, _ = _run(cfg=make_config(auto_order_enabled=True), broker=broker,
                      fetch_official=lambda day: None)
    assert outcome.exit_code == 1


def test_a_non_trading_day_does_not_reconcile():
    """非交易日的約定是「不登入、不發任何訊息」。

    對帳會連外抓期交所資料，違反那個約定；而且休市日也沒有新的官方資料。

    ⚠️ **必須先放一筆可對的觀測。** 沒有的話，就算對帳真的被呼叫了，
    它也會在「沒東西可對」就返回而從不碰取數函式——這條測試會假通過。
    突變測試確認過：拿掉非交易日的判斷，沒有這一筆時 24 條測試全綠。
    """
    from observations import append_observation
    append_observation(
        Observation(trading_day=20260814, tx=45850.0, mtx=45812.0, tmf=45863.0,
                    signal=LONG, contract_month="202608"),
        path=observations_path(),
    )
    called = []
    _run(today=WEEKEND, fetch_official=lambda day: called.append(day) or None)
    assert called == []
