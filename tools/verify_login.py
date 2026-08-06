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

# 報價訂閱用的近月連續代碼。
#
# ⚠️ 群益對同一個商品提供兩種代碼：
#     TX00    全盤（含夜盤）—— 開盤價是夜盤開盤，**不是策略要的**
#     TX00AM  AM 盤（日盤）—— 開盤價才是 08:45 那筆
#   這正是 ADR-0002 警告的陷阱。兩個都訂閱下來比對，才能確定誰是誰。
#
# 代碼由第 7 節的商品清單實測確認（2026-08-07）：
#   大台 TX00 / TX00AM      小台 MTX00 / MTX00AM      微台 TM0000 / TM0000AM
# 微台用四個零，與小型電子 ZE0000、小型金融 ZF0000 同格式，不是大台的兩個零。
QUOTE_CODES = ("TX00", "TX00AM", "MTX00", "MTX00AM", "TM0000", "TM0000AM")

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


def _mask(value: str, head: int = 3, tail: int = 3) -> str:
    """遮罩敏感字串，保留頭尾供人辨識。

    這支工具的輸出常被貼進聊天或截圖求助，預設就該是可安全外流的。
    需要完整值時用 --show-account。
    """
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= head + tail:
        return "*" * len(value)
    return value[:head] + "*" * (len(value) - head - tail) + value[-tail:]


def _mask_name(name: str) -> str:
    """姓名遮罩：保留姓，其餘以 ○ 取代。"""
    name = (name or "").strip()
    return name[:1] + "○" * (len(name) - 1) if len(name) > 1 else name


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


def check_com_registered() -> str | None:
    """Step 4-3：檢查 regsvr32 有沒有跑成功。

    回傳註冊時登記的 SKCOM.dll 絕對路徑——後面載入型別程式庫要用它。
    用相對檔名 "SKCOM.dll" 會失敗（TYPE_E_CANTLOADLIBRARY），
    因為 DLL 不在執行目錄下。
    """
    _head("2. COM 元件註冊狀態")
    dll_path = None
    all_ok = True
    for name, clsid in CLSIDS.items():
        key_path = rf"CLSID\{clsid}\InprocServer32"
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
                path = winreg.QueryValueEx(key, "")[0]
            _ok(f"{name} 已註冊 → {path}")
            if not os.path.exists(path):
                _fail("  但該路徑的檔案不存在！元件資料夾被搬走或改名了。")
                _info("  → 回 docs/LOGIN_SETUP.md Step 4，重新複製並註冊")
                all_ok = False
            else:
                dll_path = path
        except FileNotFoundError:
            _fail(f"{name} 未註冊")
            all_ok = False
    if not all_ok:
        _info("→ 以系統管理員身分執行：cd C:\\SKCOM && install.bat")
        return None
    return dll_path


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
    _ok(f"帳號 {_mask(user_id, head=2, tail=2)}（密碼不顯示）")
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


class _CenterEvents:
    """接收聲明書／同意書簽署狀態。

    ⚠️ 這個必須註冊，否則 `EnterMonitorLONG` 會回錯誤 2018
    （SK_WARNING_SIGN_STOCK_OR_FUTURE_API_AGREEMENT_FIRST）——
    但真正的原因不是「沒簽」，而是官方定義的第三種：「**無法取得聲明書狀態**」。
    登入後元件會非同步查詢簽署狀態並由 OnShowAgreement 回傳；沒有接收端就拿不到。
    """

    def __init__(self) -> None:
        self.agreements: list[str] = []

    def OnShowAgreement(self, bstrData):
        self.agreements.append(bstrData)

    def OnTimer(self, nTime):
        pass

    def OnNotifySGXAPIOrderStatus(self, nStatus, bstrOFAccount):
        pass


class _OrderEvents:
    """接收帳號資訊。

    `OnAccount` 的 bstrAccountData 以逗號分隔，欄位依序為
    『市場, 分公司代碼, 分公司, 帳號, 身份證字號, 姓名』；
    期貨的「分公司代碼」即 Broker ID，完整帳號 = 分公司代碼 + 帳號。
    """

    def __init__(self) -> None:
        self.accounts: list[str] = []

    def OnAccount(self, bstrLogInID, bstrAccountData):
        self.accounts.append(bstrAccountData)


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


