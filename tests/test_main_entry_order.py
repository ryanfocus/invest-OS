"""進場下單 —— 開關、方向、口數。

這個檔案守的是本系統唯一會動到錢的地方，所以斷言的重點跟其他檔案不同：
其他地方問「算出什麼」，這裡問「**做了什麼**」，而且包含「**什麼都沒做**」。

「下單函式一次都沒被呼叫」只有在能觀察呼叫紀錄時才證明得了——
這正是 SPEC 把 `broker` 定為 seam 的理由。
"""

from datetime import date

import pytest

from broker import BUY, MTX_CODE, OpenPrices, SELL, TMF_CODE, TX_CODE
from broker.fake import FakeBroker
from conftest import CONTRACTS, RecordingNotifier, make_config, observations_path, state_path
from main import run_entry
from state import PositionRecord, read_position, write_position

D = date(2026, 8, 10)

# 2026/07/31 實際開盤價：大台高於另兩者 → 做多。
LONG_OPENS = OpenPrices(tx=42331, mtx=42298, tmf=42265)
# 2026/08/10 實際開盤價：大台低於另兩者 → 做空。
SHORT_OPENS = OpenPrices(tx=44987, mtx=45000, tmf=45007)
# 2026/08/06 實際開盤價：大台夾在中間 → 不動作。
NO_TRADE_OPENS = OpenPrices(tx=44177, mtx=44142, tmf=44258)

@pytest.fixture
def state_file(isolated_state_file):
    """本次測試的狀態檔（conftest 的 autouse fixture 已經建好路徑）。"""
    return isolated_state_file


def _run(opens, cfg=None, broker=None, today=D):
    broker = broker or FakeBroker(script=[opens], contracts=CONTRACTS)
    notifier = RecordingNotifier()
    outcome = run_entry(
        cfg or make_config(auto_order_enabled=True),
        today=today,
        broker=broker,
        notify=notifier,
        sleep=lambda _s: None,
        state_path=state_path(),
        observations_path=observations_path(),
        fetch_official=lambda day: None,
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
    """非交易日連報價都不取，自然也不會下單。"""
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS)
    _run(LONG_OPENS, broker=broker, today=date(2026, 8, 8))   # 週六
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


def test_order_failure_still_reports_the_signal_that_was_computed():
    """下單失敗**不等於**無法判斷。

    CONTEXT.md 把 `訊號為 None` 保留給「根本沒算出訊號」。這裡訊號算出來了、
    也發出去了，只是委託沒送成功——填 None 會讓兩種完全不同的處境撞在一起，
    對帳與排查時分不出到底發生了什麼事。
    """
    from broker import OrderFailed
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS,
                        order_error=OrderFailed("代碼 1038"))
    outcome, _, _ = _run(LONG_OPENS, broker=broker)
    assert outcome.signal == "LONG"
    assert outcome.opens is not None, "開盤價也是事實，一起留著"


def test_order_failure_does_not_claim_a_notification_that_never_happened():
    """Discord 關閉時 notified 必須是 False，不可以寫死成 True。"""
    from broker import OrderFailed
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS,
                        order_error=OrderFailed("代碼 1038"))
    cfg = make_config(auto_order_enabled=True, discord_enabled=False)
    outcome, notifier, _ = _run(LONG_OPENS, cfg=cfg, broker=broker)
    assert notifier.sent == []
    assert outcome.notified is False


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


def test_an_expired_contract_is_never_ordered(state_file):
    """**過期的合約絕不可以送出去。**

    群益的月份代碼（MTX08）在該月已過期時會**自動改送隔年同月且不報錯**
    （官方文件 SendFutureOrderCLR 備註明載）。也就是說 2026 年 9 月送 MTX08，
    成交的會是 2027 年 8 月的合約——完全不同的東西，而且不會有任何錯誤訊息。

    正常情況下商品清單給的近月合約不會過期，所以這條守的是「清單過期或解析錯了」。
    """
    from broker import ContractInfo
    stale = {
        code: ContractInfo(code=info.code, last_trading_day=20260717,   # 上個月就到期了
                           order_code=info.order_code)
        for code, info in CONTRACTS.items()
    }
    broker = FakeBroker(script=[LONG_OPENS], contracts=stale)
    outcome, notifier, _ = _run(LONG_OPENS, broker=broker)
    assert broker.orders == [], "寧可今天不交易，也不要交易到隔年的合約"
    assert outcome.exit_code != 0
    assert "20260717" in notifier.text, "要講出是哪個日期過期了"


def test_a_contract_expiring_today_is_still_tradable(state_file):
    """結算日當天照常進場——合約要到 13:30 才停止交易。"""
    _, _, broker = _run(LONG_OPENS, today=date(2026, 8, 19))
    assert len(broker.orders) == 1


