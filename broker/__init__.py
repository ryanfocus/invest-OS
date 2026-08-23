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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 只給型別檢查器看。`broker` 不在執行期依賴 `state`——方向必須是
    # 上層依賴下層，反過來會讓假 broker 也得認識狀態檔。
    from state import PositionRecord

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


class ProductListUnavailable(BrokerError):
    """商品清單查不到、或缺少我們要交易的商品。重試可能有幫助。"""


class OrderFailed(BrokerError):
    """委託沒有送出去（被拒絕、連線斷、參數錯）。**確定沒有部位產生。**

    ⚠️ 分不清楚有沒有部位時，用 `FillUnknown`，不要用這個。
    """


class FillUnknown(BrokerError):
    """**委託已經送出，但不知道成交幾口。** 回報沒回來，或認不出是哪一筆。

    與 `OrderFailed` 的差別是這個系統最貴的一條分界線：

      OrderFailed  → 確定沒有部位 → 下午什麼都不用做
      FillUnknown  → **可能已經成交** → 下午絕不可以自動送單（可能加倉、
                     也可能開出反向新倉），要留下記錄並叫人去看帳戶

    當成 `OrderFailed` 處理的話不會寫狀態檔，13:40 那班就不會去平——
    部位直接進夜盤而使用者不知情。

    `order_seq` 與 `known_filled` 帶著給人用：委託序號讓使用者能在券商 APP
    直接查到那一筆，已知成交口數告訴他對帳時**至少**要看到幾口。
    只有一句錯誤訊息的話，他得自己從幾百筆委託裡找。

    `record` 是**呼叫端掛上去的**，不是 broker 填的：broker 不知道狀態檔長什麼樣。
    進場那段在寫完狀態檔之後把記錄掛在例外上帶出去（見 `main._place_entry_order`），
    這樣通知那一段就不必在 except 區段裡重新讀檔——那次讀本身可能再拋一個例外，
    於是整個例外逃出 `run_entry` 而一則通知都不發。

    宣告在這裡是因為**有人依賴它**（`main.py` 讀 `exc.record` 去組訊息）。
    靠動態賦值的話，那個依賴不在任何一處介面上，改壞了也沒有東西會講。
    """

    def __init__(self, message: str, *, order_seq: str = "", known_filled: int = 0,
                 record: PositionRecord | None = None):
        super().__init__(message)
        self.order_seq = order_seq
        self.known_filled = known_filled
        self.record = record


# 委託買賣別。刻意不用群益的 0/1：那兩個數字在程式碼裡看不出誰是誰，
# 而寫反的後果是開出完全相反的部位。轉成群益格式的工作留在 broker.capital。
BUY = "BUY"
SELL = "SELL"

# 委託意圖：這筆單是要**開**部位還是**平**部位。
#
# 它決定群益的倉別參數（`sNewClose`），而那個參數兩邊的正確值不一樣——
# 進場與出場是**兩個獨立的未知數**，進場驗過不代表出場的填法就對。
# 對應規則與理由見 `broker.capital.build_future_order_fields`。
ENTRY = "ENTRY"
EXIT = "EXIT"


def to_yyyymmdd(day) -> int:
    """`date` → `20260810`。群益的日期欄位都是這個整數格式。"""
    return int(day.strftime("%Y%m%d"))


def format_yyyymmdd(day: int, sep: str = "/") -> str:
    """`20260810` → `2026/08/10`。給人看的、以及期交所查詢參數用的格式。

    收在這裡而不是各處自己拆位數：那串
    `f"{d // 10000:04d}/{d // 100 % 100:02d}/{d % 100:02d}"` 原本逐字重複在
    `taifex` 與 Discord 訊息裡，而寫錯的後果是**查錯日期的資料**——
    然後拿去跟另一天的觀測比對，報一個看起來很真實的不一致。
    """
    return f"{day // 10000:04d}{sep}{day // 100 % 100:02d}{sep}{day % 100:02d}"


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


