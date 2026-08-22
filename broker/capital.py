r"""群益策略王 COM 元件的 broker 實作。

⚠️ **本模組在載入時不碰 COM。** 所有 `comtypes` 的 import 與物件建立都在函式內，
   否則未註冊 COM 的機器連 `import` 都會失敗，測試一行都跑不了（見 tests/test_capital_broker.py）。
   群益官方範例在模組層級就建立物件，**不要照抄那部分**。

這裡的每一段幾乎都是踩過坑換來的，順序不能隨意調動：

  1. `GetModule` 必須用**絕對路徑**。用相對檔名 `"SKCOM.dll"` 會拿到
     `TYPE_E_CANTLOADLIBRARY (0x80029C4A)`，因為 DLL 不在執行目錄。
     路徑從註冊表 `CLSID\...\InprocServer32` 讀，不寫死 `C:\SKCOM`。
  2. **註冊公告事件（`OnReplyMessage`）必須在登入之前**，官方明訂。
  3. **`OnShowAgreement` 也必須註冊**，否則登入後查不到聲明書狀態，
     `EnterMonitorLONG` 會回 2018——訊息說「請先簽署」，但真正的原因是
     官方列的第三種「無法取得聲明書狀態」。
  4. 登入用 `SKCenterLib_LoginSetQuote(id, pw, "Y")`，第三個參數明確啟用報價元件。
  5. `EnterMonitorLONG` 之後要等 `OnConnection` 回 `STOCKS_READY`，
     商品資料沒下載完就查詢會拿不到即時欄位。
  6. **先 `RequestStocks` 訂閱才有即時報價**。未訂閱時 `GetStockByNoLONG`
     只回得到商品名稱、昨收這類靜態欄位——開盤價會是 0，看起來像「尚未成交」。
"""

from __future__ import annotations

import logging
import os
import time
import winreg
from dataclasses import dataclass

from broker import (
    BUY,
    ENTRY,
    EXIT,
    FillUnknown,
    PRODUCT_CODES,
    ContractInfo,
    LoginFailed,
    OpenPrices,
    OrderFailed,
    OrderRequest,
    OrderResult,
    ProductListUnavailable,
    Quote,
    QuoteNotReady,
    build_open_prices,
)

logger = logging.getLogger(__name__)

# SKCenterLib 的 CLSID，用來從註冊表反查 SKCOM.dll 的實際位置
_SKCENTER_CLSID = "{AC30BAB5-194A-4515-A8D3-6260749F8577}"

# 報價主機的連線狀態代碼：收到它才代表商品資料下載完成
_STOCKS_READY = 3003

# 商品清單的市場別：0 上市、1 上櫃、2 期貨、3 選擇權
_MARKET_FUTURES = 2

# 群益的價格是整數且放大 100 倍
_PRICE_SCALE = 100.0

# 連線環境（SKCenterLib_SetAuthority）。範例程式把 flag 標成
# 0 正式／1 正式SGX／2 測試／3 測試SGX，但**官方文件的說明是**：
#
#   「(SGX 專線only)手動設定特殊功能屬性開啟或關閉。
#     SGX 專線屬性：關閉／開啟：0／1
#     Bit 1為環境設定：預設0，代表正式環境
#     一般客戶可忽略此部分」
#
# ⚠️ **所以這不是一般台指期帳戶的模擬環境。** 它屬於 SGX 專線（需另向營業員申請）。
#    設成 test 很可能只是無效，而「以為切到測試環境所以很安全」比不切更危險。
#    在向營業員確認之前，不可以把它當成防護。
_AUTHORITY_FLAGS = {"production": 0, "test": 2}

# FUTUREORDER 的欄位值（官方文件 5 章的結構定義）
_TRADE_TYPE_IOC = 1        # 0:ROD 1:IOC 2:FOK
_BUY, _SELL = 0, 1         # sBuySell
_DAY_TRADE_NO = 0          # 不標記當沖
_SESSION_INTRADAY = 0      # sReserved：0 盤中（T盤及T+1盤）、1 T盤預約
_MARKET_PRICE = "M"        # bstrPrice：「M」市價、「P」範圍市價；限 IOC/FOK

# 倉別 sNewClose：0 新倉、1 平倉、2 自動。**進場與出場的正確值不一樣。**
#
# 進場 = 0（新倉）。⚠️ 尚未實機驗證（ticket 04 的實機里程碑）。台指期同帳號同商品
#   同月份是淨額計算，已有反向部位時「新倉」可能被拒；若實測如此改用 2。
#
# 出場 = 2（自動）。⚠️ **這是兩個都有缺點的選項之間的判斷，不是已證明的結論。**
#
#   選「自動」的理由：SPEC 部位隔離那張表（使用者固定持有 +1、OS 做空 3 口）裡，
#     13:40 買回 3 口時帳上只有 2 口空單。「平倉」的語意是「平掉 N 口」，
#     對 N 大於現有部位是做不到的。
#   選「自動」的代價：OS 的部位若已不在（使用者自己手動平掉），「自動」會安靜地
#     開出一個反向新倉——而那正是 ticket 06 開頭警告的失效模式。「平倉」會被拒，
#     失敗得比較大聲。
#
# ⚠️ **而且這個論證對進場同樣成立**：08:50 賣 3 口時帳上是 +1，那筆也跨越零，
#    但進場用的是 0（新倉）。兩邊不一致是已知的、尚未解決的問題——
#    真正的答案只能靠 ticket 04 的實機里程碑（進出各驗一次，同一天）。在那之前不可開啟自動下單。
_NEW_CLOSE_BY_INTENT = {ENTRY: 0, EXIT: 2}

