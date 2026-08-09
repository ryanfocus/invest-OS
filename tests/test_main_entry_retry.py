"""進場流程的重試與失敗路徑。

三種結局必須分得開：
  正常     —— 產生 LONG / SHORT / NO_TRADE
  無法判斷 —— 資料拿不到，**不是不動作**，程式仍正常結束
  致命     —— 登入失敗，以錯誤結束

不斷言重試間隔或呼叫次數（那是實作細節）；只斷言「會再試」與「試完會做什麼」。
"""

from datetime import date

import settings as settings_module
from broker import LoginFailed, OpenPrices, QuoteNotReady
from broker.fake import FakeBroker
from main import run_entry
from strategy import LONG

# 2026/07/31 的實際開盤價（期交所一般時段）——大台高於另兩者，訊號為做多。
# 刻意不用 08/06 那組：那天大台夾在中間，訊號是不動作，會跟「無法判斷」混淆。
D = date(2026, 7, 31)
GOOD = OpenPrices(tx=42331, mtx=42298, tmf=42265)


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def __call__(self, payload) -> bool:
        self.sent.append(payload)
        return True

    @property
    def text(self) -> str:
        return "\n".join(p["content"] for p in self.sent)


def _config(**overrides):
    base = {"discord_enabled": True, "quote_retry_attempts": 3, "quote_retry_interval_seconds": 60}
    base.update(overrides)
    return settings_module.Config(**base)


def _run(broker, cfg=None):
    notifier = RecordingNotifier()
    outcome = run_entry(
        cfg or _config(),
        today=D,
        broker=broker,
        notify=notifier,
        sleep=lambda _seconds: None,   # 測試不真的睡；不對它做任何斷言
    )
    return outcome, notifier


# --- 重試真的會再試 ---


def test_recovers_when_data_becomes_ready_on_a_later_attempt():
    """前兩次未就緒、第三次成功 → 訊號照常產生。

    這是重試存在的唯一理由：08:50 報價偶爾晚幾十秒才齊。
    """
    broker = FakeBroker(script=[
        QuoteNotReady("TX00AM 開盤價為 0（尚未成交）"),
        QuoteNotReady("TX00AM 開盤價為 0（尚未成交）"),
        GOOD,
    ])
    outcome, notifier = _run(broker)
    assert outcome.signal == LONG
    assert outcome.exit_code == 0
    assert "42331" in notifier.text


def test_gives_up_after_configured_attempts_and_reports_no_signal():
    """一直未就緒 → Discord 告知今日無訊號，程式正常結束。"""
    broker = FakeBroker(script=[QuoteNotReady("TX00AM 開盤價為 0（尚未成交）")])
    outcome, notifier = _run(broker)
    assert outcome.signal is None
    assert outcome.exit_code == 0, "拿不到資料是預期內的情況，不是程式錯誤"
    assert len(notifier.sent) == 1


def test_no_signal_message_is_distinguishable_from_no_trade():
    """『無法判斷』不可長得像『不動作』——後者是判斷的結果，前者是沒判斷。"""
    broker = FakeBroker(script=[QuoteNotReady("TM0000AM 沒有報價")])
    _, notifier = _run(broker)
    assert "不動作" not in notifier.text
    assert "TM0000AM" in notifier.text, "要講出是哪個商品出問題，否則無從排查"


def test_retry_count_comes_from_config():
    """把重試次數設成 1 就只試一次——證明設定真的有生效。"""
    broker = FakeBroker(script=[QuoteNotReady("尚未成交"), GOOD])
    outcome, _ = _run(broker, cfg=_config(quote_retry_attempts=1))
    assert outcome.signal is None, "只准試一次，第二次的好資料不該被拿到"


# --- 登入失敗是致命的 ---


def test_login_failure_notifies_and_exits_with_error():
    broker = FakeBroker(script=[GOOD], login_error=LoginFailed("代碼 600：憑證錯誤"))
    outcome, notifier = _run(broker)
    assert outcome.signal is None
    assert outcome.exit_code != 0, "登入失敗要以錯誤結束，不可靜默"
    assert "600" in notifier.text


def test_login_failure_does_not_ask_for_quotes():
    """登入都失敗了就別再去要報價，那只會得到更難懂的錯誤。"""
    broker = FakeBroker(script=[GOOD], login_error=LoginFailed("代碼 600"))
    _run(broker)
    assert broker.open_price_calls == 0


# --- Discord 關閉時仍要正確結束 ---


def test_failures_still_return_correct_exit_code_when_discord_off():
    broker = FakeBroker(script=[GOOD], login_error=LoginFailed("代碼 600"))
    outcome, notifier = _run(broker, cfg=_config(discord_enabled=False))
    assert outcome.exit_code != 0
    assert notifier.sent == []
