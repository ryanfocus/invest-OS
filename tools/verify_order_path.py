"""下單路徑驗證 —— 走完所有準備動作，**但絕不送出委託**。

用法：
    python tools/verify_order_path.py               # 監聽 5 分鐘
    python tools/verify_order_path.py --listen 900  # 監聽 15 分鐘
    python tools/verify_order_path.py --show-account  # 不遮罩（輸出別外流）

## 為什麼需要這支工具

「下單」不是一個動作，是一條路徑：

    1. 初始化下單元件
    2. 連上回報主機          <- 成交通知從這條線進來
    3. 查出可用的期貨帳號
    4. 組出委託單
    5. 送出                  <- 只有這一步會產生真的委託
    6. 等成交回報            <- 從第 2 步那條線收

本工具走第 1、2、3 步，**跳過第 4、5 步**，然後坐在第 6 步聽。
再加上兩個純查詢函式（查委託、查成交）——那兩個是問問題，不會下單。

它回答三個目前完全不知道的問題：

  1. 第 2 步「連回報主機」在這個專案裡**從來沒有執行過一次**。
     若它需要另外開通權限，現在的設計要等到第一次真單送出、
     然後永遠等不到回報才會發現——那時錢已經花了。

  2. 成交回報的**欄位位置**是從官方文件的排列推導出來的，沒有實機驗證。
     推錯的後果：記下錯的成交口數 -> 下午平錯量 -> 開出反向新倉。
     回報主機是廣播式的，**你在群益 APP 手動下的任何一筆單都會推過來**，
     所以不必特地為它下單。

  3. GetOrderReport / GetFulfillReport 的回傳格式（ticket 09 要用）。

## 安全性

群益整個 API 裡能產生委託的函式只有 `Send*` 系列。本工具**一個都不呼叫**，
而且這件事不是靠承諾——啟動時會掃自己的原始碼確認（見 `self_check`）。

連上回報主機只是**接收**，像把收音機轉到某個頻道：收得到播出的內容，
但沒有在發送。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

# 捕獲的原始回報存這裡。**內容含交易帳號，不進版控**（見 .gitignore）。
CAPTURE_DIR = os.path.join(_ROOT, "captured")

# --- 輸出樣式（比照 verify_login.py）---


def _head(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _fail(msg: str) -> None:
    print(f"  [失敗] {msg}")


def _info(msg: str) -> None:
    print(f"         {msg}")


def _mask(value: str, head: int = 3, tail: int = 3) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= head + tail:
        return "*" * len(value)
    return value[:head] + "*" * (len(value) - head - tail) + value[-tail:]


class _Redactor:
    """把已知的機密字串（帳號、身分證字號）從輸出中換掉。

    刻意用「整串取代」而不是「遮某一欄」——欄位位置正是這支工具要查的東西，
    還不知道的時候不能拿它當遮罩的依據。
    """

    def __init__(self, secrets, enabled: bool = True):
        self._secrets = [s for s in secrets if s and len(s) >= 4]
        self._enabled = enabled

    def __call__(self, text: str) -> str:
        if not self._enabled:
            return text
        for secret in self._secrets:
            text = text.replace(secret, _mask(secret, head=2, tail=2))
        return text


# --- 自我檢查：確認本檔案不呼叫任何送單函式 ---


def self_check() -> bool:
    """掃自己的**語法樹**，確認沒有呼叫任何名稱以 `Send` 開頭的函式。

    這是本工具「零風險」宣稱的機器可驗證版本。人可以忘記、可以改壞，
    但這段每次啟動都會跑一次。

    用語法樹而不是文字比對，是因為文字比對會被自己的說明文件誤判——
    第一版就是這樣：docstring 裡寫了 `.SendXxx(` 當例子，結果自我檢查
    對著自己的註解報警。語法樹只看**真正的呼叫**。

    ⚠️ 已知極限：動態呼叫（`getattr(order, "Send...")()`）躲得過這個檢查。
    這裡不防那個——真要那樣寫的人不會被一個自我檢查擋住，
    而這段的目的是防止「不小心加進去」，不是防惡意。
    """
    import ast

    _head("0. 自我檢查：本工具不得送出任何委託")
    with open(__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), __file__)

    called = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name and name.startswith("Send"):
            called.append(f"{name}（第 {node.lineno} 行）")

    if called:
        _fail(f"語法樹中發現送單呼叫：{called}")
        _fail("這支工具的前提被破壞了，拒絕執行")
        return False
    _ok("語法樹中沒有任何 Send* 呼叫")
    _info("群益 API 能產生委託的只有 Send* 系列，一個都沒被呼叫")
    return True


# --- 設定讀取 ---


def read_env() -> dict:
    path = os.path.join(_ROOT, ".env")
    if not os.path.exists(path):
        return {}
    values = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def _resolve_dll_path() -> str:
    import winreg

    key_path = r"CLSID\{AC30BAB5-194A-4515-A8D3-6260749F8577}\InprocServer32"
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
        return winreg.QueryValueEx(key, "")[0]


def _pump(seconds: float) -> None:
    """COM 事件要有訊息幫浦才會觸發。"""
    import pythoncom

    deadline = time.time() + seconds
    while time.time() < deadline:
        pythoncom.PumpWaitingMessages()
        time.sleep(0.05)


# --- COM 事件接收端 ---


class _ReplyEvents:
    """回報接收端。`OnNewData` 就是我們來抓的東西。"""

    def __init__(self) -> None:
        self.rows: list = []
        self.connected = False

    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1        # 官方要求回 -1

    def OnNewData(self, bstrUserID, bstrData):
        self.rows.append((datetime.now(), bstrData))

    def OnConnect(self, bstrUserID, nErrorCode):
        self.connected = nErrorCode == 0

    def OnDisconnect(self, bstrUserID, nErrorCode):
        self.connected = False

    def OnComplete(self, bstrUserID):
        pass

    def OnSolaceReplyConnection(self, bstrUserID, nErrorCode):
        pass


class _CenterEvents:
    def OnShowAgreement(self, bstrData):
        pass

    def OnTimer(self, nTime):
        pass

    def OnNotifySGXAPIOrderStatus(self, nStatus, bstrOFAccount):
        pass


class _OrderEvents:
    """帳號接收端。`bstrAccountData` 欄位依序為
    『市場, 分公司代碼, 分公司, 帳號, 身份證字號, 姓名』；
    期貨完整帳號 = 分公司代碼 + 帳號。
    """

    def __init__(self) -> None:
        self.accounts: list = []

    def OnAccount(self, bstrLogInID, bstrAccountData):
        self.accounts.append(bstrAccountData)


# --- 各檢查段落 ---


def login(center, user_id: str, password: str) -> bool:
    _head("1. 登入")
    code = center.SKCenterLib_LoginSetQuote(user_id, password, "Y")
    if code != 0:
        _fail(f"登入失敗（代碼 {code}）：{center.SKCenterLib_GetReturnCodeMessage(code)}")
        _info("→ 先跑 tools/verify_login.py 排除環境問題")
        return False
    _ok("登入成功")
    center.SKCenterLib_RequestAgreement(user_id)
    _pump(2)
    return True


def prepare_order_path(center, order, reply, order_events, user_id: str):
    """下單前置三步。**這三步在 CapitalBroker 裡從來沒有整條跑過。**"""
    _head("2. 下單前置路徑（這是本工具的主要目的之一）")
    msg = center.SKCenterLib_GetReturnCodeMessage

    code = order.SKOrderLib_Initialize()
    if code != 0:
        _fail(f"[1/3] SKOrderLib_Initialize 失敗（{code}）：{msg(code)}")
        return None
    _ok(f"[1/3] SKOrderLib_Initialize")

    # ⚠️ 這一行是整個專案從來沒有執行過的那一步
    code = reply.SKReplyLib_ConnectByID(user_id)
    if code != 0:
        _fail(f"[2/3] SKReplyLib_ConnectByID 失敗（{code}）：{msg(code)}")
        _info("→ 這條線接不上的話，真單送出去也收不到成交回報")
        return None
    _pump(3)
    _ok(f"[2/3] SKReplyLib_ConnectByID（回報主機）")

    code = order.GetUserAccount()
    if code != 0:
        _fail(f"[3/3] GetUserAccount 失敗（{code}）：{msg(code)}")
        return None
    _pump(2)
    _ok(f"[3/3] GetUserAccount，收到 {len(order_events.accounts)} 筆帳號")
    return order_events.accounts


def pick_futures_account(accounts, env_account: str, redact) -> str:
    _head("3. 期貨帳號")
    if env_account:
        _ok(f".env 指定：{redact(env_account)}")
        return env_account

    for raw in accounts:
        fields = raw.split(",")
        if len(fields) >= 6 and fields[0].strip() == "TF":
            full = fields[1].strip() + fields[3].strip()
            _ok(f"自 OnAccount 取得（市場 TF）：{redact(full)}")
            _info("→ 建議填進 .env 的 CAPITAL_FUTURES_ACCOUNT，避免每次自動挑")
            return full

    _fail("找不到市場別為 TF 的期貨帳號")
    return ""


def query_reports(order, user_id: str, account: str, redact) -> None:
    """兩個**純查詢**函式。它們不會下單，只是問問題。"""
    _head("4. 查詢函式（純查詢，ticket 09 要用）")
    if not account:
        _fail("沒有帳號，跳過")
        return

    for name, fn, fmt, desc in (
        ("GetOrderReport", order.GetOrderReport, 1, "委託回報查詢（1=全部）"),
        ("GetFulfillReport", order.GetFulfillReport, 1, "成交回報查詢（1=完整）"),
    ):
        print()
        _info(f"--- {name}：{desc} ---")
        try:
            result = fn(user_id, account, fmt)
        except Exception as exc:  # noqa: BLE001
            _fail(f"{name} 拋出例外：{type(exc).__name__}: {exc}")
            continue
        text = (result or "").strip()
        if not text:
            _info("（回傳空字串）")
        elif text.startswith("M003"):
            _ok(f"{name} 可呼叫，目前查無資料：{text}")
            _info("→ 呼叫路徑通、錯誤格式已確認。有交易紀錄時再跑一次就能看到真實格式")
        elif text.startswith("M999"):
            _fail(f"{name} 查詢錯誤：{text}")
        else:
            _ok(f"{name} 有資料，共 {len(text.splitlines())} 列")
            for line in text.splitlines()[:5]:
                print(f"           {redact(line)}")
            if len(text.splitlines()) > 5:
                _info(f"（只顯示前 5 列，完整內容已存檔）")
        _save(f"{name}.txt", text)
        # 文件明載「限制每次查詢間需間隔五秒」
        time.sleep(5.5)


def listen(reply_events, seconds: float, redact) -> None:
    """坐在回報頻道上聽。**這是本工具最有價值的部分。**"""
    import pythoncom

    from broker.capital import parse_reply_row

    _head("5. 監聽成交回報")
    _info(f"監聽 {seconds:.0f} 秒（Ctrl+C 可提早結束）")
    print()
    _info("👉 現在請在群益 APP 隨便下一筆單（1 口微台進出即可）")
    _info("   你本來就要做的交易也算數——本工具只是在旁邊聽，不影響那筆交易")
    print()

    seen = 0
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            pythoncom.PumpWaitingMessages()
            while seen < len(reply_events.rows):
                stamp, raw = reply_events.rows[seen]
                seen += 1
                print(f"\n  ── 第 {seen} 則  {stamp:%H:%M:%S} " + "─" * 30)
                print(f"  原始：{redact(raw)}")
                parsed = parse_reply_row(raw)
                if parsed is None:
                    print("  解讀：**認不出來**（形狀檢查沒過）")
                    print("        → 這代表我推的欄位位置可能是錯的，正是要找的資訊")
                else:
                    print(f"  解讀：序號={parsed.seq} 型態={parsed.type} "
                          f"失敗={parsed.failed} 數量={parsed.qty}")
                    print("        → 請人工對照原始字串，確認「數量」抓到的真的是成交口數")
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n  （已中斷）")

    print()
    if seen:
        _ok(f"共收到 {seen} 則回報")
    else:
        _info("這段期間沒有收到任何回報")
        _info("→ 不代表連線有問題；沒有交易就不會有回報。下次交易時再跑一次即可")


def _save(name: str, text: str) -> str:
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(CAPTURE_DIR, f"{stamp}-{name}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text or "")
    return path


def save_captures(reply_events) -> None:
    _head("6. 存檔")
    if not reply_events.rows:
        _info("沒有回報可存")
        return
    body = "\n".join(raw for _stamp, raw in reply_events.rows)
    path = _save("OnNewData.txt", body)
    _ok(f"原始回報已存至 {path}")
    print()
    _fail("⚠️ 這個檔案**未經遮罩**，內含交易帳號。captured/ 已在 .gitignore 內，")
    _info("   但不要把它貼進聊天室、issue 或截圖")
    print()
    _info("下一步（ticket 04 里程碑 1 的交付物）：")
    _info("  把其中一列去識別化後放進 tests/，改寫 test_reply_parsing.py——")
    _info("  該檔案現在誠實寫著「不寫欄位位置的測試，因為期望值來自自己的推導」，")
    _info("  有了真實資料才寫得出真的會失敗的斷言")


def main() -> int:
    parser = argparse.ArgumentParser(description="驗證下單路徑，不送出任何委託")
    parser.add_argument("--listen", type=float, default=300,
                        help="監聽回報的秒數（預設 300）")
    parser.add_argument("--show-account", action="store_true",
                        help="不遮罩帳號（輸出請勿外流）")
    args = parser.parse_args()

    if not self_check():
        return 1

    env = read_env()
    user_id = env.get("CAPITAL_USER_ID", "")
    password = env.get("CAPITAL_PASSWORD", "")
    if not user_id or not password:
        _fail("找不到 CAPITAL_USER_ID / CAPITAL_PASSWORD，請檢查 .env")
        return 1

    env_account = env.get("CAPITAL_FUTURES_ACCOUNT", "")
    redact = _Redactor([user_id, env_account], enabled=not args.show_account)
    if args.show_account:
        _fail("⚠️ 已關閉遮罩，這次的輸出請勿外流")

    import comtypes.client

    comtypes.client.GetModule(_resolve_dll_path())
    import comtypes.gen.SKCOMLib as sk

    center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
    reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
    order = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)

    reply_events, order_events = _ReplyEvents(), _OrderEvents()
    handlers = [                                        # noqa: F841 —— 被回收就收不到事件
        comtypes.client.GetEvents(reply, reply_events),
        comtypes.client.GetEvents(center, _CenterEvents()),
        comtypes.client.GetEvents(order, order_events),
    ]

    if not login(center, user_id, password):
        return 1
    accounts = prepare_order_path(center, order, reply, order_events, user_id)
    if accounts is None:
        return 1

    account = pick_futures_account(accounts, env_account, redact)
    query_reports(order, user_id, account, redact)
    listen(reply_events, args.listen, redact)
    save_captures(reply_events)

    _head("完成")
    _ok("全程未呼叫任何送單函式")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