# OnNewData 的欄位位置。**2026-08-17 實機驗證通過**——監聽整個交易日收到兩則
# 真實回報（1 口微台的委託 + 成交），四個位置全部正確。
# 去識別化的樣本存在 tests/fixtures/onnewdata-real-2026-08-17.txt，
# tests/test_reply_parsing.py 對著它斷言。
_REPLY_KEYNO, _REPLY_MARKET, _REPLY_TYPE, _REPLY_ERR, _REPLY_QTY = 0, 1, 2, 3, 20
_MARKET_FUTURES_REPLY = "TF"
_REPLY_TYPES = frozenset("NCUPDBS")     # N委託 C取消 U改量 P改價 D成交 B改價改量 S動態退單
_REPLY_FILLED = "D"        # 成交
_REPLY_CANCELLED = "C"     # 取消。IOC 的剩餘量會走這裡，收到它才代表委託真的結束

# ── 兩個查詢函式的回傳格式（2026-08-19 實機取得，見
#    tests/fixtures/reports-2026-08-19.txt）。與 OnNewData 是**三種不同的格式**。
#
# ⚠️ **委託序號只在 GetOrderReport 裡，GetFulfillReport 沒有。**
#    所以後備管道要兩段：先用序號換出「委託書號」，再拿它去比對成交。
#    串得起兩份的只有委託書號。
_ORDER_MARKET, _ORDER_BOOK_NO, _ORDER_SEQ, _ORDER_DAY = 0, 7, 8, 11
_FILL_MARKET, _FILL_BOOK_NO, _FILL_DAY = 0, 7, 9
_FILL_PRODUCT, _FILL_PRICE, _FILL_QTY = 12, 25, 26

# 查詢的兩種非答案。**必須分開處理**：查無資料是正常的（單還沒成交、
# 或那天沒交易），查詢錯誤是故障。兩者都不可以當成「確定沒成交」。
# 官方文件：「限制每次查詢間需間隔五秒」。多留 0.5 秒緩衝。
_QUERY_INTERVAL_SECONDS = 5.5

_QUERY_NO_DATA = "M003"
_QUERY_ERROR = "M999"

__all__ = ["CapitalBroker"]


def parse_product_list(raw: str) -> dict:
    """解析群益商品清單，取出我們交易的三個商品。

    這是**群益的線路格式**，所以住在這裡而不是 broker 套件的共用契約層——
    假 broker 直接回傳 ContractInfo，不需要解析任何東西。

    格式：以 `;` 分隔的 `商品代碼,名稱,最後交易日,交易所代碼`，整串還會夾雜
    `%類別碼%類別名%` 的分類標頭——不剝掉的話第一筆代碼會變成
    `%201%期指數%TX00AM` 而永遠對不上。標頭可能出現在中段，不只開頭。

    殘缺的列直接跳過（清單裡混雜殘列很正常）；但三個目標商品缺任一個就 raise，
    因為訊號需要三個價才算得出來，帶著半套資料往下走只會在更遠的地方壞掉。
    """
    entries = {}
    for entry in raw.replace("\n", ";").split(";"):
        if entry.startswith("%"):
            entry = entry.rsplit("%", 1)[-1]
        fields = entry.split(",")
        if len(fields) < 3:
            continue
        code, last_day = fields[0].strip(), fields[2].strip()
        if last_day.isdigit():
            entries[code] = int(last_day)

    contracts = {}
    for code in PRODUCT_CODES:
        if code not in entries:
            continue
        last_day = entries[code]
        contracts[code] = ContractInfo(
            code=code,
            last_trading_day=last_day,
            order_code=_find_order_code(code, last_day, entries),
        )

    missing = [c for c in PRODUCT_CODES if c not in contracts]
    if missing:
        raise ProductListUnavailable(f"商品清單缺少：{', '.join(missing)}")
    return contracts


def _find_order_code(quote_code: str, last_day: int, entries: dict) -> str:
    """從清單中找出對應的**下單代碼**（指名月份的那個）。

    報價用 `TX00AM`，下單要用 `TX08`。配對條件是「同商品前綴 + 同最後交易日」：
    清單裡同時有 TX08（20260819）與 TX09（20260916），用最後交易日才分得開。

    ⚠️ 刻意不自己組字串。三個商品的月份寫法互不相同——大台 `TX08`、
    小台 `MTX08`、微台 `TM2608`（多了年份兩碼）——任何「規則」都會在微台上錯。

    找不到就 raise。退而求其次用近月連續代碼是不行的：那個代碼能不能下單
    沒有文件保證，猜錯的後果是委託被拒、或下到完全不同的商品上。
    """
    prefix = quote_code[:-2].rstrip("0")      # TX00AM → TX00 → TX
    candidates = [
        code for code, day in entries.items()
        if day == last_day
        and code != quote_code
        and code.endswith("AM")
        and code.startswith(prefix)
        # 前綴之後必須全是數字，否則 TX 會撈到選擇權之類的其他商品
        and code[len(prefix):-2].isdigit()
    ]
    if not candidates:
        raise ProductListUnavailable(
            f"商品清單找不到 {quote_code}（最後交易日 {last_day}）對應的下單代碼"
        )
    # 同前綴同最後交易日理應只有一個；真有多個時取最短的，
    # 那是「商品代碼＋月份」最基本的形式。
    order_code = min(candidates, key=len)
    return order_code[:-2]                    # 去掉 AM 後綴，下單不用盤別代碼


def build_future_order_fields(request: OrderRequest, account: str) -> dict:
    """把一筆 `OrderRequest` 轉成 `FUTUREORDER` 的欄位值。

    抽成純函式是為了**讓委託參數可被測試**。這些欄位寫錯的後果各不相同但都很貴：
    當沖標錯會被課不同稅率也可能被拒、時效寫成 ROD 會掛單整天、
    買賣別寫反會開出完全相反的部位。留在 COM 呼叫裡的話，沒有 Windows
    與帳號就一行都驗不到。

    參數依據 ADR-0003（市價、IOC、不標當沖）與官方文件的 FUTUREORDER 結構定義。
    """
    return {
        "bstrFullAccount": account,
        # 下單代碼（TX08），不是報價代碼（TX00AM）
        "bstrStockNo": request.order_code,
        "sBuySell": _BUY if request.side == BUY else _SELL,
        # 市價在群益是 bstrPrice="M"，且官方註明只能配 IOC 或 FOK
        "bstrPrice": _MARKET_PRICE,
        "sTradeType": _TRADE_TYPE_IOC,
        "nQty": request.lots,
        "sDayTrade": _DAY_TRADE_NO,
        "sNewClose": _NEW_CLOSE_BY_INTENT[request.intent],
        "sReserved": _SESSION_INTRADAY,
    }


