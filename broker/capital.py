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

from broker import (
    MTX_CODE,
    PRODUCT_CODES,
    TMF_CODE,
    TX_CODE,
    LoginFailed,
    OpenPrices,
    Quote,
    QuoteNotReady,
    build_open_prices,
)

logger = logging.getLogger(__name__)

# SKCenterLib 的 CLSID，用來從註冊表反查 SKCOM.dll 的實際位置
_SKCENTER_CLSID = "{AC30BAB5-194A-4515-A8D3-6260749F8577}"

# 報價主機的連線狀態代碼：收到它才代表商品資料下載完成
_STOCKS_READY = 3003

# 群益的價格是整數且放大 100 倍
_PRICE_SCALE = 100.0

__all__ = ["CapitalBroker", "PRODUCT_CODES"]


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
    """公告接收端。官方要求登入前註冊，且事件必須回傳 -1。"""

    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1


class _CenterEvents:
    """聲明書狀態接收端。沒有它，EnterMonitorLONG 會回 2018。"""

    def OnShowAgreement(self, bstrData):
        logger.info("聲明書狀態：%s", bstrData)

    def OnTimer(self, nTime):
        pass

    def OnNotifySGXAPIOrderStatus(self, nStatus, bstrOFAccount):
        pass


class _QuoteEvents:
    """報價連線狀態接收端。"""

    def __init__(self) -> None:
        self.stocks_ready = False
        self.quote_updates = 0

    def OnConnection(self, nKind, nCode):
        if nKind == _STOCKS_READY:
            self.stocks_ready = True

    def OnNotifyQuoteLONG(self, sMarketNo, nIndex):
        self.quote_updates += 1


class CapitalBroker:
    """群益 COM 的 broker 實作。與 `broker.fake.FakeBroker` 同契約。"""

    def __init__(self, user_id: str, password: str, connect_timeout: float = 30.0):
        self._user_id = user_id
        self._password = password
        self._connect_timeout = connect_timeout
        self._sk = None
        self._center = None
        self._quote = None
        self._quote_events = None
        self._handlers: list = []      # 事件 handler 要留著，被回收就收不到事件了
        self._subscribed = False

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
        self._quote_events = _QuoteEvents()

        # 順序有意義：公告與聲明書都必須在登入前就有接收端
        self._handlers = [
            comtypes.client.GetEvents(reply, _ReplyEvents()),
            comtypes.client.GetEvents(self._center, _CenterEvents()),
            comtypes.client.GetEvents(self._quote, self._quote_events),
        ]

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

    def _message(self, code: int) -> str:
        return f"代碼 {code}：{self._center.SKCenterLib_GetReturnCodeMessage(code)}"

    # --- 對外契約 ---

    def login(self) -> None:
        """登入並連上報價主機。失敗一律 raise LoginFailed（重試無用，要人處理）。"""
        self._ensure_com()

        # 第三個參數 "Y" 才會啟用報價元件
        code = self._center.SKCenterLib_LoginSetQuote(self._user_id, self._password, "Y")
        if code != 0:
            raise LoginFailed(self._message(code))
        logger.info("群益登入成功")

        # 聲明書狀態是登入後非同步查回來的，沒等它就連報價主機會拿到 2018
        self._center.SKCenterLib_RequestAgreement(self._user_id)
        self._pump_until(lambda: False, 2.0)

        code = self._quote.SKQuoteLib_EnterMonitorLONG()
        if code != 0:
            raise LoginFailed(f"連線報價主機失敗，{self._message(code)}")
        if not self._pump_until(lambda: self._quote_events.stocks_ready, self._connect_timeout):
            raise LoginFailed(f"{self._connect_timeout:.0f} 秒內未收到商品資料就緒通知")
        logger.info("報價主機連線完成")

    def get_open_prices(self) -> OpenPrices:
        """取三個商品的當日 AM 盤開盤價。

        未就緒或數值不合理時 raise QuoteNotReady——判定邏輯與假 broker 共用
        （`broker.build_open_prices`），所以兩者行為一致不靠自律。
        """
        if self._center is None:
            raise QuoteNotReady("尚未登入")

        if not self._subscribed:
            page_no = 0
            page_no, code = self._quote.SKQuoteLib_RequestStocks(page_no, ",".join(PRODUCT_CODES))
            if code != 0:
                raise QuoteNotReady(f"訂閱報價失敗，{self._message(code)}")
            # 訂閱後等第一批報價進來；等不到就當作未就緒，交給上層重試
            self._pump_until(lambda: self._quote_events.quote_updates > 0, 15.0)
            self._subscribed = True
        else:
            self._pump_until(lambda: False, 1.0)

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
            )

        return build_open_prices(quotes)
