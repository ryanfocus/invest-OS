"""期交所公開資料 —— 對帳唯一的第三方真相。

免登入、免金鑰，任何人都拿得到同一份數字。這正是它的價值：
群益的報價與策略的判斷都在同一條管線上，管線本身錯了兩邊會一起錯；
期交所是這條管線之外的來源。

⚠️ 這份實作原本住在 `tools/compare_open.py`，2026-08-18 搬過來讓正式程式
（ticket 07 對帳）與那支工具共用。CSV 的欄位位置與「一般／盤後」的篩選
很容易寫錯，兩份各自維護遲早會漂移，而漂移的那一份會**安靜地**對錯東西。
"""

from __future__ import annotations

import csv
import io
import logging

import requests

from broker import format_yyyymmdd

logger = logging.getLogger(__name__)

TAIFEX_CSV = "https://www.taifex.com.tw/cht/3/dlFutDataDown"

# 期交所商品代號 → 本專案內部用的鍵。
# 內部鍵沿用 `OpenPrices` 與 `Observation` 的欄位名，讓對帳不必再做一次轉換。
COMMODITIES = {"TX": "tx", "MTX": "mtx", "TMF": "tmf"}


def fetch_ohlc(commodity: str, date_str: str, *, timeout=(10, 60)) -> dict | None:
    """取某商品某日「**一般交易時段**」近月合約的 OHLC。查無資料回 `None`。

    `commodity` 用期交所的商品代號：TX（大台）、MTX（小台）、TMF（微台）。
    `date_str` 格式 `YYYY/MM/DD`。
    """
    resp = requests.post(
        TAIFEX_CSV,
        headers={"User-Agent": "Mozilla/5.0"},
        data={
            "down_type": "1",
            "commodity_id": commodity,
            "commodity_id2": "",
            "queryStartDate": date_str,
            "queryEndDate": date_str,
        },
        timeout=timeout,
    )
    text = resp.content.decode("ms950", errors="replace")

    # ⚠️ 查詢範圍超過一個月時，期交所會回 HTML 錯誤頁但 HTTP 仍然是 200。
    #    必須驗內容，只看狀態碼會把錯誤頁當成空資料。
    if not text.startswith("交易日期"):
        return None

    candidates = []
    for row in csv.reader(io.StringIO(text)):
        # 欄位：0 交易日期 1 契約 2 到期月份 3 開盤 4 最高 5 最低 6 收盤 … 17 交易時段
        if len(row) < 18 or row[1].strip() != commodity:
            continue
        month = row[2].strip()
        # ⚠️ 兩個篩選都不可少：`isdigit()` 排除週別合約（如 `202608W3`），
        #    「一般」排除盤後（夜盤）時段——那正是本策略最怕拿錯的那個盤別。
        if not month.isdigit() or row[17].strip() != "一般":
            continue
        candidates.append((month, row))

    if not candidates:
        return None
    candidates.sort()        # 月份最小的就是近月
    month, row = candidates[0]

    def num(value):
        return float(value.strip().replace(",", ""))

    return {
        "date": row[0].strip(),      # 交易日期。呼叫端要拿它驗「回的是不是那一天」
        "month": month,
        "open": num(row[3]),
        "high": num(row[4]),
        "low": num(row[5]),
        "close": num(row[6]),
        "volume": num(row[9]),
    }


def fetch_official_opens(trading_day: int) -> dict[str, float] | None:
    """取某交易日三個商品的官方開盤價，供對帳使用。

    回 `{"tx": ..., "mtx": ..., "tmf": ...}`，**任一商品拿不到就整份回 `None`**。

    為什麼不回傳部分結果：拿到兩個商品時無從判斷是「期交所還沒公布完」
    還是「格式變了」。前者該安靜跳過，後者該修程式——而只比兩個商品
    會讓後者看起來像前者。缺就整份跳過，下一個交易日再對。

    ⚠️ 三個商品各發一次 HTTP 請求。呼叫端必須確保這件事跑在
    **當日訊號、通知、下單全部完成之後**——它可能花上好幾十秒。
    """
    date_str = format_yyyymmdd(trading_day)
    opens = {}
    for commodity, key in COMMODITIES.items():
        data = fetch_ohlc(commodity, date_str)
        if data is None:
            logger.info("期交所查無 %s 在 %s 的一般時段資料", commodity, date_str)
            return None
        if data["date"] != date_str:
            # ⚠️ 查詢參數說要哪一天，回傳列也要**真的是那一天**。
            #    只信參數的話，期交所改行為（回最近一天、忽略範圍）時我們會
            #    拿別天的資料去跟這天的觀測比對——報出一個看起來很真實的不一致，
            #    而人會去查一個根本沒問題的日子。
            logger.error("期交所回的是 %s 的資料，要的是 %s", data["date"], date_str)
            return None
        opens[key] = data["open"]
    return opens
