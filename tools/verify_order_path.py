"""下單路徑驗證 —— 走完正式程式的所有準備動作，**但絕不送出委託**。

用法：
    python tools/verify_order_path.py                 # 監聽 5 分鐘
    python tools/verify_order_path.py --listen 900    # 監聽 15 分鐘
    python tools/verify_order_path.py --show-account  # 不遮罩（輸出別外流）

## 為什麼需要這支工具

「下單」不是一個動作，是一條路徑：

    1. 登入
    2. 連報價主機
    3. 初始化下單元件
    4. 連回報主機            <- 成交通知從這條線進來
    5. 查帳號
    6. 組委託單
    7. 送出                  <- 只有這一步會產生真的委託
    8. 等成交回報            <- 從第 4 步那條線收

本工具走第 1~5 步，**跳過第 6、7 步**，然後坐在第 8 步聽。
再加上兩個純查詢函式（查委託、查成交）——那兩個是問問題，不會下單。

## ⚠️ 它驅動的是**正式程式**，不是自己抄一份

第一版自己寫了一套呼叫順序，於是驗到的只是「這個順序可行」，
不是「`CapitalBroker` 那個順序可行」——而後者才是真正會拿去下單的東西。
兩者確實不同：正式流程在下單前會先連報價主機（`main.py` 的進場流程是
登入 → 查商品清單 → 取開盤價 → 下單），第一版跳過了那一步。

現在改成直接呼叫 `CapitalBroker` 的方法，驗證結果才轉移得過去。
代價是失去逐步的成功／失敗訊息，改由例外訊息指出是哪一步（那些訊息本來
就寫得夠清楚）。用私有方法是刻意的：這支工具的職責就是把正式程式的
內部路徑走一遍。

## 安全性

群益 API 能產生委託的只有 `Send*` 系列，正式程式的入口是 `place_order`。
本工具**兩者都不呼叫**，而且不是靠承諾——啟動時掃自己的語法樹確認
（見 `self_check`）。

連回報主機只是**接收**，像把收音機轉到某個頻道：收得到播出的內容，
但沒有在發送。
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

# 捕獲的原始資料存這裡。**內容含交易帳號，不進版控**（見 .gitignore）。
CAPTURE_DIR = os.path.join(_ROOT, "captured")

# 禁止出現在本檔案裡的名稱。前綴 Send 是群益所有送單函式的共同開頭；
# place_order 是正式程式的下單入口（本工具會 import CapitalBroker，
# 所以光擋 Send* 是不夠的）。
_FORBIDDEN_PREFIX = "Send"
_FORBIDDEN_EXACT = frozenset({"place_order"})


# --- 輸出樣式（比照 verify_login.py）---


def _head(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _fail(msg: str) -> None:
    print(f"  [失敗] {msg}")


def _warn(msg: str) -> None:
    """警告不是失敗。`_fail` 印的是 [失敗]，拿它標警語會讓人以為檢查沒過。"""
    print(f"  [注意] {msg}")


def _info(msg: str) -> None:
    print(f"         {msg}")


def _mask(value: str, head: int = 2, tail: int = 2) -> str:
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

    ⚠️ **必須能事後追加。** 第一版在 main 裡一次建好就固定了，於是 `.env` 沒填
    帳號、改由 `OnAccount` 撈出來的那條路（**也就是第一次跑的人一定會走的路**）
    完全沒被遮到——帳號連同後續每一列回報的 CustNo 全部明文印出。
    """

    def __init__(self, enabled: bool = True):
        self._secrets: list = []
        self._enabled = enabled

    def add(self, *values: str) -> None:
        for value in values:
            value = (value or "").strip()
            if len(value) >= 4 and value not in self._secrets:
                self._secrets.append(value)

    def __call__(self, text: str) -> str:
        if not self._enabled:
            return text
        for secret in self._secrets:
            text = text.replace(secret, _mask(secret))
        return text


# --- 自我檢查 ---


