"""非交易日的行為 —— 完全靜默。

「靜默」在這個系統有特殊份量：Discord 的沉默只准有一種解釋，就是今天休市。
所以非交易日不只是不發訊息，連登入都不做——登入失敗會發告警，那就破功了。
"""

from datetime import date

from broker import MTX_CODE, OpenPrices, Quote, TMF_CODE, TX_CODE
from broker.fake import FakeBroker
from conftest import CONTRACTS, RecordingNotifier, make_config, observations_path, state_path
from main import run_entry
from strategy import LONG

GOOD = OpenPrices(tx=42331, mtx=42298, tmf=42265)

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
        state_path=state_path(),
        observations_path=observations_path(),
        fetch_official=lambda day: None,
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
    assert outcome.signal is not None, "補班日照常算訊號"


# --- 近月合約與結算日 ---


def test_trading_day_reports_the_front_month_contracts():
    """出場那一班要知道當初交易的是哪個合約，而那筆記錄源自這裡。"""
    outcome, _, _ = _run(TRADING_DAY)
    assert outcome.contracts[TX_CODE].contract_month == "202608"


def test_settlement_day_is_flagged_from_the_product_list():
    outcome, _, _ = _run(SETTLEMENT)
    assert outcome.is_settlement_day is True


def test_ordinary_day_is_not_flagged_as_settlement():
    outcome, _, _ = _run(TRADING_DAY)
    assert outcome.is_settlement_day is False


# --- 沒有人公告的休市：日曆不知道，資料的日期知道 ---
#
# 颱風假的公告在前一晚或當天清晨才出來，設定檔多半來不及更新，
# 所以「請使用者填 extra_closures」不能當成防護——那只是補救。
# 真正的防線是：期交所沒開市，群益就繼續給**上一個交易日**的報價，
# 而那筆報價帶著自己的日期。日期對不上就不判斷、不下單。
#
# 這幾條刻意用 `quotes=`（會走真正的 build_open_prices），不用 script=。
# script= 回的是成品 OpenPrices，證明不了 run_entry 有把今天的日期傳下去。

TYPHOON_DAY = date(2026, 8, 11)      # 週二，日曆看起來完全正常
PREVIOUS_SESSION = 20260810

STALE_QUOTES = {
    code: Quote(code=code, open=price, limit_up=48726, limit_down=39868,
                trading_day=PREVIOUS_SESSION)
    for code, price in ((TX_CODE, 44987), (MTX_CODE, 45000), (TMF_CODE, 45007))
}


def test_unannounced_closure_produces_no_signal_even_though_the_data_looks_perfect():
    """颱風假沒填進設定檔時，唯一擋得住的就是報價自己的交易日。

    這組價格非 0、落在漲跌停內、數值合理——L1 與 L2 全部會放行，
    照樣算得出 SHORT。擋下它的是「這是 08/10 的價格，今天是 08/11」。
    """
    outcome, _, _ = _run(TYPHOON_DAY, broker=FakeBroker(quotes=STALE_QUOTES,
                                                        contracts=CONTRACTS))
    assert outcome.signal is None, "用上一個交易日的價格算出來的訊號不算數"
    assert outcome.exit_code == 0, "休市不是程式錯誤"


def test_the_same_quotes_are_accepted_on_the_day_they_belong_to():
    """對照組：資料沒問題，被拒絕的理由確實是日期而不是資料本身。"""
    outcome, _, _ = _run(date(2026, 8, 10), broker=FakeBroker(quotes=STALE_QUOTES,
                                                              contracts=CONTRACTS))
    assert outcome.signal is not None


def test_stale_data_reason_reaches_discord_with_both_dates():
    """使用者要能一眼看出「今天沒開市」，而不是以為程式壞了。"""
    _, notifier, _ = _run(TYPHOON_DAY, broker=FakeBroker(quotes=STALE_QUOTES,
                                                         contracts=CONTRACTS))
    assert "20260810" in notifier.text and "20260811" in notifier.text
