"""群益 API 環境驗證 — 一支程式檢查 docs/LOGIN_SETUP.md 的每一步是否到位。

用法：
    python tools/verify_login.py

除了驗證登入，它還回答專案目前卡住的兩個問題：
    1. 微台在群益的商品代號到底是什麼（文件完全沒寫）
    2. 即時報價的開盤價是「日盤」還是「全盤」的
       ——這是唯一一個會讓每天訊號都錯、卻從數字看不出來的風險

本程式刻意不依賴專案其他模組，可獨立執行。
COM 初始化全部延遲到函式內，因此在沒有註冊 COM 的機器上仍可 import。
"""

from __future__ import annotations

import os
import platform
import sys
import time
import winreg

# 群益報價主機的連線狀態代碼。收到這個才代表商品資料下載完成、可以查詢。
# 若實際跑出來不是 3003，看 OnConnection 印出的訊息改這個值。
SK_SUBJECT_CONNECTION_STOCKS_READY = 3003

# 期貨市場代碼（0=上市 1=上櫃 2=期貨 3=選擇權）
MARKET_FUTURES = 2

# 報價訂閱用的近月連續代碼。TMF00 是推定值，本程式就是要驗證它。
QUOTE_CODES = ("TX00", "MTX00", "TMF00")

# SKCenterLib / SKOrderLib 的 CLSID，用來確認 COM 是否註冊過
CLSIDS = {
    "SKCenterLib": "{AC30BAB5-194A-4515-A8D3-6260749F8577}",
    "SKOrderLib": "{54FE0E28-89B6-43A7-9F07-BE988BB40299}",
}

TAIFEX_API = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"