@dataclass(frozen=True)
class ContractInfo:
    """近月合約資訊，來自**商品清單查詢**。

    ⚠️ 刻意不由日期推算。「每月第三個週三」這條算式在最後交易日遇假日順延時會失準，
    而結算日算錯的後果很直接：出場該提前到 13:30 卻用了 13:40，
    那時合約已停止交易，單送不出去（見 ADR-0003 與 SPEC 的結算日章節）。
    """

    code: str                  # 報價代碼（近月連續，如 TX00AM）
    last_trading_day: int      # yyyymmdd
    # 下單代碼（指名月份，如 TX08）。**與報價代碼是兩回事**：群益官方文件在
    # SendFutureOrderCLR 的備註寫明委託要帶月份代碼，而近月連續代碼能不能下單
    # 沒有任何文件保證。三個商品的格式還互不相同（TX08／MTX08／TM2608），
    # 所以只能從商品清單讀出來，不可以自己組字串。
    order_code: str = ""

    @property
    def contract_month(self) -> str:
        """契約年月（`yyyymm`）。最後交易日必定落在契約月份內，取前六碼即可。"""
        return str(self.last_trading_day)[:6]

    def is_settlement_day(self, day) -> bool:
        """`day` 是不是這個合約的最後交易日（＝結算日）。"""
        return self.last_trading_day == to_yyyymmdd(day)

    def has_expired(self, day) -> bool:
        """最後交易日是不是已經過了。**結算日當天不算過期**——那天 13:30 才停止交易。

        存在的理由是 `order_code` 的形式（`MTX08`，商品代號＋月份兩碼）：
        群益在該月已過期時會**自動改送隔年同月且不報錯**，於是 2026 年 9 月送
        `MTX08` 成交的會是 2027 年 8 月的合約——完全不同的東西，而且悄無聲息。
        """
        return self.last_trading_day < to_yyyymmdd(day)




@dataclass(frozen=True)
class OrderRequest:
    """一筆進場或出場委託。

    `contract_month` 一定要填。群益的商品代碼帶月份時，若該月已過期會**自動改送
    隔年同月**（官方文件明載於 SendFutureOrderCLR 的備註），指名年月才擋得住。
    年月的唯一可信來源是商品清單（見 ticket 03），不是由日期推算。
    """

    product: str            # 報價代碼（PRODUCT_CODES 之一），狀態檔與對帳用
    order_code: str         # 下單代碼（TX08 等），送給券商的就是這個
    contract_month: str     # yyyymm
    side: str               # BUY / SELL
    lots: int
    # ENTRY / EXIT —— 決定倉別，而兩邊的正確值不同。
    # ⚠️ 刻意**沒有預設值**：忘了填就靜默變成新倉，而那正是
    #    「平倉單被當成新倉，部位不減反增」那個失效模式。
    intent: str

    def __post_init__(self) -> None:
        """空的下單代碼絕不可以送到券商。

        `ContractInfo.order_code` 有預設空字串（不在乎下單的情境用得到），
        所以空值有可能一路流到這裡。在委託物件這一層擋住——
        送出去只會拿到一個看不出原因的拒絕訊息。
        """
        if not self.order_code:
            raise ValueError(f"{self.product} 沒有下單代碼，無法送出委託")
        if self.lots < 1:
            raise ValueError(f"委託口數必須 ≥ 1，目前是 {self.lots}")
        if self.intent not in (ENTRY, EXIT):
            raise ValueError(f"intent 必須是 {ENTRY} 或 {EXIT}，目前是 {self.intent!r}")


@dataclass(frozen=True)
class OrderResult:
    """委託結果。

    `filled_lots` 是**實際成交口數**，不是委託口數——市價 IOC 可能部分成交，
    而下午出場要平的是實際持有的量。兩者混用會平錯口數。
    """

    filled_lots: int
    order_seq: str = ""     # 群益的 13 碼委託序號，供對帳與人工查詢


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
