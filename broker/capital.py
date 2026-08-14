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
import time
import winreg
from dataclasses import dataclass

from broker import (
    BUY,
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

# 連線環境（SKCenterLib_SetAuthority）：0 正式、1 正式SGX、2 測試、3 測試SGX。
# **沒有呼叫這個函式時預設是 0（正式環境）**——所以環境一定要明講。
_AUTHORITY_FLAGS = {"production": 0, "test": 2}

# FUTUREORDER 的欄位值（官方文件 5 章的結構定義）
_TRADE_TYPE_IOC = 1        # 0:ROD 1:IOC 2:FOK
_BUY, _SELL = 0, 1         # sBuySell
_DAY_TRADE_NO = 0          # 不標記當沖
_SESSION_INTRADAY = 0      # sReserved：0 盤中（T盤及T+1盤）、1 T盤預約
_MARKET_PRICE = "M"        # bstrPrice：「M」市價、「P」範圍市價；限 IOC/FOK

# 倉別 sNewClose：0 新倉、1 平倉、2 自動。
# ⚠️ **尚未實機驗證**（ticket 04 最後一條）。台指期同帳號同商品同月份是淨額計算，
#    在已有反向部位時用「新倉」可能被拒或產生非預期結果；若實測如此，改用 2（自動）
#    由券商判斷。在測試環境實打確認之前，不可開啟自動下單。
_NEW_CLOSE_NEW = 0

# OnNewData 的欄位位置。⚠️ 由官方文件的欄位排列推導，尚未實機驗證，
# 所以 parse_reply_row 不信任索引而是驗證形狀（詳見該函式）。
_REPLY_KEYNO, _REPLY_MARKET, _REPLY_TYPE, _REPLY_ERR, _REPLY_QTY = 0, 1, 2, 3, 20
_MARKET_FUTURES_REPLY = "TF"
_REPLY_TYPES = frozenset("NCUPDBS")     # N委託 C取消 U改量 P改價 D成交 B改價改量 S動態退單
_REPLY_FILLED = "D"

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
        "sNewClose": _NEW_CLOSE_NEW,
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
    """公告與委託回報的接收端。官方要求登入前註冊，且 OnReplyMessage 必須回傳 -1。"""

    def __init__(self) -> None:
        self.rows: list = []

    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1

    def OnNewData(self, bstrUserID, bstrData):
        self.rows.append(bstrData)

    def OnConnect(self, bstrUserID, nErrorCode):
        pass

    def OnDisconnect(self, bstrUserID, nErrorCode):
        pass

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
    """一筆委託的成交結果。

    `matched_rows` 是「認出幾列屬於這筆委託」。它與 `filled_lots == 0` 合起來
    才分得出三種完全不同的處境：

      matched_rows > 0, filled > 0  → 成交了，記進狀態檔
      matched_rows > 0, filled == 0 → 確實沒成交（IOC 沒對手價，或被拒）
      matched_rows == 0             → **不知道**。回報還沒到，或認不出是哪一筆。
                                       委託可能已經成交，絕不可以當成「沒有部位」。
    """

    filled_lots: int
    rejected: bool
    matched_rows: int
    reject_reason: str = ""


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

    比對委託序號用**整列子字串搜尋**而不是特定欄位——欄位位置本身還沒實機驗證，
    而 13 碼序號夠獨特，出現在哪一欄都認得出來。
    """
    filled = 0
    matched = 0
    reject_reason = ""

    for row in rows:
        parsed = parse_reply_row(row)
        if parsed is None:
            continue
        if seq and seq not in row:
            continue
        matched += 1
        if parsed.failed and not reject_reason:
            reject_reason = row
        if parsed.type == _REPLY_FILLED:
            filled += parsed.qty

    return FillSummary(
        filled_lots=filled,
        # 有成交就不是失敗——剩餘量被取消是 IOC 的常態，不是錯誤
        rejected=bool(reject_reason) and filled == 0,
        matched_rows=matched,
        reject_reason=reject_reason if filled == 0 else "",
    )


def parse_reply_row(row: str) -> ReplyRow | None:
    """解析一列 `OnNewData` 回報。看不懂就回 `None`（不是我們的單、或格式不符）。

    ⚠️ **欄位位置是從官方文件的欄位排列推導出來的，尚未實機驗證。**
    Qty 的索引若推錯，記進狀態檔的就是錯的口數，而下午會照那個錯的數字去平倉——
    多平的部分會變成方向相反的新倉。這是本系統最貴的失效模式。

    因此這裡**不信任索引，而是驗證形狀**：市場別必須是 TF、Type 必須是已知代碼、
    數量必須是數字。任何一項對不上就回 None，讓上層當成「沒收到回報」處理
    （ticket 05 會把那種情況記為「不確定」並要求人工確認）——
    寧可承認不知道，也不要記一個看起來很合理的錯誤數字。
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

        # ⚠️ 環境必須在登入前設定，而且**不呼叫時預設是正式環境**。
        #    測試單送到正式環境就是真錢，所以這一行不可以省略。
        flag = _AUTHORITY_FLAGS[self._environment]
        code = self._center.SKCenterLib_SetAuthority(flag)
        if code != 0:
            raise LoginFailed(f"設定連線環境（{self._environment}）失敗，{self._message(code)}")
        logger.info("連線環境：%s", self._environment)

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

        # 回報連線。沒有它就收不到 OnNewData，也就不知道成交幾口。
        code = self._reply.SKReplyLib_ConnectByID(self._user_id)
        if code != 0:
            raise OrderFailed(f"回報連線失敗，{self._message(code)}")

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

        ⚠️ 收不到回報時目前是拋 `OrderFailed`。**這在語意上並不精確**：
        委託可能已經成交、只是回報沒回來，而 `OrderFailed` 的意思是「沒有部位產生」。
        正確處理（記為「不確定」並要人工確認）屬於 ticket 05，在那之前
        不可開啟自動下單。
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
            raise OrderFailed(f"委託送出失敗，{self._message(code)}（{message}）")
        logger.info("委託已送出，序號 %s", message)

        return self._await_fill(seq=str(message), since=before)

    def _await_fill(self, seq: str, since: int) -> OrderResult:
        """等回報到齊，回傳實際成交口數。彙整規則見 `summarize_fills`。"""

        def _summary() -> FillSummary:
            return summarize_fills(self._reply_events.rows[since:], seq)

        # 等到認出至少一列屬於這筆委託為止
        self._pump_until(lambda: _summary().matched_rows > 0, self._fill_timeout)
        summary = _summary()

        if summary.matched_rows == 0:
            # ⚠️ 這**不等於**沒有成交，只是回報沒到。用 OrderFailed 表達其實不精確
            #    （那個例外的意思是「確定沒有部位產生」），但在 ticket 05 把
            #    「不確定」狀態做出來之前，停手並要求人工確認是唯一安全的行為。
            raise OrderFailed(
                f"{self._fill_timeout:.0f} 秒內未收到委託 {seq} 的回報。"
                "**委託可能已經成交**，請立刻人工確認帳戶部位。"
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