def _resolve_dll_path() -> str:
    """從註冊表取得 SKCOM.dll 的絕對路徑。"""
    key_path = rf"CLSID\{_SKCENTER_CLSID}\InprocServer32"
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
            return winreg.QueryValueEx(key, "")[0]
    except FileNotFoundError as exc:
        raise LoginFailed(
            "SKCOM 元件尚未註冊。請依 docs/LOGIN_SETUP.md Step 4，"
            "以系統管理員身分執行 C:\\SKCOM\\install.bat"
        ) from exc


class _ReplyEvents:
    """公告與委託回報的接收端。官方要求登入前註冊，且 OnReplyMessage 必須回傳 -1。

    **也負責記錄回報連線的真實狀態。** `SKReplyLib_ConnectByID` 回 0
    只代表「請求已受理」——官方文件把 `OnConnect` / `OnComplete` 列為它的
    通知事件，連線結果是**非同步**回來的。

    連線若靜默失敗，程式會照常送單、然後永遠等不到 `OnNewData`
    → 每一天都變成「不確定」→ 每天都要人介入。那正是 ticket 09 要消滅的處境。
    """

    def __init__(self) -> None:
        self.rows: list = []
        # `None` = 還不知道（事件還沒回來）。**預設不可以是 True**——
        # 樂觀的預設值等於沒有這個檢查。
        self.connected: bool | None = None
        self.connect_error = ""

    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1

    def OnNewData(self, bstrUserID, bstrData):
        self.rows.append(bstrData)

    def OnConnect(self, bstrUserID, nErrorCode):
        self.connected = nErrorCode == 0
        if not self.connected:
            self.connect_error = f"回報連線失敗，代碼 {nErrorCode}"
            logger.error("%s", self.connect_error)
        else:
            logger.info("回報連線已建立")

    def OnDisconnect(self, bstrUserID, nErrorCode):
        # 中途斷線與「從未連上」的後果一樣：收不到推播。
        self.connected = False
        self.connect_error = f"回報連線中斷，代碼 {nErrorCode}"
        logger.error("%s", self.connect_error)

    def OnComplete(self, bstrUserID):
        pass


@dataclass(frozen=True)
class ReplyRow:
    """一列已經看懂的委託回報。"""

    seq: str
    type: str
    failed: bool
    qty: int


@dataclass(frozen=True)
class FillSummary:
    """一筆委託目前為止的回報彙整。

    ⚠️ **「收到回報」不等於「這筆委託結束了」。** IOC 的回報依序是：

        N 委託回報 → 交易所收下了，**單還在市場上**
        D 成交回報 → 成交一批（可能有好幾列）
        C 取消回報 → 剩餘量取消，到這裡才真的結束

    只看「有沒有收到回報」的話，`N` 一到就會以為結束了，於是在單還可能正在成交
    的時候回報 0 口、不寫狀態檔、不發通知——帳上多出來的部位沒有人知道。
    要問的問題是 `is_settled()`，不是 `matched_rows > 0`。
    """

    filled_lots: int
    rejected: bool
    matched_rows: int
    saw_cancel: bool = False
    reject_reason: str = ""

    def is_settled(self, requested_lots: int) -> bool:
        """這筆委託確定不會再有成交了嗎？

        三種確定的結局：
          被拒          → 沒有部位
          剩餘量已取消  → IOC 走完了，成交多少就是多少
          全部成交      → 沒有剩餘量可取消，等 C 只會等到逾時

        其他情況一律是「還沒結束」，交給呼叫端當成不確定處理。
        """
        return (
            self.rejected
            or self.saw_cancel
            or self.filled_lots >= requested_lots
        )


def summarize_fills(rows, seq: str) -> FillSummary:
    """彙整同一筆委託的回報，算出實際成交口數。

    三條規則，都是不對稱的——因為代價不對稱：

    1. **只要有成交就不算失敗。** 市價 IOC 的正常結局就是「成交一部分、
       剩下的取消」，取消列可能在成交列之後才到。把它當成失敗而不寫狀態檔的話，
       已成交的部位就沒有人知道，13:40 不會去平，直接進夜盤。
    2. **完全沒成交才談失敗。** 那時候確實沒有部位產生。
    3. **認不出是哪一筆就一列都不算。** 回報事件是共用的：手動下的單、
       連線時沖進來的前一批回報都在同一個緩衝區裡。加總到別人的成交上，
       下午就會平錯口數，多出來的部分變成反向新倉。

    比對委託序號用**整列子字串搜尋**而不是特定欄位。這個選擇原本是因為欄位位置
    還沒驗過，而 2026-08-17 的真實資料證明它另有價值：**成交回報的 `[0] KeyNo`
    是空的**，序號只出現在最後一欄。固定看某一欄的話，成交列就配不到委託列。

    ⚠️ **`seq` 為空字串時一列都不算**，因為空字串是任何字串的子字串。

    原本寫的是 `if seq and seq not in row: continue`——那個 `seq and` 讓
    序號拿不到時整條過濾被跳過，視窗內**每一列**期貨回報都被算成自己的成交。
    而序號來自 `SendFutureOrderCLR` 的回傳訊息，那是整個系統裡
    **唯一從來沒真正執行過的 API**，它成功時回不回序號我們並不知道。

    2026-08-19 實測證實這不是理論風險：使用者換倉的價差單就出現在
    OS 自己的回報串流上（見 fixtures/onnewdata-spread-2026-08-19.txt）。
    後果是 OS 記下一個它沒有的部位 → 13:40 送出平倉單 → 開出反向新倉。

    回空的彙整而不是拋例外：這是純函式，讓上層的 `is_settled()` 判定為
    「還沒結束」，自然走到 `FillUnknown`。那才是正確的結局——
    單確實送出去了，只是我們無法辨識回報，**絕不等於沒有部位**。
    """
    if not seq:
        return FillSummary(filled_lots=0, rejected=False, matched_rows=0)

    filled = 0
    matched = 0
    reject_reason = ""
    saw_cancel = False

    for row in rows:
        parsed = parse_reply_row(row)
        if parsed is None:
            continue
        if seq not in row:
            continue
        matched += 1
        if parsed.failed and not reject_reason:
            reject_reason = row
        if parsed.type == _REPLY_FILLED:
            filled += parsed.qty
        elif parsed.type == _REPLY_CANCELLED:
            saw_cancel = True

    return FillSummary(
        filled_lots=filled,
        # 有成交就不是失敗——剩餘量被取消是 IOC 的常態，不是錯誤
        rejected=bool(reject_reason) and filled == 0,
        matched_rows=matched,
        saw_cancel=saw_cancel,
        reject_reason=reject_reason if filled == 0 else "",
    )


