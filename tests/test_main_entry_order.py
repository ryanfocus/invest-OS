"""進場下單 —— 開關、方向、口數。

這個檔案守的是本系統唯一會動到錢的地方，所以斷言的重點跟其他檔案不同：
其他地方問「算出什麼」，這裡問「**做了什麼**」，而且包含「**什麼都沒做**」。

「下單函式一次都沒被呼叫」只有在能觀察呼叫紀錄時才證明得了——
這正是 SPEC 把 `broker` 定為 seam 的理由。
"""

from datetime import date

import pytest

from broker import BUY, MTX_CODE, SELL, TMF_CODE, TX_CODE, ContractInfo, OpenPrices
from broker.fake import FakeBroker
from conftest import RecordingNotifier, make_config
from main import run_entry
from state import PositionRecord, read_position, write_position
from strategy import LONG, NO_TRADE, SHORT

D = date(2026, 8, 10)

# 2026/07/31 實際開盤價：大台高於另兩者 → 做多。
LONG_OPENS = OpenPrices(tx=42331, mtx=42298, tmf=42265)
# 2026/08/10 實際開盤價：大台低於另兩者 → 做空。
SHORT_OPENS = OpenPrices(tx=44987, mtx=45000, tmf=45007)
# 2026/08/06 實際開盤價：大台夾在中間 → 不動作。
NO_TRADE_OPENS = OpenPrices(tx=44177, mtx=44142, tmf=44258)

CONTRACTS = {
    TX_CODE: ContractInfo(code=TX_CODE, last_trading_day=20260819, order_code="TX08"),
    MTX_CODE: ContractInfo(code=MTX_CODE, last_trading_day=20260819, order_code="MTX08"),
    TMF_CODE: ContractInfo(code=TMF_CODE, last_trading_day=20260819, order_code="TM2608"),
}


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """每個測試都用自己的狀態檔。

    autouse 是刻意的：少了它，任何一個開著下單開關的測試都會寫進
    專案裡真正的 state/position.json，污染開發機的實際部位記錄。
    """
    import main
    path = tmp_path / "position.json"
    monkeypatch.setattr(main, "STATE_PATH", str(path))
    return path


def _run(opens, cfg=None, broker=None, state_path=None, today=D):
    broker = broker or FakeBroker(script=[opens], contracts=CONTRACTS)
    notifier = RecordingNotifier()
    outcome = run_entry(
        cfg or make_config(auto_order_enabled=True),
        today=today,
        broker=broker,
        notify=notifier,
        sleep=lambda _s: None,
        state_path=state_path,
    )
    return outcome, notifier, broker


# --- 開關開啟：依訊號方向送單 ---


def test_long_signal_places_a_buy_order():
    _, _, broker = _run(LONG_OPENS)
    assert len(broker.orders) == 1
    assert broker.orders[0].side == "BUY"


def test_short_signal_places_a_sell_order():
    _, _, broker = _run(SHORT_OPENS)
    assert broker.orders[0].side == "SELL"


def test_lot_size_comes_from_config():
    """程式不檢查槓桿或保證金——設定幾口就送幾口。"""
    cfg = make_config(auto_order_enabled=True, order_lots=3)
    _, _, broker = _run(LONG_OPENS, cfg=cfg)
    assert broker.orders[0].lots == 3


def test_product_comes_from_config():
    cfg = make_config(auto_order_enabled=True, order_product=TMF_CODE)
    _, _, broker = _run(LONG_OPENS, cfg=cfg)
    assert broker.orders[0].product == TMF_CODE


# --- 不該下單的情況：斷言「什麼都沒做」 ---


def test_switch_off_places_no_order_at_all():
    """ticket 04 的核心驗收條件。開關關閉時，下單函式**一次都不准被呼叫**。

    不是「送出後被擋下」，是根本沒走到那裡——這個差別在真實系統很重要，
    因為送出去之後就沒有反悔的餘地了。
    """
    _, notifier, broker = _run(LONG_OPENS, cfg=make_config(auto_order_enabled=False))
    assert broker.orders == []
    assert len(notifier.sent) == 1, "不下單不代表不發訊號——訊號才是這個系統的主要產出"


def test_no_trade_signal_places_no_order_even_with_the_switch_on():
    """『不動作』是判斷出來的結果，不是故障——它的正確行為就是不下單。"""
    _, notifier, broker = _run(NO_TRADE_OPENS)
    assert broker.orders == []
    assert len(notifier.sent) == 1


def test_no_order_when_the_quote_never_became_ready():
    """『無法判斷』時不可以猜一個方向下單。"""
    from broker import QuoteNotReady
    broker = FakeBroker(script=[QuoteNotReady("TX00AM 開盤價為 0")], contracts=CONTRACTS)
    _run(LONG_OPENS, broker=broker)
    assert broker.orders == []


def test_no_order_on_a_non_trading_day():
    from broker import QuoteNotReady  # noqa: F401  (只為說明非交易日連報價都不取)
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS)
    run_entry(
        make_config(auto_order_enabled=True),
        today=date(2026, 8, 8),          # 週六
        broker=broker,
        notify=RecordingNotifier(),
        sleep=lambda _s: None,
    )
    assert broker.orders == []


