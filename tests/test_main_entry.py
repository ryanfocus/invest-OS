"""進場流程 —— 從最上層打進去，斷言外部可觀察的效果。

這裡不測內部 helper。可觀察的效果有兩種：**產生的 Discord 訊息**與
**送出的委託**；後者由 test_main_entry_order.py 專責，這裡只看發報。
"""

from datetime import date

from conftest import ON_TIME, RecordingNotifier, make_config, observations_path, state_path
from broker.fake import FakeBroker
from main import run_entry
from strategy import LONG, NO_TRADE, SHORT

D = date(2026, 7, 31)


def _run(tx, mtx, tmf, cfg=None, notifier=None):
    notifier = notifier or RecordingNotifier()
    outcome = run_entry(
        cfg or make_config(),
        today=D,
        broker=FakeBroker(tx=tx, mtx=mtx, tmf=tmf),
        notify=notifier,
        state_path=state_path(),
        observations_path=observations_path(),
        fetch_official=lambda day: None,
        now=ON_TIME,
    )
    return outcome, notifier


# --- 端到端：假 broker 給價 → 算訊號 → 發 Discord ---


def test_entry_sends_one_message_with_the_signal():
    outcome, notifier = _run(42331, 42298, 42265)
    assert outcome.signal == LONG
    assert len(notifier.sent) == 1
    assert "做多" in notifier.sent[0]["content"]


def test_entry_message_contains_the_open_prices_from_the_broker():
    _, notifier = _run(45046, 45184, 45189)
    text = notifier.sent[0]["content"]
    assert "45046" in text and "45184" in text and "45189" in text


def test_no_trade_day_still_sends_a_message():
    """不動作也要發——沉默必須只代表非交易日，否則無法與程式故障區分。"""
    outcome, notifier = _run(45353, 45378, 45288)
    assert outcome.signal == NO_TRADE
    assert len(notifier.sent) == 1


def test_short_signal_flows_through():
    outcome, _ = _run(45046, 45184, 45189)
    assert outcome.signal == SHORT


# --- 開關 ---


def test_discord_disabled_sends_nothing_but_still_computes_signal():
    outcome, notifier = _run(42331, 42298, 42265, cfg=make_config(discord_enabled=False))
    assert outcome.signal == LONG
    assert notifier.sent == []