def self_check() -> bool:
    """掃自己的語法樹，確認沒有**提到**任何送單函式或 `place_order`。

    用語法樹而不是文字比對，是因為文字比對會被自己的說明文件誤判——
    第一版就是這樣：docstring 裡舉了例子，結果對著自己的註解報警。

    ⚠️ **只看「呼叫」是不夠的**（第二版的漏洞）。本檔案已經有把函式塞進表格、
    之後才呼叫的寫法：

        for label, fn, desc in (("GetOrderReport", order.GetOrderReport, ...

    照著這個既有風格多加一列送單函式，語法樹上它是 `Attribute` 而不是 `Call`
    （真正的呼叫是 `fn(...)`，而 `fn` 只是變數名），只檢查 `Call` 就會放行。
    這不是刻意繞過，是**照抄現有寫法就會中**。所以改成掃所有 `Attribute`
    與 `Name` 節點——只要名字出現就算違規。

    ⚠️ 已知極限：動態組出來的名稱躲得過。這裡不防那個——目的是防
    「不小心加進去」，不是防惡意。
    """
    _head("0. 自我檢查：本工具不得送出任何委託")
    with open(__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), __file__)

    found = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        if name and (name.startswith(_FORBIDDEN_PREFIX) or name in _FORBIDDEN_EXACT):
            found.append(f"{name}（第 {node.lineno} 行）")

    if found:
        _fail(f"語法樹中出現送單相關名稱：{found}")
        _fail("這支工具的前提被破壞了，拒絕執行")
        return False
    _ok(f"語法樹中沒有 {_FORBIDDEN_PREFIX}* 或 {sorted(_FORBIDDEN_EXACT)} 這些名稱")
    _info("掃的是所有 Attribute／Name 節點，不只是呼叫——塞進表格再呼叫也擋得住")
    return True


# --- COM 事件：本工具額外掛的接收端 ---


class _AccountEvents:
    """帳號接收端。

    `CapitalBroker` 本身**沒有**掛 `OnAccount`（它只用 `.env` 指定的帳號），
    所以這是工具額外掛的。`bstrAccountData` 欄位依序為
    『市場, 分公司代碼, 分公司, 帳號, 身份證字號, 姓名』；
    期貨完整帳號 = 分公司代碼 + 帳號。
    """

    def __init__(self) -> None:
        self.accounts: list = []
        self.handler = None      # 事件 handler 要留著，被回收就收不到事件

    def OnAccount(self, bstrLogInID, bstrAccountData):
        self.accounts.append(bstrAccountData)


# --- 各段落 ---


def walk_production_path(broker, account_events, redact) -> bool:
    """依**正式程式的順序**走完下單前的所有準備動作。

    對照 `main.py` 的進場流程：登入 → 查商品清單（會連報價主機）→ 取開盤價
    → 下單。所以連報價主機排在下單準備之前，這裡照著走。
    """
    import comtypes.client

    from broker import OrderFailed, QuoteNotReady

    _head("1. 登入（CapitalBroker.login）")
    try:
        broker.login()
    except Exception as exc:  # noqa: BLE001
        _fail(f"{type(exc).__name__}: {redact(str(exc))}")
        _info("→ 先跑 tools/verify_login.py 排除環境問題")
        return False
    _ok("登入成功")

    # login() 之後 COM 物件才存在，這時才掛得上 OnAccount
    account_events.handler = comtypes.client.GetEvents(broker._order, account_events)

    _head("2. 連報價主機（正式流程在下單前會先做這一步）")
    try:
        broker._ensure_quote_connection()
    except QuoteNotReady as exc:
        _fail(f"連報價主機失敗：{redact(str(exc))}")
        return False
    _ok("報價主機連線完成")

    _head("3. 下單前置路徑（CapitalBroker._ensure_order_ready）")
    _info("初始化下單元件 → 連回報主機 → 查帳號")
    try:
        broker._ensure_order_ready()
    except OrderFailed as exc:
        _fail(redact(str(exc)))
        _info("→ 訊息本身會指出是三步中的哪一步")
        return False
    _ok("三步都回傳成功")

    # ⚠️ 回傳 0 只代表「請求已受理」。回報主機的連線結果是**非同步**回來的
    #    （官方文件把 OnSolaceReplyConnection / OnComplete 列為它的通知事件），
    #    所以回傳 0 不等於「連上了」。
    connected = getattr(broker._reply_events, "connected", None)
    if connected is True:
        _ok("回報主機已回報連線成功")
    else:
        _warn("回傳 0 只代表請求已受理，**不等於連上了**")
        _info(f"（CapitalBroker 目前沒有記錄回報連線狀態：connected={connected}）")
        _info("→ 真正能證明這條線活著的是收到 OnNewData，見第 6 段")
    return True


def pick_futures_account(account_events, env_account: str, redact) -> str:
    _head("4. 期貨帳號")
    if env_account:
        _ok(f".env 指定：{redact(env_account)}")
        return env_account

    for raw in account_events.accounts:
        fields = raw.split(",")
        if len(fields) >= 6 and fields[0].strip() == "TF":
            full = fields[1].strip() + fields[3].strip()
            # **先加進遮罩再印**，連同身分證字號那一欄
            redact.add(full, fields[4].strip())
            _ok(f"自 OnAccount 取得（市場 TF）：{redact(full)}")
            _info("→ 建議填進 .env 的 CAPITAL_FUTURES_ACCOUNT，避免每次自動挑")
            return full

    _fail("找不到市場別為 TF 的期貨帳號")
    return ""


