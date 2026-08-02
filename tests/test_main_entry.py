"""進場流程 —— 從最上層打進去，斷言外部可觀察的效果。

這裡不測內部 helper。可觀察的效果目前有一種：**產生的 Discord 訊息**。
（送出的委託、寫入的狀態檔會在後續 ticket 加入。）
"""

from datetime import date

import settings as settings_module
from broker.fake import FakeBroker
from main import run_entry
from strategy import LONG, NO_TRADE, SHORT

D = date(2026, 7, 31)


class RecordingNotifier:
    """記錄被要求送出什麼，取代真正的 Discord。"""

    def __init__(self):
        self.sent = []

    def __call__(self, payload) -> bool:
        self.sent.append(payload)
        return True


def _config(**overrides):
    base = {"discord_enabled": True}
    base.update(overrides)
    return settings_module.Config(**base)


def _run(tx, mtx, tmf, cfg=None, notifier=None):
    notifier = notifier or RecordingNotifier()
    outcome = run_entry(
        cfg or _config(),
        today=D,
        broker=FakeBroker(tx=tx, mtx=mtx, tmf=tmf),
        notify=notifier,
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
    outcome, notifier = _run(42331, 42298, 42265, cfg=_config(discord_enabled=False))
    assert outcome.signal == LONG
    assert notifier.sent == []


# --- broker 只被要求取價一次，不做多餘呼叫 ---


def test_entry_reads_open_prices_exactly_once():
    broker = FakeBroker(tx=42331, mtx=42298, tmf=42265)
    run_entry(_config(), today=D, broker=broker, notify=RecordingNotifier())
    assert broker.open_price_calls == 1
