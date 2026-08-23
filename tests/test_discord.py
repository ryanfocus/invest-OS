"""Discord 發報。

需求 §6：交易日一律發報，內容是三個開盤價與訊號方向。
非交易日不發（由 main 決定不呼叫），沉默因此只有一種解釋——今天休市。
"""

from datetime import date

from broker import BUY, MTX_CODE
from notifiers.discord import build_signal_payload
from state import PositionRecord
from strategy import compute

D = date(2026, 7, 31)


def _content(result, d=D):
    return build_signal_payload(result, d)["content"]


def test_message_lists_all_three_open_prices():
    text = _content(compute(tx=42331, mtx=42298, tmf=42265))
    assert "42331" in text
    assert "42298" in text
    assert "42265" in text


def test_message_labels_each_product_so_numbers_are_not_ambiguous():
    text = _content(compute(tx=42331, mtx=42298, tmf=42265))
    assert "大台" in text
    assert "小台" in text
    assert "微台" in text


def test_long_signal_is_shown_in_chinese():
    assert "做多" in _content(compute(tx=42331, mtx=42298, tmf=42265))


def test_short_signal_is_shown_in_chinese():
    assert "做空" in _content(compute(tx=45046, mtx=45184, tmf=45189))


def test_no_trade_signal_is_shown_and_is_not_an_error():
    """不動作是正常結果，訊息不該長得像故障。"""
    text = _content(compute(tx=45353, mtx=45378, tmf=45288))
    assert "不動作" in text
    assert "失敗" not in text
    assert "錯誤" not in text


def test_message_carries_the_trading_date():
    assert "2026/07/31" in _content(compute(tx=42331, mtx=42298, tmf=42265))


def test_open_prices_are_rendered_without_decimals():
    """開盤價還原小數後是浮點數，但台指期報價是整數點位，不該顯示 42331.0。"""
    text = _content(compute(tx=42331.0, mtx=42298.0, tmf=42265.0))
    assert "42331" in text
    assert "42331.0" not in text


def test_the_exit_unknown_message_says_what_was_actually_sent():
    """**這則訊息的口數必須等於出場單真的送出去的口數。**

    出場送的是 `record.lots`（早上**實際成交**的口數），不是 `requested_lots`
    （早上**委託**的口數）。早上委託 3 口只成交 2 口時，下午送的是 2 口。

    而這則訊息的用途是叫人「先確認帳戶實際部位再決定要不要補單」——
    數字錯在這裡，人會拿著錯的數字去對帳，然後補一筆錯的單。

    ⚠️ 對照 `build_exit_blocked_payload`：那一則講的是**早上那筆**委託
    （「早上送出過 N 口的委託，但沒有收到成交回報」），所以它用
    `requested_lots` 是對的。兩則描述的是不同的委託，不可以互相參照。
    """
    from notifiers.discord import build_exit_unknown_payload
    record = PositionRecord(
        trading_day=20260821, product=MTX_CODE, order_code="MTX09",
        contract_month="202609", side=BUY, lots=2, requested_lots=3,
        last_trading_day=20260916, order_seq="SEQ1",
    )
    text = build_exit_unknown_payload(record, "逾時", date(2026, 8, 21))["content"]
    assert "賣出 2 口" in text, f"出場送的是 2 口（實際成交），不是 3 口（委託量）：{text}"
    assert "3 口" not in text


def test_the_exit_blocked_message_still_describes_the_morning_order():
    """對照組：那一則講的**確實是**早上那筆委託，所以用委託口數是對的。

    少了這條，上面那條可以靠「兩則都改成 lots」通過——而那會讓
    「早上送出過 N 口」變成一個我們其實不知道的數字（不確定時 lots 是 None）。
    """
    from notifiers.discord import build_exit_blocked_payload
    from state import UNCERTAIN, UNCERTAIN_ENTRY
    record = PositionRecord(
        trading_day=20260821, product=MTX_CODE, order_code="MTX09",
        contract_month="202609", side=BUY, lots=None, requested_lots=3,
        last_trading_day=20260916, status=UNCERTAIN,
        uncertain_stage=UNCERTAIN_ENTRY, order_seq="SEQ1",
    )
    text = build_exit_blocked_payload(record, date(2026, 8, 21))["content"]
    assert "3 口" in text, "早上委託的是 3 口，那個數字是知道的"