def query_reports(broker, user_id: str, account: str, redact) -> None:
    """兩個**純查詢**函式。它們不會下單，只是問問題。"""
    _head("5. 查詢函式（純查詢，ticket 09 要用）")
    if not account:
        _fail("沒有帳號，跳過")
        return

    order = broker._order
    for label, fn, desc in (
        ("GetOrderReport", order.GetOrderReport, "委託回報查詢（格式 1=全部）"),
        ("GetFulfillReport", order.GetFulfillReport, "成交回報查詢（格式 1=完整）"),
    ):
        print()
        _info(f"--- {label}：{desc} ---")
        started = time.perf_counter()
        try:
            result = fn(user_id, account, 1)
        except Exception as exc:  # noqa: BLE001
            _fail(f"{label} 拋出例外：{type(exc).__name__}: {redact(str(exc))}")
            continue
        elapsed = time.perf_counter() - started

        text = (result or "").strip()
        # ticket 09 的驗收條件之一：「阻塞時間實測多久」
        _info(f"阻塞 {elapsed:.2f} 秒（文件註明此查詢為阻塞式）")

        if not text:
            _info("（回傳空字串）")
        elif text.startswith("M003"):
            _ok(f"可呼叫，目前查無資料：{text!r}")
        elif text.startswith("M999"):
            _fail(f"查詢錯誤：{text}")
        else:
            lines = text.splitlines()
            _ok(f"有資料，共 {len(lines)} 列")
            for line in lines[:5]:
                print(f"           {redact(line)}")
            if len(lines) > 5:
                _info("（只顯示前 5 列，完整內容已存檔）")
        _save(f"{label}.txt", text)
        # 文件明載「限制每次查詢間需間隔五秒」
        time.sleep(5.5)


def listen(broker, seconds: float, redact) -> int:
    """坐在回報頻道上聽。**這是本工具最有價值的部分。**"""
    import pythoncom

    from broker.capital import parse_reply_row

    _head("6. 監聽成交回報")
    _info(f"監聽 {seconds:.0f} 秒（Ctrl+C 可提早結束）")
    print()
    _info("👉 現在請在群益 APP 隨便下一筆單（1 口微台進出即可）")
    _info("   你本來就要做的交易也算數——本工具只是在旁邊聽，不影響那筆交易")
    print()

    rows = broker._reply_events.rows
    seen = len(rows)          # 只看從現在起新到的
    shown = 0
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            pythoncom.PumpWaitingMessages()
            while seen < len(rows):
                raw = rows[seen]
                seen += 1
                shown += 1
                print(f"\n  ── 第 {shown} 則  {datetime.now():%H:%M:%S} " + "─" * 28)
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
    if shown:
        _ok(f"共收到 {shown} 則回報——回報連線確實是活的")
    else:
        _info("這段期間沒有收到任何回報")
        _info("→ 不代表連線有問題；沒有交易就不會有回報。**但也還沒證明它是活的**")
    return shown


def _save(name: str, text: str) -> str:
    """存檔。**存的是未遮罩的原始內容**，所以每次都要講一次。"""
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(CAPTURE_DIR, f"{stamp}-{name}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text or "")
    _warn(f"已存至 {os.path.relpath(path, _ROOT)}（**未遮罩**，勿外流）")
    return path


def save_replies(broker) -> None:
    _head("7. 存檔")
    rows = broker._reply_events.rows
    if not rows:
        _info("沒有回報可存")
        return
    _save("OnNewData.txt", "\n".join(rows))
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
                        help="不遮罩（輸出請勿外流）")
    args = parser.parse_args()

    if not self_check():
        return 1

    import settings
    from broker.capital import CapitalBroker

    user_id = settings.read_env("CAPITAL_USER_ID")
    password = settings.read_env("CAPITAL_PASSWORD")
    if not user_id or not password:
        _fail("找不到 CAPITAL_USER_ID / CAPITAL_PASSWORD，請檢查 .env")
        return 1
    env_account = settings.read_env("CAPITAL_FUTURES_ACCOUNT")

    redact = _Redactor(enabled=not args.show_account)
    redact.add(user_id, env_account)
    if args.show_account:
        _warn("已關閉遮罩，這次的輸出請勿外流")

    config = settings.load()
    broker = CapitalBroker(
        user_id, password,
        environment=config.capital_environment,
        account=env_account,
    )
    account_events = _AccountEvents()

    try:
        if not walk_production_path(broker, account_events, redact):
            return 1
        account = pick_futures_account(account_events, env_account, redact)
        query_reports(broker, user_id, account, redact)
        listen(broker, args.listen, redact)
        save_replies(broker)
    except Exception as exc:  # noqa: BLE001
        _head("發生未預期的錯誤")
        _fail(f"{type(exc).__name__}: {redact(str(exc))}")
        _info("→ 若是 COM 相關，先跑 tools/verify_login.py 確認環境")
        import traceback
        traceback.print_exc()
        return 1

    _head("完成")
    _ok("全程未呼叫任何送單函式")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