def run_api_checks(user_id: str, password: str, dll_path: str) -> None:
    """實際連線的部分。COM 在這裡才初始化——模組層級不碰，否則沒裝 COM 的機器連 import 都會失敗。"""
    import comtypes.client

    _head("4. 初始化 COM 物件")
    # 用註冊時登記的絕對路徑，不用相對檔名
    comtypes.client.GetModule(dll_path)
    import comtypes.gen.SKCOMLib as sk  # noqa: E402  (必須在 GetModule 之後)

    center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
    # 把元件自己的 log 導到專案目錄，出事時有東西可查。必須在其他呼叫之前執行。
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    center.SKCenterLib_SetLogPath(log_dir)
    _info(f"元件 log 目錄：{log_dir}")
    reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
    quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
    order = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)
    _ok(f"SKAPI 版本 {center.SKCenterLib_GetSKAPIVersionAndBit('')}")

    def msg_of(code: int) -> str:
        return center.SKCenterLib_GetReturnCodeMessage(code)

    # 註冊公告：官方明訂必須在登入前完成
    reply_events = _ReplyEvents()
    reply_handler = comtypes.client.GetEvents(reply, reply_events)  # noqa: F841
    quote_events = _QuoteEvents()
    quote_handler = comtypes.client.GetEvents(quote, quote_events)  # noqa: F841
    center_events = _CenterEvents()
    center_handler = comtypes.client.GetEvents(center, center_events)  # noqa: F841

    _head("5. 登入")
    # 用 LoginSetQuote 而非 Login：第三個參數 "Y" 明確啟用報價元件。
    # 普通的 SKCenterLib_Login 不帶這個旗標，之後 EnterMonitorLONG 可能拿到 2018。
    code = center.SKCenterLib_LoginSetQuote(user_id, password, "Y")
    if code != 0:
        _info(f"LoginSetQuote 回 {code}（{msg_of(code)}），改用一般登入再試")
        code = center.SKCenterLib_Login(user_id, password)
    if code != 0:
        _fail(f"登入失敗（代碼 {code}）：{msg_of(code)}")
        _info("→ 對照 docs/LOGIN_SETUP.md 的「常見錯誤代碼」表")
        return
    _ok("登入成功（報價元件已啟用）")

    # 順便把期貨帳號查出來——手抄容易錯，而錯了會下到別的帳號
    order_events = _OrderEvents()
    order_handler = comtypes.client.GetEvents(order, order_events)  # noqa: F841
    order.SKOrderLib_Initialize()
    order.GetUserAccount()
    _pump(3)
    if order_events.accounts:
        reveal = "--show-account" in sys.argv
        print()
        print(f"  {'市場':<8}{'完整帳號':<18}姓名")
        for raw in order_events.accounts:
            fields = raw.split(",")
            if len(fields) >= 6:
                account = fields[1] + fields[3]
                shown = account if reveal else _mask(account, head=3, tail=3)
                name = fields[5] if reveal else _mask_name(fields[5])
                print(f"  {fields[0]:<8}{shown:<18}{name}")
        print()
        if reveal:
            _info("⭐ 把【市場為 TF】那筆填進 .env 的 CAPITAL_FUTURES_ACCOUNT")
            _info("⚠️ 目前顯示的是完整帳號，這份輸出不要貼給任何人")
        else:
            _info("帳號已遮罩，此輸出可安全貼給他人求助")
            _info("要看完整帳號填 .env：python tools/verify_login.py --show-account")
    else:
        _info("沒收到帳號資訊——確認「期貨API下單聲明書」已簽署（未簽署不會回傳期貨帳號）")

    # 聲明書狀態：登入後由元件非同步查詢。沒等它回來就連報價主機，
    # 會拿到 2018「無法取得聲明書狀態」——那不是沒簽，是還沒查到。
    center.SKCenterLib_RequestAgreement(user_id)
    _pump_until(lambda: bool(center_events.agreements), 10)
    if center_events.agreements:
        print()
        _info("聲明書狀態：")
        for item in center_events.agreements:
            print(f"           {item}")
    else:
        _info("10 秒內沒收到聲明書狀態通知（可能全部已簽署，元件就不再回傳）")

    _head("6. 連線報價主機")
    code = quote.SKQuoteLib_EnterMonitorLONG()
    # 2018 多半是聲明書狀態尚未就緒，稍等再試一次
    for attempt in range(2):
        if code != 2018:
            break
        _info(f"收到 2018（聲明書狀態未就緒），等 5 秒後重試（第 {attempt + 1} 次）")
        _pump(5)
        code = quote.SKQuoteLib_EnterMonitorLONG()
    if code != 0:
        _fail(f"EnterMonitorLONG 失敗（{code}）：{msg_of(code)}")
        try:
            last_log = center.SKCenterLib_GetLastLogInfo()
            _info(f"元件最後一筆 log：{last_log}")
        except Exception as exc:  # noqa: BLE001
            _info(f"取 log 失敗：{exc}")
        if code == 2018:
            _info("→ 官方對 2018 的三個可能原因：")
            _info("   1. 確認為證券或期貨網路戶")
            _info("   2. 確認未簽署證券API下單聲明書或期貨API下單聲明書")
            _info("   3. 無法取得聲明書狀態（例：Internet 設定不支援 TLS 1.2）")
            _info("→ 第 5 節既然查得到期貨帳號，代表聲明書已簽署，問題在第 3 項")
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
                code, name = fields[0].strip(), fields[1]
                # 只要台指家族：大台 TX、小台 MTX、微台（前綴待確認，用名稱抓）
                is_taiex_family = (
                    code.startswith(("TX", "MTX", "TM"))
                    or "台指" in name
                    or "臺指" in name
                    or "微型" in name
                )
                # 排除電子、金融等其他「小型」商品，它們會把清單淹掉
                if is_taiex_family and not any(k in name for k in ("電子", "金融")):
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
        _info("→ 若錯誤與商品代碼有關，用第 7 步查到的代碼修正 QUOTE_CODES")
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


