"""非交易日的行為 —— 完全靜默。

「靜默」在這個系統有特殊份量：Discord 的沉默只准有一種解釋，就是今天休市。
所以非交易日不只是不發訊息，連登入都不做——登入失敗會發告警，那就破功了。
"""

from datetime import date

from broker import ContractInfo, OpenPrices
from broker.fake import FakeBroker
from conftest import RecordingNotifier, make_config
from main import run_entry
from strategy import LONG

GOOD = OpenPrices(tx=42331, mtx=42298, tmf=42265)
CONTRACTS = {
    "TX00AM": ContractInfo(code="TX00AM", last_trading_day=20260819),
    "MTX00AM": ContractInfo(code="MTX00AM", last_trading_day=20260819),
    "TM0000AM": ContractInfo(code="TM0000AM", last_trading_day=20260819),
}

TRADING_DAY = date(2026, 8, 10)      # 週一
SATURDAY = date(2026, 8, 8)
NEW_YEAR = date(2026, 1, 1)          # 中華民國開國紀念日（週四）
SETTLEMENT = date(2026, 8, 19)       # 商品清單給的最後交易日


def _run(today, cfg=None, broker=None):
    broker = broker or FakeBroker(script=[GOOD], contracts=CONTRACTS)
    notifier = RecordingNotifier()
    outcome = run_entry(
        cfg or make_config(),
        today=today,
        broker=broker,
        notify=notifier,
        sleep=lambda _s: None,
    )
    return outcome, notifier, broker


# --- 非交易日：什麼都不做 ---


def test_weekend_sends_nothing_and_does_not_log_in():
    outcome, notifier, broker = _run(SATURDAY)
    assert outcome.skipped is True
    assert outcome.exit_code == 0
    assert notifier.sent == [], "沉默只准代表休市——發任何訊息都會破壞這個約定"
    assert broker.login_calls == 0, "連登入都不該做，登入失敗會觸發告警"


def test_public_holiday_sends_nothing():
    outcome, notifier, broker = _run(NEW_YEAR)
    assert outcome.skipped is True
    assert notifier.sent == []
    assert broker.login_calls == 0


def test_skipped_day_is_not_a_signal_and_not_a_failure():
    """非交易日既不是「不動作」也不是「無法判斷」，是第四種結果。"""
    outcome, _, _ = _run(SATURDAY)
    assert outcome.signal is None
    assert outcome.failure is None


# --- 交易日：照常 ---


def test_ordinary_trading_day_runs_normally():
    outcome, notifier, broker = _run(TRADING_DAY)
    assert outcome.skipped is False
    assert outcome.signal == LONG
    assert broker.login_calls == 1
    assert len(notifier.sent) == 1


# --- 臨時開市／休市由設定覆寫 ---


def test_typhoon_closure_from_config_stops_an_ordinary_weekday():
    cfg = make_config(calendar_extra_closures={TRADING_DAY})
    outcome, notifier, broker = _run(TRADING_DAY, cfg=cfg)
    assert outcome.skipped is True
    assert broker.login_calls == 0


def test_makeup_workday_from_config_makes_a_saturday_trade():
    cfg = make_config(calendar_extra_openings={SATURDAY})
    outcome, _, broker = _run(SATURDAY, cfg=cfg)
    assert outcome.skipped is False
    assert broker.login_calls == 1


# --- 近月合約與結算日 ---


def test_trading_day_reports_the_front_month_contracts():
    """出場那一班要知道當初交易的是哪個合約，而那筆記錄源自這裡。"""
    outcome, _, _ = _run(TRADING_DAY)
    assert outcome.contracts["TX00AM"].contract_month == "202608"


def test_settlement_day_is_flagged_from_the_product_list():
    outcome, _, _ = _run(SETTLEMENT)
    assert outcome.is_settlement_day is True


def test_ordinary_day_is_not_flagged_as_settlement():
    outcome, _, _ = _run(TRADING_DAY)
    assert outcome.is_settlement_day is False