# --- 狀態檔：下午出場那一班唯一的依據 ---


def test_a_filled_order_is_recorded(state_file):
    _run(LONG_OPENS)
    record = read_position(path=str(state_file))
    assert record.side == BUY
    assert record.trading_day == 20260810
    assert record.contract_month == "202608"


def test_the_record_carries_the_order_code_end_to_end(state_file):
    """出場要靠這個欄位送反向委託，所以它必須真的**被寫進去**。

    只驗 `write_position`/`read_position` 的往返證明不了這件事——
    突變測試確認過：把 main.py 寫入的 order_code 改成空字串，
    其餘 182 個測試依然全綠。
    """
    _run(LONG_OPENS)
    assert read_position(path=str(state_file)).order_code == "MTX08"


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


def test_a_repeat_run_sends_no_second_discord_message(state_file):
    """SPEC 進場流程第 2 步：「檢查狀態檔是否已有今日記錄 → 有則跳過」
    排在登入與發報**之前**。

    放到下單那一步才檢查的話，重跑雖然不會重複下單，卻會再發一則一模一樣的訊號，
    使用者看到兩則會以為系統出了什麼事。
    """
    _run(LONG_OPENS)
    _, notifier, _ = _run(LONG_OPENS)
    assert notifier.sent == [], "當日已經跑過，重跑要完全靜默"


def test_a_repeat_run_does_not_even_log_in(state_file):
    """既然決定跳過，就不該再去碰券商——登入失敗會變成一則無謂的告警。"""
    _run(LONG_OPENS)
    _, _, broker = _run(LONG_OPENS)
    assert broker.login_calls == 0


def test_yesterdays_record_does_not_block_todays_entry(state_file):
    """昨天的記錄不是今天的部位。擋住今天等於整天不交易。"""
    write_position(
        PositionRecord(trading_day=20260807, product=MTX_CODE, order_code="MTX08",
                       contract_month="202608", side=SELL, lots=1,
                       requested_lots=1, last_trading_day=20260819),
        path=str(state_file),
    )
    _, _, broker = _run(LONG_OPENS)
    assert len(broker.orders) == 1


# --- 送單後收不到成交回報（ticket 05）---
#
# 「委託失敗」與「不知道成交沒有」是兩件完全不同的事：
#   委託失敗   → 確定沒有部位 → 下午什麼都不用做
#   回報沒回來 → **可能已經成交** → 下午絕不可以自動送單，要人去看帳戶
#
# 混為一談的代價：當成失敗處理 → 不寫狀態檔 → 13:40 不去平 → 部位進夜盤。


def _unknown_fill_broker():
    """推播沒來，**而且 ticket 09 的成交查詢也答不出來**。

    `query_result=None` 是這一組測試的前提：它們驗的是「連查詢都救不回來」
    之後的行為（記成不確定、告警、重跑不再送單）。
    查得到的那條路徑由 `test_fill_query_fallback.py` 負責。

    ⚠️ 不寫這個參數的話，假 broker 會直接 AssertionError 而不是靜靜通過——
    ticket 09 加上查詢那一步時，正是這個哨兵指出這七條測試的前提變了。
    """
    from broker import FillUnknown
    return FakeBroker(
        script=[LONG_OPENS], contracts=CONTRACTS,
        order_error=FillUnknown("10 秒內委託 SEQ0000000001 仍未結束",
                                order_seq="SEQ0000000001"),
        query_result=None,
    )


def test_an_unconfirmed_fill_is_recorded_as_uncertain(state_file):
    """回報沒到時**仍要留下記錄**——什麼都不寫的話，下午那班會以為今天沒進場。"""
    from state import UNCERTAIN
    _run(LONG_OPENS, broker=_unknown_fill_broker())
    record = read_position(path=str(state_file))
    assert record is not None, "已經送出委託了，這件事必須被記下來"
    assert record.status == UNCERTAIN
    assert record.lots is None, "不知道成交幾口，不可以填一個數字"


def test_the_uncertain_record_keeps_what_a_human_needs_to_check_the_account(state_file):
    """使用者要拿著這筆記錄去 APP 對帳，所以商品、方向、合約都得在。"""
    _run(LONG_OPENS, broker=_unknown_fill_broker())
    record = read_position(path=str(state_file))
    assert record.product == MTX_CODE
    assert record.order_code == "MTX08"
    assert record.side == BUY
    assert record.contract_month == "202608"


def test_the_uncertain_record_carries_the_order_sequence_number(state_file):
    """券商 APP 是用委託序號查單的。沒有它，使用者得從幾百筆裡自己找。"""
    _run(LONG_OPENS, broker=_unknown_fill_broker())
    assert read_position(path=str(state_file)).order_seq == "SEQ0000000001"


