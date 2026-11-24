"""進場的時間表（ADR-0006）：08:44 起跑、開盤前準備好、08:45:05 讀開盤價、試到 entry_cutoff（08:50）。

使用者決定的時刻——**08:45:05 第一次讀**、**試到 entry_cutoff**、補跑與 `none` 時
**試一分鐘**——這裡就斷言那些。中間的節奏（每秒重讀、重連一次、之後每 10 秒）是實作細節，
只斷言「幾秒內就會補讀到」「會重連、只主動重連一次」這種行為，不斷言間隔秒數。

SPEC 的測試原則是只斷言三種外部效果（委託、Discord、狀態檔）。這個檔案是例外：
時間表本身就是需求，而「什麼時候去讀」只能從 broker 被呼叫的時刻看出來。

時間用 `FakeClock`：只有 `sleep` 會讓它前進，所以等待與重試在測試裡瞬間走完，
而 broker 可以問它「現在幾點」，記下每一次讀開盤價的時刻。
"""

from datetime import date, datetime, time, timedelta

from broker import OpenPrices, OrderFailed, ProductListUnavailable, QuoteNotReady
from broker.fake import FakeBroker
from conftest import CONTRACTS, RecordingNotifier, make_config, observations_path, state_path
from main import run_entry
from state import read_position
from strategy import LONG

D = date(2026, 7, 31)
# 2026/07/31 的實際開盤價：大台高於另兩者 → 做多
GOOD = OpenPrices(tx=42331, mtx=42298, tmf=42265)
NOT_READY = QuoteNotReady("TX00AM 開盤價為 0.0（尚未成交）")


class FakeClock:
    def __init__(self, hh, mm, ss=0):
        self.t = datetime.combine(D, time(hh, mm, ss))

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class TimedBroker(FakeBroker):
    """記下每一次讀開盤價的時刻；`ready_at` 之前一律未就緒。

    `ready_after_reconnect=True` 時，則是**重連之前**一律未就緒——
    模擬「報價連線斷了卻沒人發現」。
    """

    def __init__(self, clock, *, ready_at=None, ready_after_reconnect=False, **kw):
        kw.setdefault("contracts", CONTRACTS)
        super().__init__(script=[GOOD], **kw)
        self.clock = clock
        self.ready_at = ready_at
        self.ready_after_reconnect = ready_after_reconnect
        self.read_times: list = []
        self.reconnect_times: list = []

    def reconnect_quotes(self):
        super().reconnect_quotes()
        self.reconnect_times.append(self.clock.now().time())

    def get_open_prices(self, expected_trading_day=None):
        self.read_times.append(self.clock.now().time())
        self.open_price_calls += 1
        self.calls.append("get_open_prices")
        if self.ready_after_reconnect and not self.reconnect_calls:
            raise NOT_READY
        if self.ready_at is not None and self.clock.now().time() < self.ready_at:
            raise NOT_READY
        return GOOD


def _run(broker, clock, cfg=None):
    notifier = RecordingNotifier()
    outcome = run_entry(
        cfg or make_config(),
        today=D,
        broker=broker,
        notify=notifier,
        now=clock.now().time(),
        wall_clock=clock,
        state_path=state_path(),
        observations_path=observations_path(),
        fetch_official=lambda day: None,
    )
    return outcome, notifier


# --- 08:45:05 才讀，開盤前把能做的先做完 ---


def test_the_first_read_happens_at_0845_05():
    """08:44 起跑，第一次讀開盤價是 08:45:05——使用者定的時刻。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock)
    outcome, _ = _run(broker, clock)
    assert broker.read_times == [time(8, 45, 5)]
    assert outcome.signal == LONG


def test_everything_that_can_be_done_before_the_open_is_done_before_the_first_read():
    """登入、商品清單、訂閱、下單準備都排在讀開盤價**之前**，下單緊接在後。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock)
    _run(broker, clock, cfg=make_config(auto_order_enabled=True, order_product="TM0000AM"))
    first_read = broker.calls.index("get_open_prices")
    assert broker.calls.index("subscribe_quotes") < first_read
    assert broker.calls.index("prepare_order") < first_read
    assert broker.calls.index("place_order") > first_read