def _taifex_reference(days: int = 10) -> dict:
    """抓最近幾個交易日的期交所開盤價，一般與盤後兩個時段都要。

    回傳 {(商品, 日期, 時段): 開盤價}。
    只比一天不夠——群益的值可能落在前一日、或落在夜盤那格，
    攤開來看才知道它到底對應哪裡。
    """
    import csv
    import datetime
    import io

    import requests

    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    fmt = "%Y/%m/%d"
    table: dict = {}
    for cid in ("TX", "MTX", "TMF"):
        resp = requests.post(
            "https://www.taifex.com.tw/cht/3/dlFutDataDown",
            headers={"User-Agent": "Mozilla/5.0"},
            data={
                "down_type": "1",
                "commodity_id": cid,
                "commodity_id2": "",
                "queryStartDate": start.strftime(fmt),
                "queryEndDate": end.strftime(fmt),
            },
            timeout=(10, 60),
        )
        text = resp.content.decode("ms950", errors="replace")
        # 期交所超過範圍會回 HTML 但 HTTP 仍是 200——必須驗內容不能只看狀態碼
        if not text.startswith("交易日期"):
            continue
        per: dict = {}
        for row in csv.reader(io.StringIO(text)):
            if len(row) < 18 or row[1].strip() != cid:
                continue
            month = row[2].strip()
            if not month.isdigit():
                continue
            key = (cid, row[0].strip(), row[17].strip())
            # 同一天同時段可能有多個月份，取最小者（近月）
            if key not in per or month < per[key][0]:
                try:
                    per[key] = (month, float(row[3].strip().replace(",", "")))
                except ValueError:
                    continue
        table.update({k: v[1] for k, v in per.items()})
    return table


def compare_with_taifex(opens: dict[str, float]) -> None:
    """把群益每個代碼的開盤價，對到期交所的（日期 × 時段）表格上。

    目的不是「一致 / 不一致」，而是回答：**這個代碼給的是哪一天、哪個盤別的開盤價。**
    """
    _head("9. 與期交所官方開盤價對照")
    if not opens:
        _fail("沒有取到任何開盤價，跳過")
        return
    try:
        table = _taifex_reference()
    except Exception as exc:  # noqa: BLE001
        _fail(f"取期交所資料失敗：{exc}（不影響上面的結果）")
        return
    if not table:
        _fail("期交所沒回資料，跳過")
        return

    dates = sorted({d for (_, d, _) in table}, reverse=True)[:4]
    _info("期交所近期開盤價（一般＝日盤 08:45／盤後＝夜盤）：")
    print(f"\n  {'日期':<12}{'商品':<6}{'一般':>10}{'盤後':>10}")
    for d in sorted(dates):
        for cid in ("TX", "MTX", "TMF"):
            normal = table.get((cid, d, "一般"))
            after = table.get((cid, d, "盤後"))
            if normal is None and after is None:
                continue
            print(f"  {d:<12}{cid:<6}"
                  f"{(f'{normal:.0f}' if normal else '—'):>10}"
                  f"{(f'{after:.0f}' if after else '—'):>10}")

    print(f"\n  {'群益代碼':<12}{'開盤':>10}  對應到期交所的哪一格")
    prefix_to_cid = {"TX00": "TX", "MTX00": "MTX", "TM0000": "TMF"}
    for quote_code, mine in sorted(opens.items()):
        base = quote_code[:-2] if quote_code.endswith("AM") else quote_code
        cid = prefix_to_cid.get(base, base)
        matches = [
            f"{d} {sess}"
            for (c, d, sess), value in table.items()
            if c == cid and abs(value - mine) < 0.5
        ]
        verdict = "、".join(sorted(matches)) if matches else "查無相符（可能是今日尚未公布的盤別）"
        print(f"  {quote_code:<12}{mine:>10.0f}  {verdict}")

    print()
    _info("判讀方式：")
    _info("  帶 AM 的代碼若對到「一般」→ 它就是日盤開盤價，策略要用它")
    _info("  不帶 AM 的代碼若對到「盤後」或查無 → 它是全盤，**不可用於本策略**")


def main() -> int:
    print("群益 API 環境驗證")
    print("對照文件：docs/LOGIN_SETUP.md")

    if not check_python():
        return 1
    dll_path = check_com_registered()
    if dll_path is None:
        return 1
    creds = load_credentials()
    if creds is None:
        return 1

    try:
        run_api_checks(*creds, dll_path=dll_path)
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
