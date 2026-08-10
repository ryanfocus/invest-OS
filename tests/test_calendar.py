"""交易日判定。

非交易日執行時程式要完全靜默——不登入、不發訊息。判斷錯了有兩種後果：
把交易日當成假日 → 整天沒訊號；把假日當成交易日 → 拿到舊資料算出假訊號。

期望值全部來自 `holidays` 套件的 2026 台灣假期表（獨立事實來源），不是我自己數日曆。
"""

from datetime import date

import pytest

from calendar_tw import is_trading_day


# --- 一般日 ---


@pytest.mark.parametrize("day, label", [
    (date(2026, 8, 10), "週一"),
    (date(2026, 8, 11), "週二"),
    (date(2026, 8, 12), "週三"),
    (date(2026, 8, 13), "週四"),
    (date(2026, 8, 14), "週五"),
])
def test_ordinary_weekdays_are_trading_days(day, label):
    assert is_trading_day(day) is True


@pytest.mark.parametrize("day, label", [
    (date(2026, 8, 8), "週六"),
    (date(2026, 8, 9), "週日"),
])
def test_weekends_are_not_trading_days(day, label):
    assert is_trading_day(day) is False


# --- 國定假日 ---


@pytest.mark.parametrize("day, name", [
    (date(2026, 1, 1), "中華民國開國紀念日"),
    (date(2026, 2, 16), "農曆除夕"),
    (date(2026, 2, 17), "春節"),
    (date(2026, 2, 20), "農曆除夕（補假）"),
    (date(2026, 4, 6), "民族掃墓節（補假）"),
    (date(2026, 5, 1), "勞動節"),
    (date(2026, 6, 19), "端午節"),
    (date(2026, 9, 28), "孔子誕辰紀念日"),
    (date(2026, 12, 25), "行憲紀念日"),
])
def test_public_holidays_are_not_trading_days(day, name):
    assert is_trading_day(day) is False, f"{day} 是{name}"


# --- 跨年 ---


def test_last_weekday_of_the_year_is_a_trading_day():
    """2025/12/31 是週三、不是假日 —— 跨年不該讓判定失準。"""
    assert is_trading_day(date(2025, 12, 31)) is True


def test_new_year_day_is_not_a_trading_day():
    assert is_trading_day(date(2026, 1, 1)) is False


def test_first_weekday_after_new_year_is_a_trading_day():
    assert is_trading_day(date(2026, 1, 2)) is True


# --- 臨時開市（補班日）與臨時休市（颱風假）---


def test_makeup_workday_saturday_can_be_marked_as_trading():
    """補班日是週六卻要開市。`holidays` 套件不會告訴我們，只能人工指定。"""
    saturday = date(2026, 8, 8)
    assert is_trading_day(saturday) is False
    assert is_trading_day(saturday, extra_openings={saturday}) is True


def test_typhoon_closure_overrides_an_ordinary_weekday():
    """颱風假是平日卻休市。invest-hm 踩過這個坑。"""
    weekday = date(2026, 8, 10)
    assert is_trading_day(weekday) is True
    assert is_trading_day(weekday, extra_closures={weekday}) is False


def test_closure_wins_over_opening_when_both_are_listed():
    """補班日又遇到颱風 → 休市。安全的方向優先。"""
    day = date(2026, 8, 8)
    assert is_trading_day(day, extra_openings={day}, extra_closures={day}) is False


def test_overrides_do_not_leak_between_calls():
    """一次呼叫的覆寫不可影響下一次——共用可變狀態是這類 bug 的溫床。"""
    day = date(2026, 8, 10)
    is_trading_day(day, extra_closures={day})
    assert is_trading_day(day) is True
