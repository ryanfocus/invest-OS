"""broker —— 與券商往來的唯一出入口。

這一層存在的理由是可測試性：群益的 API 是 Windows COM 元件，無法在測試環境執行。
把它隔離在這裡，策略、狀態、通知等所有其他部分都能在沒有 COM、沒有帳號的機器上驗證。

實作有兩個：
  - `broker.fake`    測試用，回放預先指定的資料並記錄收到的指令
  - `broker.capital` 群益 COM

⚠️ 任何真實 COM 的初始化都必須延遲到函式內執行，不可在模組載入時做——
   否則未註冊 COM 的機器連 import 都會失敗，測試一行都跑不了。

本模組只放**兩個實作共用的契約**：商品代碼、資料結構、就緒判定。
共用的好處是「假 broker 與真實 broker 通過同一組行為測試」不是靠自律，而是靠結構。
"""

from __future__ import annotations

from dataclasses import dataclass

# 報價訂閱用的近月連續代碼。
#
# ⚠️ **必須帶 `AM` 後綴**。群益對同一商品提供兩套代碼，不帶後綴的是全盤（含夜盤），
#    開盤價會是夜盤的開盤價而非 08:45 那筆。用錯不會報錯、數值也完全合理，
#    但每天的訊號都會靜默失準。詳見 docs/adr/0005-use-am-session-quote-codes.md。
#
# 微台用四個零（與小型電子 ZE0000、小型金融 ZF0000 同格式），不是大台那種兩個零。
TX_CODE = "TX00AM"
MTX_CODE = "MTX00AM"
TMF_CODE = "TM0000AM"
PRODUCT_CODES = (TX_CODE, MTX_CODE, TMF_CODE)


class BrokerError(Exception):
    """broker 層的錯誤基底。"""


class LoginFailed(BrokerError):
    """登入失敗。重試無用，需要人介入（密碼、憑證、聲明書等）。"""


class QuoteNotReady(BrokerError):
    """開盤價尚未就緒或看起來不對。重試可能有幫助。

    「未就緒」與「不動作」是**完全不同的兩件事**：前者是無法判斷，後者是判斷的結果。
    """


@dataclass(frozen=True)
class Quote:
    """單一商品的報價。價格已還原小數（群益回傳為整數且放大 100 倍）。

    `trading_day` 是這筆報價所屬的**交易日**（`yyyymmdd`）。它存在的理由是：
    休市時群益不會回「沒有資料」，而是繼續給你**上一個交易日**的價格，
    而且看起來完全正常（非 0、在漲跌停內）。沒有這個欄位就分不出
    「今天的開盤價」與「上次的開盤價」。
    """

    code: str
    open: float
    limit_up: float
    limit_down: float
    trading_day: int | None = None


@dataclass(frozen=True)
class OpenPrices:
    """大台、小台、微台的當日 AM 盤開盤價。"""

    tx: float
    mtx: float
    tmf: float


def build_open_prices(quotes: dict, expected_trading_day: int | None = None) -> OpenPrices:
    """驗證三檔報價後組成 `OpenPrices`，任一不合格就 raise `QuoteNotReady`。

    三件事都以**該商品自己的**資料為準——不同商品、不同盤別的漲跌停不同，
    混用會誤判（實測 AM 盤 48990／40084、全盤 48683／39833）。

      L0 新鮮度：報價所屬交易日必須等於 `expected_trading_day`
      L1 就緒：報價存在，且開盤價不為 0（0 代表當日尚未成交）
      L2 不變量：跌停 ≤ 開盤價 ≤ 漲停（含等號，漲停鎖死是合法開盤價）

    L0 是最重要的一層，因為它擋的是**唯一一種看起來完全正常的錯誤**：
    休市或報價尚未換日時，群益會給上一個交易日的價格——非 0、在漲跌停內、
    數值合理，L1 與 L2 全部會放行。實測 2026-08-09（週日）取到的就是 08-07 的價格。

    `expected_trading_day` 為 None 時跳過 L0（供不在乎日期的情境使用）。

    問題會一次列完，不是遇到第一個就停——排查時才不必修一個發現還有下一個。
    """
    problems = []
    for code in PRODUCT_CODES:
        quote = quotes.get(code)
        if quote is None:
            problems.append(f"{code} 沒有報價")
            continue
        if expected_trading_day is not None and quote.trading_day != expected_trading_day:
            problems.append(
                f"{code} 的報價屬於交易日 {quote.trading_day}，"
                f"不是預期的 {expected_trading_day}（拿到舊資料）"
            )
            continue
        if quote.open <= 0:
            problems.append(f"{code} 開盤價為 {quote.open}（尚未成交）")
            continue
        if not (quote.limit_down <= quote.open <= quote.limit_up):
            problems.append(
                f"{code} 開盤價 {quote.open:.0f} 不在漲跌停區間內"
                f"（{quote.limit_down:.0f}~{quote.limit_up:.0f}）"
            )

    if problems:
        raise QuoteNotReady("；".join(problems))

    return OpenPrices(
        tx=quotes[TX_CODE].open,
        mtx=quotes[MTX_CODE].open,
        tmf=quotes[TMF_CODE].open,
    )