def test_the_uncertain_record_says_how_many_lots_were_ordered(state_file):
    """成交幾口不知道，但**委託幾口是知道的**——那是曝險的上界。

    「可能有 1 口」和「可能有 10 口」對使用者是完全不同的緊急程度。
    """
    cfg = make_config(auto_order_enabled=True, order_lots=3)
    _run(LONG_OPENS, cfg=cfg, broker=_unknown_fill_broker())
    record = read_position(path=str(state_file))
    assert record.requested_lots == 3
    assert record.lots is None, "成交幾口仍然是不知道"


# --- 不確定之後重跑：不可以靜默 ---


def test_a_repeat_run_after_an_uncertain_morning_is_not_silent(state_file):
    """**「絕不在不確定自己持有什麼的狀態下靜默結束」對重跑也成立。**

    早上留下不確定記錄之後，人再跑一次通常正是因為想知道現在怎麼了。
    這時候什麼都不說，看起來就像「已經沒事了」——但帳上可能還有部位。
    """
    _run(LONG_OPENS, broker=_unknown_fill_broker())
    outcome, notifier, _ = _run(LONG_OPENS)
    assert notifier.sent != [], "不確定還沒解決，不可以靜悄悄結束"
    assert outcome.exit_code != 0


def test_a_repeat_run_after_a_confirmed_entry_stays_silent(state_file):
    """對照組：早上正常成交的話，重跑就該安靜——那天沒有待處理的事。"""
    _run(LONG_OPENS)
    _, notifier, _ = _run(LONG_OPENS)
    assert notifier.sent == []


def test_an_unconfirmed_fill_asks_for_human_confirmation(state_file):
    """Discord 訊息要能讓人直接行動，不是只說「出錯了」。"""
    _, notifier, _ = _run(LONG_OPENS, broker=_unknown_fill_broker())
    assert "MTX08" in notifier.text, "要講出是哪個商品，否則不知道去看什麼"
    assert "人工確認帳戶實際部位" in notifier.text, "要講出該做什麼，不是只說出事了"


def test_a_rejected_order_does_not_ask_the_user_to_check_the_account(state_file):
    """與上一條合起來，才是「兩者分得開」。

    委託確定沒送出去就沒有部位，叫人去對帳只會製造無謂的緊張——
    而狼來了喊多了，真正該行動的那一則就會被忽略。

    刻意寫成兩條獨立的測試而不是在同一條裡比對兩段文字：那樣寫的話，
    前一次執行留下的狀態檔會讓第二次撞上重複執行保護，根本走不到被拒那條路。
    """
    from broker import OrderFailed
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS,
                        order_error=OrderFailed("代碼 1038：保證金不足"))
    _, notifier, _ = _run(LONG_OPENS, broker=broker)
    assert "人工確認帳戶實際部位" not in notifier.text
    assert "1038" in notifier.text, "但要講清楚為什麼沒送出去"


def test_a_rejected_order_leaves_no_position_record(state_file):
    """對照組：委託確定沒送出去，就不該有任何部位記錄。"""
    from broker import OrderFailed
    broker = FakeBroker(script=[LONG_OPENS], contracts=CONTRACTS,
                        order_error=OrderFailed("代碼 1038：保證金不足"))
    _run(LONG_OPENS, broker=broker)
    assert read_position(path=str(state_file)) is None


def test_an_unconfirmed_fill_exits_with_an_error(state_file):
    """這一天需要有人看，不可以看起來像正常結束。"""
    outcome, _, _ = _run(LONG_OPENS, broker=_unknown_fill_broker())
    assert outcome.exit_code != 0


def test_the_signal_still_goes_out_when_the_fill_is_unconfirmed(state_file):
    """訊號是主要產出，不因為下單那一步出狀況而消失。"""
    _, notifier, _ = _run(LONG_OPENS, broker=_unknown_fill_broker())
    assert "42331" in notifier.text


def test_a_repeat_run_does_not_re_order_after_an_uncertain_record(state_file):
    """**這是不確定狀態最重要的一條。**

    重跑時看到不確定的記錄，絕不可以「因為不知道有沒有成交所以再送一次」——
    那有一半機率變成兩倍部位。已經送過單就是送過了。
    """
    _run(LONG_OPENS, broker=_unknown_fill_broker())
    _, _, second = _run(LONG_OPENS)
    assert second.orders == []


def test_a_corrupted_state_file_stops_the_order(state_file):
    """讀不懂就不知道帳上有沒有部位，這時候下單可能變成加倉或反向新倉。

    正確行為是停手並告警——不確定的時候什麼都不做，比猜一個好。
    """
    state_file.write_text('{"trading_day": 2026', encoding="utf-8")
    outcome, notifier, broker = _run(LONG_OPENS)
    assert broker.orders == []
    assert outcome.exit_code != 0
    assert len(notifier.sent) == 2, "訊號一則、狀態檔壞掉告警一則"