def test_order_preparation_is_skipped_when_auto_order_is_off():
    """開關關著時，下單那條路連準備都不碰。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock)
    _run(broker, clock)
    assert broker.prepare_calls == 0
    assert broker.orders == []


def test_a_failed_preparation_does_not_stop_the_signal_or_the_order():
    """開盤前準備失敗只記 log：訊號照發，送單時 `place_order` 會再準備一次。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, prepare_error=OrderFailed("回報連線失敗，代碼 1001"))
    outcome, notifier = _run(broker, clock, cfg=make_config(auto_order_enabled=True,
                                                            order_product="TM0000AM"))
    assert outcome.signal == LONG
    assert outcome.exit_code == 0
    assert len(broker.orders) == 1
    assert len(notifier.sent) == 1, "準備失敗本身不發訊息——送單也失敗了才是下單失敗"


def test_a_late_start_reads_immediately():
    """補跑（例如 08:47 才被叫起來）不等，直接讀。"""
    clock = FakeClock(8, 47, 30)
    broker = TimedBroker(clock)
    _run(broker, clock)
    assert broker.read_times == [time(8, 47, 30)]


# --- 沒拿齊：重試到 08:50 ---


def test_keeps_trying_until_0850_and_then_reports_no_signal():
    """一直拿不齊 → 最後一次讀在 08:50:00，之後不再讀，發一則「今日無訊號」。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, ready_at=time(23, 59))
    outcome, notifier = _run(broker, clock)
    assert broker.read_times[0] == time(8, 45, 5)
    assert broker.read_times[-1] == time(8, 50)
    assert outcome.signal is None
    assert outcome.exit_code == 0, "拿不到資料是預期內的情況，不是程式錯誤"
    assert len(notifier.sent) == 1
    assert "TX00AM" in notifier.text


def test_reconnects_exactly_once_after_the_first_minute():
    """開盤後一分鐘還拿不齊，最可能是報價連線斷了 → 重連一次，不是一直重連。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, ready_at=time(23, 59))
    _run(broker, clock)
    assert broker.reconnect_calls == 1
    assert broker.reconnect_times[0] >= time(8, 46)


def test_a_failed_reconnect_does_not_end_the_day_early():
    """重連也連不上 → 不是致命錯誤，照樣重試到界線，最後發「今日無訊號」。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, ready_at=time(23, 59),
                         reconnect_error=QuoteNotReady("連線報價主機失敗，代碼 1001"))
    outcome, notifier = _run(broker, clock)
    assert broker.read_times[-1] == time(8, 50)
    assert outcome.exit_code == 0
    assert len(notifier.sent) == 1


def test_a_failed_pre_open_subscription_does_not_stop_the_day():
    """開盤前訂閱失敗只記下來——讀開盤價時 broker 會再訂一次，那一段有自己的重試。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, subscribe_error=QuoteNotReady("訂閱報價失敗，代碼 3030"))
    outcome, _ = _run(broker, clock)
    assert outcome.signal == LONG
    assert outcome.exit_code == 0


def test_an_unexpected_subscription_error_is_fatal_and_said_out_loud():
    """COM 壞掉這類非預期錯誤不重試，但一定要發出去——靜默結束會被當成「今天休市」。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, subscribe_error=OSError("COM 物件建立失敗"))
    outcome, notifier = _run(broker, clock)
    assert outcome.exit_code != 0
    assert broker.open_price_calls == 0
    assert "訂閱報價" in notifier.text


def test_reconnecting_rescues_a_dead_quote_connection():
    """斷線的報價重連之後就拿得到 → 訊號照常產生。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, ready_after_reconnect=True)
    outcome, _ = _run(broker, clock)
    assert outcome.signal == LONG
    assert broker.reconnect_calls == 1