def parse_reply_row(row: str) -> ReplyRow | None:
    """解析一列 `OnNewData` 回報。看不懂就回 `None`（不是我們的單、或格式不符）。

    欄位位置**已於 2026-08-17 實機驗證**（見 tests/test_reply_parsing.py，
    期望值來自交易所回來的真實字串，不是對文件的推導）。

    但形狀檢查**沒有因此變得多餘**，理由換了一個：回報事件是共用的，
    證券（TS）、海期（OF）等格式完全不同的東西會走同一條線進來，
    而已驗證的樣本只涵蓋一種商品、一種情境。遇到沒見過的格式時，
    唯一安全的行為是承認看不懂——回 None 讓上層當成「沒收到回報」處理
    （ticket 05 會記為「不確定」並要求人工確認）。

    Qty 抓錯的代價仍然是最貴的那個：記進狀態檔的口數錯 → 下午照那個數字平倉
    → 多平的部分變成方向相反的新倉。
    """
    fields = row.split(",")
    if len(fields) <= _REPLY_QTY:
        return None
    if fields[_REPLY_MARKET].strip() != _MARKET_FUTURES_REPLY:
        return None
    row_type = fields[_REPLY_TYPE].strip()
    if row_type not in _REPLY_TYPES:
        return None
    qty = fields[_REPLY_QTY].strip()
    if not qty.isdigit():
        return None

    return ReplyRow(
        seq=fields[_REPLY_KEYNO].strip() or fields[-1].strip(),
        type=row_type,
        failed=fields[_REPLY_ERR].strip() == "Y",
        qty=int(qty),
    )


def _query_rows(text: str) -> list[list[str]]:
    """把查詢回傳切成一列一列的欄位。非答案（空、M003、M999）回空 list。

    **`M003` 與 `M999` 都不是「沒成交」**——前者是查無資料（單可能還沒送到
    券商主機的紀錄裡），後者是查詢本身故障。當成「確定沒有部位」的話，
    真的成交了的那些口數就沒有人知道，13:40 不會去平，直接進夜盤。
    """
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith(_QUERY_NO_DATA):
        # 查無資料是**正常**的：單還沒進到券商主機的紀錄、或那天根本沒交易。
        logger.info("查詢回報：查無資料（%s）", _QUERY_NO_DATA)
        return []
    if text.startswith(_QUERY_ERROR):
        # 查詢**故障**。與上面那條長得很像但意思相反，所以等級刻意不同——
        # 兩者都回 []（絕不可當成「確定沒成交」），但這一種代表後備管道
        # 自己壞了，而那正是這張票要消滅的處境。log 分不出來的話，
        # 它會安靜地永遠回 None 而沒有人發現。
        logger.error("查詢回報：查詢錯誤（%s）：%s", _QUERY_ERROR, text[:120])
        return []
    return [line.split(",") for line in text.splitlines() if line.strip()]


def parse_order_book_no(text: str, order_seq: str, trading_day: int) -> str | None:
    """後備管道第一段：用**委託序號**在委託查詢裡找到自己那列，回傳**委託書號**。

    委託序號是 `SendFutureOrderCLR` 給的，也是 `OnNewData` 帶的；
    但成交查詢裡沒有它（2026-08-19 實測）。委託書號是唯一串得起兩份的欄位。

    ⚠️ **`order_seq` 為空字串時一列都不比。** 空字串是任何字串的子字串——
    ticket 04 的漏洞就是這樣把別人的成交算成自己的。

    ⚠️ **一定要比日期。** 委託書號的唯一性尚未驗證（每筆唯一？當日唯一？
    跨日重複？），在那之前日期是唯一擋得住「隔日殘留紀錄被誤認」的東西。
    """
    if not order_seq:
        return None

    for fields in _query_rows(text):
        if len(fields) <= _ORDER_DAY:
            continue
        if fields[_ORDER_MARKET].strip() != _MARKET_FUTURES_REPLY:
            continue        # 證券、海期走同一條線，格式完全不同
        if fields[_ORDER_SEQ].strip() != order_seq:
            continue
        if fields[_ORDER_DAY].strip() != str(trading_day):
            continue
        book_no = fields[_ORDER_BOOK_NO].strip()
        if book_no:
            return book_no
    return None


