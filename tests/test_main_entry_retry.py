"""進場流程的重試與失敗路徑。

三種結局必須分得開：
  正常     —— 產生 LONG / SHORT / NO_TRADE
  無法判斷 —— 資料拿不到，**不是不動作**，程式仍正常結束
  致命     —— 登入失敗，以錯誤結束

不斷言重試間隔或重試次數（那是實作細節）；只斷言「會再試」與「試完會做什麼」。
唯一的呼叫觀察是「**零**呼叫」——那是在證明某件事沒發生，屬於行為斷言。
"""

from datetime import date

from conftest import RecordingNotifier, make_config
from broker import LoginFailed, OpenPrices, QuoteNotReady
from broker.fake import FakeBroker
from main import run_entry
from strategy import LONG

# 2026/07/31 的實際開盤價（期交所一般時段）——大台高於另兩者，訊號為做多。
# 刻意不用 08/06 那組：那天大台夾在中間，訊號是不動作，會跟「無法判斷」混淆。
D = date(2026, 7, 31)
GOOD = OpenPrices(tx=42331, mtx=42298, tmf=42265)


def _run(broker, cfg=None):
    notifier = RecordingNotifier()
    outcome = run_entry(
        cfg or make_config(),
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
    outcome, _ = _run(broker, cfg=make_config(quote_retry_attempts=1))
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
    outcome, notifier = _run(broker, cfg=make_config(discord_enabled=False))
    assert outcome.exit_code != 0
    assert notifier.sent == []


# --- 非預期例外不可靜默逃出（code-review 2026-08-09 發現的真實漏洞）---


class _ExplodingBroker:
    """模擬環境壞掉：COM 沒註冊會 ImportError、CreateObject 會 OSError。

    這兩種都**不是** LoginFailed。原本的實作只攔 LoginFailed，
    於是例外直接逃出 run_entry，一則通知都不發——使用者會以為今天只是沒訊號。
    """

    def __init__(self, error, fail_on="login"):
        self._error = error
        self._fail_on = fail_on
        self.open_price_calls = 0

    def login(self):
        if self._fail_on == "login":
            raise self._error

    def get_contracts(self):
        from broker import PRODUCT_CODES, ContractInfo
        return {c: ContractInfo(code=c, last_trading_day=20260819) for c in PRODUCT_CODES}

    def get_open_prices(self, expected_trading_day=None):
        self.open_price_calls += 1
        raise self._error


def test_unexpected_login_error_still_notifies_and_exits_with_error():
    broker = _ExplodingBroker(ImportError("comtypes 沒裝"))
    outcome, notifier = _run(broker)
    assert outcome.exit_code != 0
    assert len(notifier.sent) == 1, "非預期例外也必須通知，絕不可靜默"
    assert "comtypes" in notifier.text


def test_unexpected_quote_error_is_fatal_and_not_retried():
    """重試 ImportError 三次沒有意義，只是拖時間。"""
    broker = _ExplodingBroker(OSError("COM 物件建立失敗"), fail_on="quote")
    outcome, notifier = _run(broker)
    assert outcome.exit_code != 0
    assert broker.open_price_calls == 1, "非預期例外不該重試"
    assert len(notifier.sent) == 1


def test_zero_retry_attempts_is_rejected_when_config_is_built():
    """設定成 0 次會讓迴圈一次都不跑（原本拋 TypeError）。

    驗證發生在 Config 建構時，不是執行到一半才發現——
    設定錯誤偽裝成「今日無訊號」會讓人以為是市場問題。
    """
    import pytest

    with pytest.raises(ValueError, match="retry_attempts"):
        make_config(quote_retry_attempts=0)