# --- 發報與下單的先後 ---


def test_signal_is_published_before_the_order_is_sent():
    """下單失敗不可以讓訊號跟著消失。

    訊號是主要產出、下單是附加的。反過來寫（先下單再發報）時，
    下單那一步一拋例外，使用者就完全不知道今天該做什麼了。
    """
    from broker import OrderFailed
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS,
                        order_error=OrderFailed("代碼 1038：保證金不足"))
    _, notifier, _ = _run(LONG_OPENS, broker=broker)
    assert "42331" in notifier.text, "訊號必須已經送達"


def test_order_failure_is_reported_and_not_swallowed():
    from broker import OrderFailed
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS,
                        order_error=OrderFailed("代碼 1038：保證金不足"))
    outcome, notifier, _ = _run(LONG_OPENS, broker=broker)
    assert outcome.exit_code != 0, "送不出去是異常，不可以看起來像正常的一天"
    assert "1038" in notifier.text
    assert len(notifier.sent) == 2, "訊號一則、下單失敗告警一則"


def test_order_carries_the_contract_month_from_the_product_list():
    """下單要指名合約年月，不能讓券商替我們挑。

    群益的 `bstrStockNo` 帶月份代碼時，若該月已過期會**自動改送隔年同月**
    （官方文件明載）。年月來自 ticket 03 查到的商品清單，是唯一可信的來源。
    """
    _, _, broker = _run(LONG_OPENS)
    assert broker.orders[0].contract_month == "202608"


def test_order_uses_the_order_code_not_the_quote_code():
    """委託帶的是下單代碼（MTX08），不是報價代碼（MTX00AM）。

    寫錯不會靜靜地做錯事——群益會拒絕——但錯誤訊息不會告訴你是這個原因，
    所以留一條測試在這裡當路標。
    """
    _, _, broker = _run(LONG_OPENS)
    assert broker.orders[0].order_code == "MTX08"
    assert broker.orders[0].product == MTX_CODE, "報價代碼仍要留著，狀態檔與對帳用得到"


# --- 狀態檔：下午出場那一班唯一的依據 ---


def test_a_filled_order_is_recorded(state_file):
    _run(LONG_OPENS)
    record = read_position(path=str(state_file))
    assert record.side == BUY
    assert record.trading_day == 20260810
    assert record.contract_month == "202608"


def test_the_recorded_lots_come_from_the_fill_not_the_request(state_file):
    """委託 3 口、只成交 1 口 → 記 1。

    記成 3 的話，下午會送出 3 口的反向委託：平掉手上那 1 口，
    另外 2 口變成方向相反的**新倉**，而且沒有人會去平它。
    """
    cfg = make_config(auto_order_enabled=True, order_lots=3)
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS, fills=[1])
    _run(LONG_OPENS, cfg=cfg, broker=broker)
    assert read_position(path=str(state_file)).lots == 1


def test_nothing_is_recorded_when_the_switch_is_off(state_file):
    """沒有部位就不該有記錄，否則下午會去平一個不存在的東西。"""
    _run(LONG_OPENS, cfg=make_config(auto_order_enabled=False))
    assert read_position(path=str(state_file)) is None


def test_nothing_is_recorded_for_a_no_trade_signal(state_file):
    _run(NO_TRADE_OPENS)
    assert read_position(path=str(state_file)) is None


def test_an_unfilled_order_is_not_recorded_as_a_position(state_file):
    """市價 IOC 完全沒成交是可能的（瞬間沒有對手價）。0 口不是部位。"""
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS, fills=[0])
    _run(LONG_OPENS, broker=broker)
    assert read_position(path=str(state_file)) is None


# --- 重複執行 ---


def test_running_twice_on_the_same_day_places_only_one_order(state_file):
    """排程重試、或人工手動再跑一次，都不可以變成兩倍部位。"""
    _, _, first = _run(LONG_OPENS)
    _, _, second = _run(LONG_OPENS)
    assert len(first.orders) == 1
    assert second.orders == [], "當日已有記錄就不該再下單"


def test_yesterdays_record_does_not_block_todays_entry(state_file):
    """昨天的記錄不是今天的部位。擋住今天等於整天不交易。"""
    write_position(
        PositionRecord(trading_day=20260807, product=MTX_CODE,
                       contract_month="202608", side=SELL, lots=1),
        path=str(state_file),
    )
    _, _, broker = _run(LONG_OPENS)
    assert len(broker.orders) == 1


def test_a_corrupted_state_file_stops_the_order(state_file):
    """讀不懂就不知道帳上有沒有部位，這時候下單可能變成加倉或反向新倉。

    正確行為是停手並告警——不確定的時候什麼都不做，比猜一個好。
    """
    state_file.write_text('{"trading_day": 2026', encoding="utf-8")
    outcome, notifier, broker = _run(LONG_OPENS)
    assert broker.orders == []
    assert outcome.exit_code != 0
    assert len(notifier.sent) == 2, "訊號一則、狀態檔壞掉告警一則"