def parse_filled_lots(
    text: str, order_book_no: str, trading_day: int, requested_lots: int
) -> int | None:
    """後備管道第二段：加總這筆委託的成交口數。**認不出來就回 `None`。**

    回 `None` 與回 `0` 是完全不同的答案：

        None  不知道成交幾口 → 維持「不確定」，發 Discord 要人看帳戶
        0     **確定**沒有成交 → 上層什麼都不做

    所以「找不到成交列」回的是 `None` 而不是 `0`。查詢可能只是還沒反映，
    而若其實成交了卻記成 0，那個部位就沒有人知道、13:40 不會去平。

    ⚠️ **單腳成交列的欄位位置尚未實機驗證。** 手上唯一的真實樣本是價差單
    （使用者換倉），單腳的列會不會把第二隻腳那幾欄留白、還是整個往前移，
    我們不知道——這正是當初把微台 CID 推成 `FITMF` 的同一類錯誤。

    所以除了欄位，還有三道**與欄位位置無關**的檢查：日期要對、口數不得超過
    委託量、不得是價差列。推錯欄位最可能的症狀是抓到一個不相干的數字，
    而那三道擋得住它。實機里程碑送出第一筆真單後要回來把期望值釘死。
    """
    if not order_book_no:
        return None

    total = 0
    matched = 0
    for fields in _query_rows(text):
        if len(fields) <= _FILL_QTY:
            continue
        if fields[_FILL_MARKET].strip() != _MARKET_FUTURES_REPLY:
            continue
        if fields[_FILL_BOOK_NO].strip() != order_book_no:
            continue
        if fields[_FILL_DAY].strip() != str(trading_day):
            continue
        if "/" in ",".join(fields):
            # 價差列：一隻腳一列，加總會得到兩倍。OS 自己不下價差單，
            # 所以看到它就代表委託書號認錯了——前提已破，別硬算。
            #
            # ⚠️ **看整列，不是只看 `[12]`。** 初版只檢查 `[12]`，而真實的
            #    價差成交列在那一欄放的是**單腳**代碼（`TMFH6` / `TMFI6`）——
            #    斜線只出現在最後一欄。那道守衛因此對真實資料完全不會觸發
            #    （2026-08-19 code-review 實測；當時的測試之所以綠，是被
            #    「超額口數」那道擋下的，不是被這道）。
            #
            #    整列掃是刻意保守：價格、日期、序號都不含斜線，所以誤殺的機會
            #    很低；而萬一誤殺，結果只是回 None → 維持「不確定」→ 要人看一眼。
            return None
        qty = fields[_FILL_QTY].strip()
        if not qty.isdigit() or int(qty) < 1:
            # 0 口的成交列是矛盾的（沒成交就不該有列），多半是抓到別的欄位。
            return None
        try:
            float(fields[_FILL_PRICE].strip())
        except ValueError:
            # 口數與價格必須**同時**說得通。只驗口數的話，欄位整個位移時
            # 仍可能剛好撿到一個合法的小數字。
            return None
        total += int(qty)
        matched += 1

    if not matched:
        return None
    if total > requested_lots:
        # 委託 1 口卻算出 5 口——不可能是真的，而且是最貴的一種錯：
        # 下午照 5 口平倉，多出來的變成方向相反的新倉。
        logger.error("成交查詢算出 %d 口，超過委託的 %d 口，判定為解析錯誤",
                     total, requested_lots)
        return None
    return total


class _CenterEvents:
    """聲明書狀態接收端。沒有它，EnterMonitorLONG 會回 2018。"""

    def OnShowAgreement(self, bstrData):
        logger.info("聲明書狀態：%s", bstrData)

    def OnTimer(self, nTime):
        pass

    def OnNotifySGXAPIOrderStatus(self, nStatus, bstrOFAccount):
        pass


class _QuoteEvents:
    """報價連線狀態與商品清單的接收端。"""

    def __init__(self) -> None:
        self.stocks_ready = False
        self.quote_updates = 0
        self.commodity_chunks: list = []

    def OnConnection(self, nKind, nCode):
        if nKind == _STOCKS_READY:
            self.stocks_ready = True

    def OnNotifyQuoteLONG(self, sMarketNo, nIndex):
        self.quote_updates += 1

    def OnNotifyCommodityListWithTypeNo(self, sMarketNo, bstrCommodityData):
        # 清單很長（實測 46 萬字元），會分多次送來，全部接完再一起解析
        self.commodity_chunks.append(bstrCommodityData)


