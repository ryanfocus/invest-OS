"""取大台開盤價，兩個來源並列比對。

用法：
    python tools/compare_open.py              # 今天
    python tools/compare_open.py 2026/08/10   # 指定日期（只影響期交所那邊）

存在的理由：2026-08-10 實測發現群益與期交所的大台開盤價差 2 點
（44987 vs 44985），但高、低、收完全一致，小台微台也完全一致。
原因未明，需要累積幾天的樣本才判斷得出是常態還是偶發。
把兩邊並排印出來，每天跑一次就能累積證據。

⚠️ 群益的報價**只有當天**，指定日期只會改變期交所那一側。
"""

from __future__ import annotations

import csv
import datetime
import io
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TAIFEX_CSV = "https://www.taifex.com.tw/cht/3/dlFutDataDown"


# ─────────────────────────────────────────────────────────────
# 來源一：期交所（公開、免登入）
# ─────────────────────────────────────────────────────────────


def taifex_open(commodity: str, date_str: str) -> dict | None:
    """取期交所某商品某日「一般交易時段」近月合約的 OHLC。

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
        timeout=(10, 60),
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
        if not month.isdigit() or row[17].strip() != "一般":
            continue        # 排除週別合約與盤後（夜盤）時段
        candidates.append((month, row))

    if not candidates:
        return None
    candidates.sort()        # 月份最小的就是近月
    month, row = candidates[0]

    def num(value):
        return float(value.strip().replace(",", ""))

    return {
        "month": month,
        "open": num(row[3]),
        "high": num(row[4]),
        "low": num(row[5]),
        "close": num(row[6]),
        "volume": num(row[9]),
    }


# ─────────────────────────────────────────────────────────────
# 來源二：群益 COM（需登入）
# ─────────────────────────────────────────────────────────────


def capital_first_real_tick(broker, code: str, seconds: float = 20.0) -> dict | None:
    """取當日**第一筆非模擬成交** —— 那就是開盤價的定義。

    ⚠️ 必須用 `nSimulate` 過濾。08:30 起就有成交明細，但那是**試撮**
    （開盤前的模擬撮合）。2026-08-10 實測：最早的試撮價 44981，
    真正的開盤價 44985 在 08:45:00。天真地取第一筆會拿到 44981，錯得更多。

    這個做法的價值不只是正確，而是**帶時間戳可以自我驗證**：
    時間必須是 084500，那是 `nOpen` 欄位給不了的保證。
    """
    import comtypes.client

    collected: list = []

    class _TickEvents:
        def OnNotifyHistoryTicksLONG(self, sMarketNo, nIndex, nPtr, nDate, nTimehms,
                                     nTimemillismicros, nBid, nAsk, nClose, nQty, nSimulate):
            collected.append((nPtr, nDate, nTimehms, nClose / 100.0, nSimulate))

        def OnNotifyTicksLONG(self, *args):
            pass

        def OnConnection(self, *args):
            pass

        def OnNotifyQuoteLONG(self, *args):
            pass

    handler = comtypes.client.GetEvents(broker._quote, _TickEvents())  # noqa: F841
    page_no = 0
    page_no, ret = broker._quote.SKQuoteLib_RequestTicks(page_no, code)
    if ret != 0:
        return None
    broker._pump_for(seconds)

    real = sorted(t for t in collected if not t[4])
    if not real:
        return None
    ptr, date, hhmmss, price, _ = real[0]
    return {"price": price, "time": hhmmss, "date": date,
            "total_ticks": len(collected), "simulated": len(collected) - len(real)}


def _connected_broker():
    """登入並連上報價主機，回傳可直接用的 broker。"""
    import settings
    from broker.capital import CapitalBroker

    broker = CapitalBroker(
        settings.read_env("CAPITAL_USER_ID"),
        settings.read_env("CAPITAL_PASSWORD"),
    )
    broker.login()
    broker._ensure_quote_connection()
    return broker


def capital_quotes(broker=None) -> dict:
    """取群益的三商品即時報價。回傳 {代碼: 欄位字典}。

    這裡直接用專案的 CapitalBroker，因為登入順序有一堆踩過坑的細節
    （絕對路徑載入型別庫、公告與聲明書事件必須在登入前註冊、
    用 LoginSetQuote 啟用報價、等 STOCKS_READY、先訂閱才有即時欄位）。
    細節見 broker/capital.py 的模組說明。
    """
    from broker import PRODUCT_CODES

    broker = broker or _connected_broker()
    page_no = 0
    page_no, code = broker._quote.SKQuoteLib_RequestStocks(page_no, ",".join(PRODUCT_CODES))
    if code != 0:
        raise RuntimeError(f"訂閱報價失敗，代碼 {code}")
    broker._pump_until(lambda: broker._quote_events.quote_updates > 0, 15.0)
    broker._pump_for(3.0)

    result = {}
    for stock_no in PRODUCT_CODES:
        obj = broker._sk.SKSTOCKLONG()
        obj, ret = broker._quote.SKQuoteLib_GetStockByNoLONG(stock_no, obj)
        if ret != 0:
            continue
        # 群益的價格是整數且放大 100 倍
        result[stock_no] = {
            "trading_day": obj.nTradingDay,
            "open": obj.nOpen / 100.0,
            "high": obj.nHigh / 100.0,
            "low": obj.nLow / 100.0,
            "close": obj.nClose / 100.0,
        }
    return result


def main() -> int:
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime("%Y/%m/%d")
    pairs = [("TX00AM", "TX", "大台"), ("MTX00AM", "MTX", "小台"), ("TM0000AM", "TMF", "微台")]

    print(f"現在時間：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"期交所查詢日期：{date_str}（群益一律是當天的即時報價）\n")

    broker = _connected_broker()
    capital = capital_quotes(broker)

    # ── 三個來源的開盤價並列 ──
    # 2026-08-10 實測：大台的 nOpen 給 44987，但 APP、期交所、第一筆真實成交
    # 都是 44985。第一筆成交帶時間戳（084500），是唯一能自我驗證的來源。
    print("═══ 開盤價：三個來源並列 ═══\n")
    print(f"{'商品':<6}{'nOpen 欄位':>12}{'第一筆真實成交':>16}{'期交所':>10}   一致？")
    print("-" * 60)
    for capital_code, taifex_id, label in pairs:
        mine = capital.get(capital_code)
        tick = capital_first_real_tick(broker, capital_code)
        theirs = taifex_open(taifex_id, date_str)

        n_open = mine["open"] if mine else None
        t_price = tick["price"] if tick else None
        x_open = theirs["open"] if theirs else None
        values = [v for v in (n_open, t_price, x_open) if v is not None]
        agree = "✅" if len(values) >= 2 and max(values) - min(values) < 0.5 else "❌ 不一致"

        def fmt(v):
            return f"{v:.0f}" if v is not None else "—"

        tick_cell = f"{fmt(t_price)} @{tick['time']}" if tick else "—"
        print(f"{label:<6}{fmt(n_open):>12}{tick_cell:>16}{fmt(x_open):>10}   {agree}")

    # ── 其他欄位（用來確認不是抓錯商品）──
    print("\n═══ 其他欄位（對得上就代表商品沒抓錯）═══\n")
    print(f"{'商品':<6}{'欄位':<6}{'群益':>10}{'期交所':>10}")
    print("-" * 34)
    for capital_code, taifex_id, label in pairs:
        mine = capital.get(capital_code)
        theirs = taifex_open(taifex_id, date_str)
        if mine is None or theirs is None:
            print(f"{label:<6}資料不全（群益={mine is not None} 期交所={theirs is not None}）")
            continue
        for field, name in (("high", "最高"), ("low", "最低"), ("close", "收盤")):
            diff = mine[field] - theirs[field]
            flag = "" if abs(diff) < 0.5 else f"  ← 差 {diff:+.0f}"
            print(f"{label if field == 'high' else '':<6}{name:<6}"
                  f"{mine[field]:>10.0f}{theirs[field]:>10.0f}{flag}")
        print(f"{'':<6}{'交易日':<6}{mine['trading_day']:>10}{theirs['month']:>10}（合約月份）")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
