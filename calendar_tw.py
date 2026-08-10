"""台灣交易日判定。

移植自 invest-hm 的同名模組，但多了**臨時開市／休市的覆寫**——
`holidays` 套件只知道國定假日，不知道補班日（週六卻開市）與颱風假（平日卻休市），
而這兩種在台灣都真實發生過。invest-hm 就在颱風假上踩過。

⚠️ **這裡沒有結算日判定。** invest-hm 用「每月第三個週三」的日期算式，
   本專案刻意不沿用——結算日改由商品清單查詢回來的**最後交易日**決定。
   理由見 ticket 03：最後交易日遇假日會順延，日期算式抓不到。
"""

from __future__ import annotations

from datetime import date

import holidays

_TW_HOLIDAYS = holidays.country_holidays("TW")


def is_trading_day(
    day: date,
    *,
    extra_closures: frozenset | set = frozenset(),
    extra_openings: frozenset | set = frozenset(),
) -> bool:
    """`day` 是否為台灣交易日。

    `extra_closures` 臨時休市（颱風假等），`extra_openings` 臨時開市（補班日等）。
    兩者同時列出時**休市優先**——安全的方向優先，寧可少做一天也不要拿假資料下單。
    """
    if day in extra_closures:
        return False
    if day in extra_openings:
        return True
    return day.weekday() < 5 and day not in _TW_HOLIDAYS