class CapitalBroker:
    """群益 COM 的 broker 實作。與 `broker.fake.FakeBroker` 同契約。"""

    def __init__(
        self,
        user_id: str,
        password: str,
        environment: str = "production",
        account: str = "",
        connect_timeout: float = 30.0,
        fill_timeout: float = 10.0,
    ):
        if environment not in _AUTHORITY_FLAGS:
            raise ValueError(
                f"environment 必須是 {list(_AUTHORITY_FLAGS)} 之一，目前是 {environment!r}"
            )
        self._user_id = user_id
        self._password = password
        self._environment = environment
        self._connect_timeout = connect_timeout
        self._fill_timeout = fill_timeout
        self._sk = None
        self._center = None
        self._quote = None
        self._order = None
        self._quote_events = None
        self._reply_events = None
        # 期貨帳號由 .env 明確指定，不從 OnAccount 自動挑——
        # 有多個帳號時「自動挑一個」會把單下到使用者沒預期的帳戶上。
        # 查法見 tools/verify_login.py。
        self._account = account
        self._handlers: list = []      # 事件 handler 要留著，被回收就收不到事件了
        self._monitoring = False
        self._subscribed = False
        self._order_ready = False
        # 券商回覆的原始存檔要寫到哪。可覆寫，讓測試用 tmp_path。
        # ⚠️ 內容未遮罩（含期貨帳號）——`logs/` 在 .gitignore 內，
        #    且由 housekeeping.purge_old_logs 於 30 天後清除。
        from housekeeping import LOGS_PATH
        self._replies_dir = LOGS_PATH

    # --- 內部：COM 生命週期 ---

    def _ensure_com(self) -> None:
        """建立 COM 物件並註冊事件。刻意延遲到這裡，模組載入時不執行。"""
        if self._center is not None:
            return

        import comtypes.client

        comtypes.client.GetModule(_resolve_dll_path())
        import comtypes.gen.SKCOMLib as sk

        self._sk = sk
        self._center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
        reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
        self._quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
        self._order = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)
        self._quote_events = _QuoteEvents()
        self._reply_events = _ReplyEvents()

        # 正式環境是不呼叫時的預設值，所以**刻意不呼叫**。
        #
        # ⚠️ 這裡曾經無條件呼叫 `SetAuthority(0)` 並在非 0 回傳時拋 LoginFailed。
        #    但文件說這個函式是「(SGX 專線only)…一般客戶可忽略此部分」——
        #    一般帳戶若回非 0，整個 08:50 的班就會死在一個它本來不需要的呼叫上。
        #    要正式環境就什麼都不做，這是最安全也最誠實的寫法。
        if self._environment != "production":
            flag = _AUTHORITY_FLAGS[self._environment]
            code = self._center.SKCenterLib_SetAuthority(flag)
            if code != 0:
                # 要了測試環境卻沒切成功 → 後續的單會進正式環境。必須擋下。
                raise LoginFailed(
                    f"要求連線環境 {self._environment} 但設定失敗，{self._message(code)}。"
                    "**不可繼續**——委託會送到正式環境。"
                    "此功能依官方文件屬於 SGX 專線，一般帳戶可能不支援，請洽營業員。"
                )
            logger.warning("已切換至非正式環境：%s（尚未經實機確認是否真的生效）",
                           self._environment)

        # 順序有意義：公告與聲明書都必須在登入前就有接收端
        self._handlers = [
            comtypes.client.GetEvents(reply, self._reply_events),
            comtypes.client.GetEvents(self._center, _CenterEvents()),
            comtypes.client.GetEvents(self._quote, self._quote_events),
        ]
        self._reply = reply

    def _pump_until(self, predicate, seconds: float) -> bool:
        """COM 事件需要訊息幫浦才會觸發。官方範例靠 tkinter 的 mainloop，我們自己打。"""
        import pythoncom

        deadline = time.time() + seconds
        while time.time() < deadline:
            pythoncom.PumpWaitingMessages()
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def _pump_for(self, seconds: float) -> None:
        """單純打滿指定秒數的訊息幫浦，不等待任何條件。"""
        self._pump_until(lambda: False, seconds)

    def _message(self, code: int) -> str:
        return f"代碼 {code}：{self._center.SKCenterLib_GetReturnCodeMessage(code)}"

    # --- 對外契約 ---

    def login(self) -> None:
        """**只做身分驗證**。失敗一律 raise LoginFailed（重試無用，要人處理）。

        ⚠️ 報價主機的連線**刻意不放在這裡**。那是會暫時性失敗的東西
        （主機忙、網路抖動），歸類成 LoginFailed 會讓它跳過重試階梯直接以錯誤結束——
        08:50 的一次小抖動就會賠掉整天的訊號。它屬於 `get_open_prices` 的可重試範圍。
        """
        self._ensure_com()

        # 第三個參數 "Y" 才會啟用報價元件
        code = self._center.SKCenterLib_LoginSetQuote(self._user_id, self._password, "Y")
        if code != 0:
            raise LoginFailed(self._message(code))
        logger.info("群益登入成功")

        # 聲明書狀態是登入後非同步查回來的，沒等它就連報價主機會拿到 2018
        self._center.SKCenterLib_RequestAgreement(self._user_id)
        self._pump_for(2.0)

    def _ensure_quote_connection(self) -> None:
        """連上報價主機並訂閱三個商品。失敗一律 QuoteNotReady——這些都值得重試。"""
        if self._monitoring:
            return

        code = self._quote.SKQuoteLib_EnterMonitorLONG()
        if code != 0:
            raise QuoteNotReady(f"連線報價主機失敗，{self._message(code)}")
        if not self._pump_until(lambda: self._quote_events.stocks_ready, self._connect_timeout):
            raise QuoteNotReady(f"{self._connect_timeout:.0f} 秒內未收到商品資料就緒通知")
        logger.info("報價主機連線完成")
        self._monitoring = True

    def get_contracts(self, wait: float = 8.0) -> dict:
        """查商品清單，取出三個商品的近月合約與最後交易日。

        近月連續代碼（`TX00AM` 等）在清單裡本身就帶最後交易日，不必自己挑月份。
        結算日當天它仍指向即將到期的那個合約——那正是我們要的，因為該合約
        當天 13:30 才停止交易，出場必須用它。

        市場別 2 = 期貨（0 上市、1 上櫃、3 選擇權）。
        """
        if self._center is None:
            raise ProductListUnavailable("尚未登入")

        self._ensure_quote_connection()
        self._quote_events.commodity_chunks.clear()

        code = self._quote.SKQuoteLib_RequestStockList(_MARKET_FUTURES)
        if code != 0:
            raise ProductListUnavailable(f"查詢商品清單失敗，{self._message(code)}")

        # 清單分多次送達。用「解析得出來」當停止條件，而不是「字串裡看得到代碼」——
        # 後者在分塊邊界剛好切在某一列中間時會誤判成收齊，拿到截斷的資料。
        parsed: dict = {}

        def _parsed_ok() -> bool:
            nonlocal parsed
            try:
                parsed = parse_product_list("".join(self._quote_events.commodity_chunks))
                return True
            except ProductListUnavailable:
                return False

        if not self._pump_until(_parsed_ok, wait):
            raise ProductListUnavailable(f"{wait:.0f} 秒內未取得完整商品清單")
        return parsed

    def query_filled_lots(
        self, *, order_seq: str, trading_day: int, requested_lots: int,
        sleep=time.sleep,
    ) -> int | None:
        """推播收不到時的後備管道：**主動問券商主機**這筆委託成交了幾口。

        回 `None` 代表「還是不知道」——上層維持「不確定」的處理（發 Discord
        要人看帳戶）。**絕不可以把 `None` 當成 0。**

        走兩段是被迫的（2026-08-19 實測）：委託序號只在 `GetOrderReport` 裡，
        `GetFulfillReport` 沒有。所以先用序號換出委託書號，再拿它去比對成交。

        ⚠️ **中間一定要隔五秒**，官方文件明載「限制每次查詢間需間隔五秒」。
        這條後備路徑因此最少要 5 秒——08:50 那班還很寬裕，但別搬到更緊的時窗。

        **這個函式不拋例外。** 它是失敗路徑上的補救措施，自己再炸一次的話，
        原本只是「不確定」的一天會變成整班掛掉。
        """
        try:
            self._ensure_order_ready()
            if not self._account:
                logger.warning("沒有期貨帳號，無法查詢成交")
                return None

            raw = self._order.GetOrderReport(self._user_id, self._account, 1)
            book_no = parse_order_book_no(raw or "", order_seq, trading_day)
            if not book_no:
                logger.info("委託查詢找不到序號 %s 的委託書號", order_seq)
                return None
            logger.info("委託 %s 的委託書號為 %s", order_seq, book_no)

            sleep(_QUERY_INTERVAL_SECONDS)

            raw = self._order.GetFulfillReport(self._user_id, self._account, 1)
            lots = parse_filled_lots(raw or "", book_no, trading_day, requested_lots)
            if lots is None:
                logger.info("成交查詢認不出委託書號 %s 的成交列", book_no)
            else:
                logger.info("成交查詢：委託書號 %s 成交 %d 口", book_no, lots)
            return lots
        except Exception as exc:  # noqa: BLE001
            logger.error("成交查詢失敗（%s）：%s", type(exc).__name__, exc)
            return None

    def _ensure_order_ready(self) -> None:
        """初始化下單元件、連上回報、取得期貨帳號。

        ⚠️ **刻意不在 `login()` 裡做。** 自動下單關閉時整條路徑都不該被走到——
        下單元件初始化失敗會變成致命錯誤，而那天其實只需要發訊號。
        """
        if self._order_ready:
            return

        code = self._order.SKOrderLib_Initialize()
        if code != 0:
            raise OrderFailed(f"下單元件初始化失敗，{self._message(code)}")

        # ⚠️ **讀憑證。少了這一步，送出委託會被回 1038 SK_ERROR_CERT_NOT_VERIFIED。**
        #
        # 2026-08-21 實機里程碑第一次真的送出委託時就是被這個擋下的。憑證本身
        # 完全正常——裝了、沒過期、有私鑰、CN 與登入 ID 相符、電腦裡只有一張。
        # 純粹是程式漏了這一行。
        #
        # 為什麼之前沒發現：**看報價、查商品清單、查委託成交都不需要憑證，
        # 只有送出委託需要。** 所以這個洞在真的下單之前不可能浮出來。
        #
        # 在這裡失敗比在送單時失敗好得多：1038 的訊息是「Cert Not Verified」，
        # 要人自己去猜是哪一步漏了；這裡可以直接說「憑證讀取失敗」。
        code = self._order.ReadCertByID(self._user_id)
        if code != 0:
            raise OrderFailed(f"憑證讀取失敗，{self._message(code)}")

        # 回報連線。沒有它就收不到 OnNewData，也就不知道成交幾口。
        code = self._reply.SKReplyLib_ConnectByID(self._user_id)
        if code != 0:
            raise OrderFailed(f"回報連線失敗，{self._message(code)}")

        # ⚠️ **回 0 只代表「請求已受理」。** 連線結果由 OnConnect 非同步送回，
        #    所以要跑一下訊息幫浦讓事件有機會進來。
        #
        #    連不上**不擋下單**：ticket 09 的查詢是獨立的後備管道，
        #    推播壞掉時它還救得回來。擋下來反而是把「今天可能沒訊號」
        #    這個更大的代價換一個更小的。但一定要記進 log——
        #    等不到回報時，這一行是唯一告訴人「該往哪裡查」的東西。
        self._pump_for(1.0)
        if self._reply_events.connected is False:
            logger.error("%s。推播收不到時將改用成交查詢（ticket 09）",
                         self._reply_events.connect_error)
        elif self._reply_events.connected is None:
            logger.warning("回報連線狀態未知（OnConnect 尚未回來）")

        # 群益要求下單前先查過帳號，即使我們用的是 .env 指定的那一個
        code = self._order.GetUserAccount()
        if code != 0:
            raise OrderFailed(f"取得帳號失敗，{self._message(code)}")
        self._pump_for(2.0)

        self._order_ready = True

    def place_order(self, request: OrderRequest) -> OrderResult:
        """送出市價 IOC 委託，等成交回報後回傳**實際成交口數**。

        依 ADR-0003：市價、IOC、不標當沖。市價在群益是 `bstrPrice = "M"`，
        且官方文件註明只能搭配 IOC 或 FOK——與 ADR 的選擇剛好一致。

        ⚠️ **失敗的分類以「單有沒有送出去」為界：**

            送出之前失敗 → `OrderFailed`   確定沒有部位
            送出之後失敗 → `FillUnknown`   不知道結局，可能已經成交

        後者包含逾時、COM 壞掉、訊息幫浦拋例外——任何意外都算。
        說成「確定沒有部位」的話，上層不會寫狀態檔，13:40 那班會以為今天沒進場。
        見 tests/test_post_send_failures.py。
        """
        if self._center is None:
            raise OrderFailed("尚未登入")

        self._ensure_order_ready()
        if not self._account:
            raise OrderFailed(
                "未設定期貨帳號。請執行 tools/verify_login.py --show-account 查出，"
                "填入 .env 的 CAPITAL_FUTURES_ACCOUNT"
            )

        order = self._sk.FUTUREORDER()
        for field, value in build_future_order_fields(request, self._account).items():
            setattr(order, field, value)

        before = len(self._reply_events.rows)
        message, code = self._order.SendFutureOrderCLR(self._user_id, False, order)
        if code != 0:
            # 送出這一步就失敗 → 確定沒有部位產生，OrderFailed 名副其實。
            raise OrderFailed(f"委託送出失敗，{self._message(code)}（{message}）")

        # ⚠️ **過了這一行，單就在市場上了。** 之後任何一種失敗都必須是
        #    FillUnknown 而不是 OrderFailed——包括 COM 壞掉、訊息幫浦拋例外
        #    這種與委託本身無關的意外。說成「確定沒有部位」的話，
        #    上層就不會留下記錄，13:40 那班會以為今天沒進場。
        # ⚠️ `str(None)` 是 `"None"`——一個看起來很正常的非空字串。
        #    少了這個 `is None` 判斷，回傳 None 時會拿字面上的 "None" 當序號，
        #    它配不到任何一列，於是白等 10 秒才以「仍未結束」收場——
        #    那句話聽起來像市場沒成交，而真正的問題是我們根本沒有序號。
        seq = "" if message is None else str(message).strip()
        if not seq:
            # 送出成功（code == 0）卻拿不到序號。**單已經在市場上了**，
            # 但我們失去了辨識自己回報的唯一依據——回報事件是共用的，
            # 使用者的手動交易走同一條線（2026-08-19 實測：換倉的價差單
            # 就出現在這條串流上）。沒有序號就只能承認不知道，
            # 不能等 10 秒逾時再說——那 10 秒的訊息會是「仍未結束」，
            # 聽起來像市場沒成交，而真正的問題是我們認不出來。
            raise FillUnknown(
                "委託已送出但未取得委託序號，無法辨識成交回報。"
                "請人工確認帳戶實際部位。",
                order_seq="",
            )
        logger.info("委託已送出，序號 %s", seq)
        try:
            return self._await_fill(seq=seq, since=before, requested_lots=request.lots)
        except (OrderFailed, FillUnknown):
            raise
        except Exception as exc:  # noqa: BLE001
            raise FillUnknown(
                f"等待委託 {seq} 的回報時發生非預期錯誤（{type(exc).__name__}）：{exc}",
                order_seq=seq,
            ) from exc
        finally:
            # 不管成功、失敗、還是不確定，都把券商回的原始內容留下來。
            # **收不到回報那天最需要它**——那正是要回頭查的時候，
            # 而只在成功時存的話，能查的都是不必查的。
            self._save_raw_replies(seq, since=before)

    def _save_raw_replies(self, seq: str, since: int) -> None:
        """把這次委託期間收到的回覆原封不動存成一個檔案。

        存**整個視窗**收到的全部內容，不只我們自己那筆——2026-08-19 就是靠
        「別人的單也在同一條線上」這個證據，才發現序號比對的漏洞。

        ⚠️ **這個函式跑在 `finally` 裡，所以它絕對不可以拋例外。**
        `finally` 拋出的東西會**取代**正在傳遞的那個例外，於是使用者收到的
        會是「寫檔失敗」而不是「收不到成交回報」——真正該處理的問題被蓋掉，
        而且蓋得無聲無息。同理，它也不可以把一筆成功的下單變成失敗。

        ⚠️ **內容未遮罩**（含期貨帳號）。`logs/` 已在 .gitignore 內，
        且由 `housekeeping.purge_old_logs` 於 30 天後清除。
        """
        try:
            rows = self._reply_events.rows[since:]
            if not rows:
                return
            directory = self._replies_dir
            os.makedirs(directory, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(directory, f"replies-{stamp}-{seq}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(chr(10).join(rows) + chr(10))
            logger.info("已存下 %d 列原始回覆：%s", len(rows), os.path.basename(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("原始回覆存檔失敗（%s）：%s", type(exc).__name__, exc)

    def _await_fill(self, seq: str, since: int, requested_lots: int) -> OrderResult:
        """等到這筆委託確定結束為止，回傳實際成交口數。

        等的是 `is_settled()`，**不是「有沒有收到回報」**——委託回報（N）
        會在成交回報之前先到，拿它當結束條件的話，程式會在單還可能正在成交時收工。
        """

        def _summary() -> FillSummary:
            return summarize_fills(self._reply_events.rows[since:], seq)

        self._pump_until(
            lambda: _summary().is_settled(requested_lots), self._fill_timeout
        )
        summary = _summary()

        if not summary.is_settled(requested_lots):
            # 不知道結局。**這不等於沒有成交**——已知成交的部分也一併講出來，
            # 使用者去對帳時知道至少要看到幾口。
            known = (f"（目前已知成交 {summary.filled_lots} 口，"
                     f"委託 {requested_lots} 口）" if summary.filled_lots else "")
            raise FillUnknown(
                f"{self._fill_timeout:.0f} 秒內委託 {seq} 仍未結束{known}",
                order_seq=seq,
                known_filled=summary.filled_lots,
            )
        if summary.rejected:
            raise OrderFailed(f"委託被拒且無成交：{summary.reject_reason}")

        if summary.filled_lots == 0:
            logger.warning("委託 %s 未成交（市價 IOC 當下沒有對手價）", seq)
        return OrderResult(filled_lots=summary.filled_lots, order_seq=seq)

    def get_open_prices(self, expected_trading_day: int | None = None) -> OpenPrices:
        """取三個商品的當日 AM 盤開盤價。

        未就緒或數值不合理時 raise QuoteNotReady——判定邏輯與假 broker 共用
        （`broker.build_open_prices`），所以兩者行為一致不靠自律。
        """
        if self._center is None:
            raise QuoteNotReady("尚未登入")

        self._ensure_quote_connection()

        if not self._subscribed:
            page_no = 0
            page_no, code = self._quote.SKQuoteLib_RequestStocks(page_no, ",".join(PRODUCT_CODES))
            if code != 0:
                raise QuoteNotReady(f"訂閱報價失敗，{self._message(code)}")
            # 訂閱後等第一批報價進來；等不到就當作未就緒，交給上層重試
            self._pump_until(lambda: self._quote_events.quote_updates > 0, 15.0)
            self._subscribed = True
        else:
            self._pump_for(1.0)

        quotes = {}
        for code_str in PRODUCT_CODES:
            obj = self._sk.SKSTOCKLONG()
            obj, ret = self._quote.SKQuoteLib_GetStockByNoLONG(code_str, obj)
            if ret != 0:
                quotes[code_str] = None
                continue
            quotes[code_str] = Quote(
                code=code_str,
                open=obj.nOpen / _PRICE_SCALE,
                limit_up=obj.nUp / _PRICE_SCALE,
                limit_down=obj.nDown / _PRICE_SCALE,
                # 休市時群益會繼續給上一交易日的價格，且看起來完全正常。
                # 沒有這個欄位就分不出「今天的」與「上次的」。
                trading_day=obj.nTradingDay,
            )

        return build_open_prices(quotes, expected_trading_day=expected_trading_day)