def test_data_that_arrives_a_few_seconds_late_is_picked_up_within_seconds():
    """08:45:08 才到齊 → 08:45:08 就讀到，不是等下一個大間隔。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, ready_at=time(8, 45, 8))
    outcome, _ = _run(broker, clock)
    assert outcome.signal == LONG
    assert broker.read_times[-1] <= time(8, 45, 9)


def test_a_late_start_after_0850_still_gets_a_minute():
    """08:55 補跑：早就過了 08:50，但不能只讀一次就放棄——至少試一分鐘。"""
    clock = FakeClock(8, 55)
    broker = TimedBroker(clock, ready_at=time(8, 55, 20))
    outcome, _ = _run(broker, clock)
    assert outcome.signal == LONG

    clock = FakeClock(8, 55)
    broker = TimedBroker(clock, ready_at=time(23, 59))
    _run(broker, clock)
    assert broker.read_times[-1] == time(8, 56)


# --- 時間關卡看的是「要下單的那一刻」---


def test_the_cutoff_is_checked_when_the_order_would_be_sent_not_when_the_run_started():
    """08:49:30 補跑、08:50:10 才拿齊 → 下單那一刻已過界線，不下單。

    起跑時間還沒過 08:50，用它來比的話這筆單會送出去。
    （補跑至少試一分鐘，所以 08:50:10 那次讀得到；訊號照發，只是不下單。）
    """
    clock = FakeClock(8, 49, 30)
    broker = TimedBroker(clock, ready_at=time(8, 50, 10))
    outcome, notifier = _run(broker, clock, cfg=make_config(auto_order_enabled=True,
                                                            order_product="TM0000AM"))
    assert outcome.signal == LONG
    assert broker.orders == []
    assert "08:50" in notifier.text


# --- 放棄的時刻就是 entry_cutoff（2026-11-23 與使用者決定合併）---


def test_the_give_up_time_follows_entry_cutoff():
    """設定改成 08:48，就試到 08:48——放棄時刻不是寫死在程式裡的另一個數字。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, ready_at=time(23, 59))
    _run(broker, clock, cfg=make_config(entry_cutoff=time(8, 48)))
    assert broker.read_times[-1] == time(8, 48)


def test_no_cutoff_tries_for_one_minute():
    """`entry_cutoff: none` 沒有放棄的時刻可用 → 從第一次讀起試一分鐘。"""
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, ready_at=time(23, 59))
    _run(broker, clock, cfg=make_config(entry_cutoff=None))
    assert broker.read_times[0] == time(8, 45, 5)
    assert broker.read_times[-1] == time(8, 46, 5)


# --- 商品清單：連報價主機失敗是暫時的 ---


def test_product_list_failures_are_retried():
    """第一次連報價主機失敗、第二次清單不完整、第三次成功 → 照常產生訊號。

    原本連線失敗拋的 QuoteNotReady 不在這一步的認知範圍裡，會被當成
    「非預期錯誤」以致命錯誤結束整天。
    """
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, contracts_error=[
        QuoteNotReady("連線報價主機失敗"),
        ProductListUnavailable("8 秒內未取得完整商品清單"),
    ])
    outcome, _ = _run(broker, clock)
    assert outcome.signal == LONG
    assert broker.contract_calls == 3


def test_a_product_list_that_never_comes_ends_as_no_signal_not_as_a_crash():
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, contracts_error=QuoteNotReady("連線報價主機失敗"))
    outcome, notifier = _run(broker, clock)
    assert outcome.signal is None
    assert outcome.exit_code == 0
    assert broker.open_price_calls == 0
    assert len(notifier.sent) == 1


# --- 0 口成交一定要講 ---


def test_an_order_that_fills_nothing_is_reported():
    """市價 IOC 送出了但 0 口成交 → 發一則「沒有成交」，不寫部位記錄。

    原本只記 log、以 0 結束：使用者手上只有早上那則訊號，會以為已經進場。
    """
    clock = FakeClock(8, 44)
    broker = TimedBroker(clock, fills=[0])
    outcome, notifier = _run(broker, clock, cfg=make_config(auto_order_enabled=True,
                                                            order_product="TM0000AM"))
    assert outcome.signal == LONG
    assert outcome.exit_code == 1
    assert len(notifier.sent) == 2, "訊號一則、沒有成交一則"
    assert "沒有成交" in notifier.sent[-1]["content"]
    assert read_position(path=state_path()) is None
