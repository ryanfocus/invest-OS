"""Discord 發報。

需求 §6：交易日一律發報，內容是三個開盤價與訊號方向。
非交易日不發（由 main 決定不呼叫），沉默因此只有一種解釋——今天休市。
"""

from datetime import date

from notifiers.discord import build_signal_payload
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
