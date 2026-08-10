"""交易日判定。

非交易日執行時程式要完全靜默——不登入、不發訊息。判斷錯了有兩種後果：
把交易日當成假日 → 整天沒訊號；把假日當成交易日 → 拿到舊資料算出假訊號。

⚠️ **關於期望值的來源**：只斷言**固定日期的法定假日**（1/1、5/1、12/25），
那些的日期與假日身分都是獨立可知的常識。農曆假日（春節、端午、中秋）的
西曆日期我只能從 `holidays` 套件查到，拿它當期望值就是用程式碼自己的來源
驗證程式碼——同義反覆，不會失敗也不證明任何事，所以不寫。

代價是農曆假日沒有測試覆蓋。真正要守住它，期望值得來自期交所的官方休市日曆，
那是另一件事（可考慮併入 ticket 07 的對帳）。
"""

from datetime import date

import pytest

from calendar_tw import is_trading_day


# --- 一般日 ---


@pytest.mark.parametrize("day", [
    date(2026, 8, 10),
    date(2026, 8, 11),
    date(2026, 8, 12),
    date(2026, 8, 13),
    date(2026, 8, 14),
])
def test_ordinary_weekdays_are_trading_days(day):
    assert is_trading_day(day) is True


@pytest.mark.parametrize("day", [date(2026, 8, 8), date(2026, 8, 9)])
def test_weekends_are_not_trading_days(day):
    assert is_trading_day(day) is False


# --- 國定假日 ---


@pytest.mark.parametrize("day, name", [
    (date(2026, 1, 1), "中華民國開國紀念日"),
    (date(2026, 5, 1), "勞動節"),
    (date(2026, 12, 25), "行憲紀念日"),
])
def test_fixed_date_statutory_holidays_are_not_trading_days(day, name):
    """只用固定日期的法定假日——日期與假日身分都不必查套件就知道。"""
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