def _head(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _fail(msg: str) -> None:
    print(f"  [失敗] {msg}")


def _info(msg: str) -> None:
    print(f"         {msg}")


def check_python() -> bool:
    """Step 4-1：位元數必須與 COM 元件一致。我們用 x64，所以 Python 要 64-bit。"""
    _head("1. Python 環境")
    bits = platform.architecture()[0]
    _info(f"版本 {sys.version.split()[0]}，架構 {bits}")
    if bits != "64bit":
        _fail("Python 不是 64 位元。請改用 64-bit Python，或改註冊 x86 版的 COM 元件。")
        return False
    _ok("64 位元，與 x64 COM 元件相符")
    return True


def check_com_registered() -> bool:
    """Step 4-3：檢查 regsvr32 有沒有跑成功。"""
    _head("2. COM 元件註冊狀態")
    all_ok = True
    for name, clsid in CLSIDS.items():
        key_path = rf"CLSID\{clsid}\InprocServer32"
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
                dll_path = winreg.QueryValueEx(key, "")[0]
            _ok(f"{name} 已註冊 → {dll_path}")
            if not os.path.exists(dll_path):
                _fail(f"  但該路徑的檔案不存在！元件資料夾被搬走或改名了。")
                _info("  → 回 docs/LOGIN_SETUP.md Step 4，重新複製並註冊")
                all_ok = False
        except FileNotFoundError:
            _fail(f"{name} 未註冊")
            all_ok = False
    if not all_ok:
        _info("→ 以系統管理員身分執行：cd C:\\SKCOM && install.bat")
    return all_ok


def load_credentials() -> tuple[str, str] | None:
    """從 .env 讀帳密。刻意不用第三方套件，少一個相依。"""
    _head("3. 讀取帳號密碼")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(here, ".env")
    if not os.path.exists(env_path):
        _fail(f"找不到 {env_path}")
        _info("→ 複製 .env.example 改名為 .env，填入帳號密碼")
        return None
    values: dict[str, str] = {}
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    user_id = values.get("CAPITAL_USER_ID", "")
    password = values.get("CAPITAL_PASSWORD", "")
    if not user_id or not password:
        _fail("CAPITAL_USER_ID 或 CAPITAL_PASSWORD 是空的")
        return None
    _ok(f"帳號 {user_id[:2]}****{user_id[-2:] if len(user_id) > 3 else ''}（密碼不顯示）")
    return user_id, password


class _QuoteEvents:
    """報價事件接收器。只記錄，不做事——真正的判讀在主流程。"""

    def __init__(self) -> None:
        self.connection_kinds: list[int] = []
        self.stocks_ready = False
        self.commodity_chunks: list[str] = []
        self.quote_notifications = 0

    def OnConnection(self, nKind, nCode):
        self.connection_kinds.append(nKind)
        if nKind == SK_SUBJECT_CONNECTION_STOCKS_READY:
            self.stocks_ready = True

    def OnNotifyCommodityListWithTypeNo(self, sMarketNo, bstrCommodityData):
        self.commodity_chunks.append(bstrCommodityData)

    def OnNotifyQuoteLONG(self, sMarketNo, nIndex):
        self.quote_notifications += 1


class _ReplyEvents:
    """註冊公告。官方要求登入前必須先註冊，且事件必須回傳 -1。"""

    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1


def _pump(seconds: float) -> None:
    """COM 事件要有訊息幫浦才會觸發。範例靠 tkinter 的 mainloop，我們自己打。"""
    import pythoncom

    deadline = time.time() + seconds
    while time.time() < deadline:
        pythoncom.PumpWaitingMessages()
        time.sleep(0.05)


def _pump_until(predicate, seconds: float) -> bool:
    import pythoncom

    deadline = time.time() + seconds
    while time.time() < deadline:
        pythoncom.PumpWaitingMessages()
        if predicate():
            return True
        time.sleep(0.05)
    return False


def run_api_checks(user_id: str, password: str) -> None:
    """實際連線的部分。COM 在這裡才初始化——模組層級不碰，否則沒裝 COM 的機器連 import 都會失敗。"""
    import comtypes.client

    _head("4. 初始化 COM 物件")
    comtypes.client.GetModule("SKCOM.dll")
    import comtypes.gen.SKCOMLib as sk  # noqa: E402  (必須在 GetModule 之後)

    center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
    reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
    quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
    _ok(f"SKAPI 版本 {center.SKCenterLib_GetSKAPIVersionAndBit('')}")

    def msg_of(code: int) -> str:
        return center.SKCenterLib_GetReturnCodeMessage(code)

    # 註冊公告：官方明訂必須在登入前完成
    reply_events = _ReplyEvents()
    reply_handler = comtypes.client.GetEvents(reply, reply_events)  # noqa: F841
    quote_events = _QuoteEvents()
    quote_handler = comtypes.client.GetEvents(quote, quote_events)  # noqa: F841

    _head("5. 登入")
    code = center.SKCenterLib_Login(user_id, password)
    if code != 0:
        _fail(f"登入失敗（代碼 {code}）：{msg_of(code)}")
        _info("→ 對照 docs/LOGIN_SETUP.md 的「常見錯誤代碼」表")
        return
    _ok("登入成功")

    _head("6. 連線報價主機")
    code = quote.SKQuoteLib_EnterMonitorLONG()
    if code != 0:
        _fail(f"EnterMonitorLONG 失敗（{code}）：{msg_of(code)}")
        return
    if not _pump_until(lambda: quote_events.stocks_ready, 30):
        _fail("30 秒內沒收到「商品資料就緒」通知")
        seen = [f"{k}({msg_of(k)})" for k in quote_events.connection_kinds]
        _info(f"期間收到的連線事件：{seen or '無'}")
        _info(f"→ 若上面有看起來像就緒的代碼，把本檔的 "
              f"SK_SUBJECT_CONNECTION_STOCKS_READY 改成該值")
        return
    _ok("商品資料下載完成")

    # ---- 問題一：微台的商品代號 ----
    _head("7. 期貨商品清單 —— 找出微台的正確代號")
    code = quote.SKQuoteLib_RequestStockList(MARKET_FUTURES)
    if code != 0:
        _fail(f"RequestStockList 失敗（{code}）：{msg_of(code)}")
    else:
        _pump(5)
        raw = "".join(quote_events.commodity_chunks)
        if not raw:
            _fail("沒收到商品清單資料")
        else:
            _ok(f"收到 {len(raw)} 字元")
            # 格式：[商品代碼],[名稱],[最後交易日],[交易所商品代碼];
            hits = []
            for entry in raw.replace("\n", ";").split(";"):
                fields = entry.split(",")
                if len(fields) < 2:
                    continue
                name = fields[1]
                if any(k in name for k in ("臺指", "台指", "微型", "小型")):
                    hits.append(fields)
            if hits:
                print(f"\n  {'商品代碼':<12}{'名稱':<18}{'最後交易日':<12}交易所代碼")
                for f in hits[:30]:
                    ltd = f[2] if len(f) > 2 else ""
                    oid = f[3] if len(f) > 3 else ""
                    print(f"  {f[0]:<12}{f[1]:<18}{ltd:<12}{oid}")
                print("\n  ⭐ 上面就是答案：找「微型臺指」那列的商品代碼，"
                      "以及它的最後交易日（結算日判定要用）")
            else:
                _info("沒篩到台指相關商品，印出前 500 字供人工判讀：")
                print("  " + raw[:500])

    # ---- 問題二：開盤價是日盤還是全盤 ----
    _head("8. 三商品開盤價 —— 確認是日盤還是夜盤")
    page_no = 0
    page_no, code = quote.SKQuoteLib_RequestStocks(page_no, ",".join(QUOTE_CODES))
    if code != 0:
        _fail(f"RequestStocks 失敗（{code}）：{msg_of(code)}")
        _info("→ 若錯誤與商品代碼有關，可能是 TMF00 不對。用第 7 步查到的代碼改 QUOTE_CODES")
        return
    if not _pump_until(lambda: quote_events.quote_notifications > 0, 15):
        _fail("15 秒內沒收到任何報價通知")
        return
    _pump(2)  # 讓三檔都進來

    opens: dict[str, float] = {}
    print(f"\n  {'代碼':<10}{'開盤':>10}{'成交':>10}{'漲停':>10}{'跌停':>10}  不變量")
    for stock_no in QUOTE_CODES:
        obj = sk.SKSTOCKLONG()
        obj, code = quote.SKQuoteLib_GetStockByNoLONG(stock_no, obj)
        if code != 0:
            print(f"  {stock_no:<10}  取值失敗（{code}）：{msg_of(code)}")
            continue
        # 群益的價格是整數並放大 100 倍
        o, c = obj.nOpen / 100.0, obj.nClose / 100.0
        up, dn = obj.nUp / 100.0, obj.nDown / 100.0
        invariant = "OK" if dn <= o <= up and o > 0 else "!! 不通過"
        print(f"  {stock_no:<10}{o:>10.0f}{c:>10.0f}{up:>10.0f}{dn:>10.0f}  {invariant}")
        opens[stock_no] = o

    compare_with_taifex(opens)


def compare_with_taifex(opens: dict[str, float]) -> None:
    """拿期交所官方資料對照。數字一致 → nOpen 是日盤；差很多 → 很可能是夜盤。"""
    _head("9. 與期交所官方開盤價對照")
    if not opens:
        _fail("沒有取到任何開盤價，跳過")
        return
    try:
        import requests

        resp = requests.get(TAIFEX_API, timeout=(5, 30))
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001
        _fail(f"取期交所資料失敗：{exc}（不影響上面的結果）")
        return

    official: dict[str, tuple[str, str]] = {}
    for cid in ("TX", "MTX", "TMF"):
        cand = [
            r for r in rows
            if r.get("Contract") == cid
            and r.get("TradingSession") == "一般"
            and str(r.get("ContractMonth(Week)", "")).strip().isdigit()
        ]
        cand.sort(key=lambda r: r["ContractMonth(Week)"])
        if cand:
            official[cid] = (cand[0]["Open"], rows[0].get("Date", "?"))

    pairs = [("TX00", "TX"), ("MTX00", "MTX"), ("TMF00", "TMF")]
    date = next(iter(official.values()))[1] if official else "?"
    _info(f"期交所資料日期：{date}（這支 API 只回最近一個交易日）")
    print(f"\n  {'商品':<8}{'群益 nOpen':>12}{'期交所 Open':>14}  判定")
    all_match = True
    for quote_code, taifex_id in pairs:
        mine = opens.get(quote_code)
        theirs = official.get(taifex_id, (None,))[0]
        if mine is None or theirs is None:
            print(f"  {taifex_id:<8}{'—':>12}{'—':>14}  資料不全")
            continue
        same = abs(mine - float(theirs)) < 0.5
        all_match &= same
        print(f"  {taifex_id:<8}{mine:>12.0f}{float(theirs):>14.0f}  "
              f"{'一致' if same else '!! 不一致'}")

    print()
    if all_match:
        _ok("nOpen 就是「一般時段（日盤）」的開盤價 —— 這正是策略要的")
        _info("⚠️ 但注意：若今天是交易日且已開盤，期交所回的是「昨天」的資料，")
        _info("   本比對只有在收盤後、或非交易日執行才有意義。")
    else:
        _fail("數字對不上！在查清楚原因之前，絕對不要開啟自動下單。")
        _info("可能原因：(a) nOpen 是全盤開盤價（前一日 17:25 夜盤）")
        _info("          (b) 執行時間點造成兩邊日期不同")
        _info("          (c) 取到的不是近月合約")


def main() -> int:
    print("群益 API 環境驗證")
    print("對照文件：docs/LOGIN_SETUP.md")

    if not check_python():
        return 1
    if not check_com_registered():
        return 1
    creds = load_credentials()
    if creds is None:
        return 1

    try:
        run_api_checks(*creds)
    except Exception as exc:  # noqa: BLE001
        _head("發生未預期的錯誤")
        _fail(f"{type(exc).__name__}: {exc}")
        _info("若是 ImportError，檢查有沒有 pip install comtypes pywin32")
        return 1

    _head("結束")
    print("  上面第 7、8、9 節的結果請貼回給我，那是專案目前卡住的兩個問題的答案。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
