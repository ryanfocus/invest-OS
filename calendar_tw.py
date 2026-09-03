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
) -> bool:
    """`day` 是否為台灣交易日。

    📌 **這裡曾經有 `extra_closures` / `extra_openings` 兩個手動覆寫**
       （颱風假、補班日），2026-09-03 拿掉。

       休市那邊安全上已經不需要：颱風假沒填的話程式照樣會跑，但取到的是
       前一交易日的報價，而 `build_open_prices` 的 L0 新鮮度檢查會擋下來
       （實測 2026-08-09 週日取到的就是 08-07 的價格）。結果是一則
       「今日無訊號」的噪音，不是錯誤的交易。

       ⚠️ **開市那邊的代價不對稱，這是刻意接受的取捨**：真的有臨時開市而
       `holidays` 不知道的話，那天會被**完全靜默地跳過**——沒有訊號、
       沒有交易、沒有訊息。使用者 2026-09-03 明確選擇拿掉，理由是那兩個
       欄位從專案開始到現在一次都沒用過，而「要記得事先填」本身就是
       最容易漏掉的那種步驟。
    """
    return day.weekday() < 5 and day not in _TW_HOLIDAYS
