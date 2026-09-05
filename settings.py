"""設定載入。

兩個設計原則：

1. **缺 key 就大聲失敗，不套用預設值。** 靜默的預設值會讓「設定沒生效」變成
   要跑到線上才發現的問題，而這個系統的線上代表真錢。

2. **設定與程式不得漂移，且由結構保證而非人工維護清單。** `build()` 讀了哪些 key
   是被追蹤出來的，不是抄一份在旁邊——抄的那份遲早會忘記更新。
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass

import yaml
import paths

_ROOT = paths.app_root()
_SETTINGS_PATH = os.path.join(_ROOT, "config", "settings.yaml")
_ENV_PATH = os.path.join(_ROOT, ".env")



# 期交所對**每筆市價委託**的口數上限：一般交易時段 10 口、盤後 5 口
# （自 108/5/27 起）。本策略只在日盤交易，所以用 10。
# 我們送的是市價 IOC（ADR-0003），因此這個上限直接適用。
MARKET_ORDER_LOT_CAP = 10

# 欄位名 → 設定檔裡的 key。錯誤訊息要講使用者看得到的那個名字，
# 不是程式內部的欄位名——他要去編輯的是 yaml。
_YAML_KEYS = {
    "auto_order_enabled": "order.auto_enabled",
    "discord_enabled": "discord.enabled",
}


@dataclass(frozen=True)
class Config:
    discord_enabled: bool
    quote_retry_attempts: int
    quote_retry_interval_seconds: int
    # 自動下單。**關閉時程式完全不碰下單 API**，只發訊號。
    auto_order_enabled: bool
    order_product: str          # 報價代碼，必須是 broker.PRODUCT_CODES 之一
    order_lots: int
    # 送出委託後等成交回報的秒數。逾時 → 記為「不確定」並要求人工確認。
    # 進場的時間界線。過了它就**不下單**（訊號與觀測照常）。
    # **`None` 代表不設限**——什麼時候跑都下單。
    # 排程設了「錯過就補跑」，而程式本身沒有時鐘——停電或強制更新重開之後，
    # 進場那班會在電腦回來的那一刻觸發並用市價送單。最壞的情況是它拖到
    # 13:40 之後才跑：出場那班早就看過「沒有記錄」而靜默結束了，
    # 然後這裡開一個部位、寫下狀態檔，**再也沒有東西會去平它**。
    entry_cutoff: datetime.time | None

    def __post_init__(self) -> None:
        """設定錯誤要在**載入時**就炸，不可以偽裝成執行期的「今日無訊號」。

        放在這裡而不是 `build()`，是因為測試會直接建構 Config——
        兩條路都要被擋住，否則守不住的那條遲早會被用上。
        """
        from broker import PRODUCT_CODES

        # ⚠️ 開關必須是真正的布林值。yaml 的 `auto_enabled: "false"` 會解析成
        #    字串 "false"，而非空字串一律為真——使用者以為開關關著，程式卻照常
        #    送出真單。與 ticket 03 那個「加引號的日期被靜默無視」同一類錯誤，
        #    但這個方向的代價大得多。
        for name in ("auto_order_enabled", "discord_enabled"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise ValueError(
                    f"{_YAML_KEYS[name]} 必須是 true 或 false（不加引號），"
                    f"目前是 {value!r}（型別 {type(value).__name__}）"
                )

        if self.quote_retry_attempts < 1:
            raise ValueError(
                f"quote.retry_attempts 必須 ≥ 1，目前是 {self.quote_retry_attempts}"
            )
        if self.quote_retry_interval_seconds < 0:
            raise ValueError(
                f"quote.retry_interval_seconds 不可為負，目前是 "
                f"{self.quote_retry_interval_seconds}"
            )
        if self.order_lots < 1:
            raise ValueError(f"order.lots 必須 ≥ 1，目前是 {self.order_lots}")
        if self.order_lots > MARKET_ORDER_LOT_CAP:
            # 我們送的是市價 IOC（ADR-0003），而期交所對每筆市價委託有口數上限。
            # 超過會被退單，而退單訊息看不出是這個原因。
            raise ValueError(
                f"order.lots 不可超過 {MARKET_ORDER_LOT_CAP}，目前是 {self.order_lots}。"
                "期交所限制每筆市價委託口數（一般交易時段 10 口），超過會被退單。"
                "要下更多口需要改成分批送單或改用限價，那是另一個設計決定。"
            )
        if self.entry_cutoff is not None and not isinstance(self.entry_cutoff, datetime.time):
            raise ValueError(
                f"order.entry_cutoff 必須是 datetime.time 或 None，目前是 "
                f"{self.entry_cutoff!r}（型別 {type(self.entry_cutoff).__name__}）"
            )
        if self.order_product not in PRODUCT_CODES:
            raise ValueError(
                f"order.product 必須是 {list(PRODUCT_CODES)} 之一，"
                f"目前是 {self.order_product!r}"
            )



def read_raw(path: str = _SETTINGS_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def flatten_keys(data: dict, prefix: str = "") -> set:
    """把巢狀 dict 的 key 攤平成 {"a.b", "a.c"}。"""
    keys = set()
    for key, value in data.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= flatten_keys(value, prefix=f"{full}.")
        else:
            keys.add(full)
    return keys


class _Tracked:
    """記錄自己被讀過哪些葉節點 key 的唯讀 mapping。

    存在的唯一理由是讓 `consumed_keys()` 能問出「build() 到底用了哪些設定」，
    不必手寫一份清單擺在旁邊等著過期。
    """

    def __init__(self, data: dict, prefix: str = "", seen: set | None = None):
        self._data = data
        self._prefix = prefix
        self.seen = set() if seen is None else seen

    def __getitem__(self, key):
        full = f"{self._prefix}{key}"
        value = self._data[key]
        if isinstance(value, dict):
            return _Tracked(value, prefix=f"{full}.", seen=self.seen)
        self.seen.add(full)
        return value



_NO_CUTOFF = "none"


def _parse_clock(value, where: str) -> datetime.time | None:
    """把 `"09:00"` 解析成 `datetime.time`。

    ⚠️ **YAML 對時間有個會咬人的陷阱：`9:00` 不是字串，是整數 540。**
       YAML 1.1 把 `分:秒` 當成六十進位數字，而**有沒有前導零決定了型別**——
       `09:00` 是字串、`9:00` 是 540。使用者手改設定時很容易寫掉那個零，
       而 540 不會在載入時出錯，只會讓時間關卡變成一個看不懂的東西。
       所以這裡收到數字時要指名道姓地講出原因。
    """
    if isinstance(value, bool):
        # YAML 的 `yes` / `on` / `true` 都是 bool。與下面那個數字的判斷分開，
        # 因為原因完全不同——講六十進位對這裡是答非所問。
        # ⚠️ 順序也不能反：`bool` 是 `int` 的子類，先判斷數字的話這一段永遠到不了。
        raise ValueError(f'{where} 讀到 {value!r}，要的是時間，例如 "09:00"')
    if isinstance(value, (int, float)):
        raise ValueError(
            f"{where} 讀到數字 {value!r} 而不是時間。"
            "YAML 把 `9:00` 當成六十進位數字（= 540），要加前導零或用引號："
            f'寫成 "09:00"'
        )
    if isinstance(value, datetime.time):
        return value
    if isinstance(value, str) and value.strip().lower() == _NO_CUTOFF:
        # **關掉一道安全關卡必須是打得出來的字。** 做成「留白＝關掉」的話，
        # 任何一次手滑刪掉值都會靜靜地把它關掉——而它擋的是
        # 「補跑的那一班在中午開倉，然後沒有東西會去平它」。
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"{where} 必須是 HH:MM 格式的字串，目前是 {value!r}"
            f"（型別 {type(value).__name__}）"
        )
    try:
        hour, _, minute = value.strip().partition(":")
        parsed = datetime.time(int(hour), int(minute))
    except (TypeError, ValueError):
        raise ValueError(f"{where} 看不懂：{value!r}，要的是 HH:MM，例如 \"09:00\"") from None
    return parsed


def build(raw) -> Config:
    """從原始 mapping 組出 Config。缺 key 會拋 KeyError，不靜默補預設值。"""
    return Config(
        discord_enabled=raw["discord"]["enabled"],
        quote_retry_attempts=raw["quote"]["retry_attempts"],
        quote_retry_interval_seconds=raw["quote"]["retry_interval_seconds"],
        auto_order_enabled=raw["order"]["auto_enabled"],
        order_product=raw["order"]["product"],
        order_lots=raw["order"]["lots"],
        entry_cutoff=_parse_clock(raw["order"]["entry_cutoff"], "order.entry_cutoff"),
    )


def consumed_keys(raw: dict) -> set:
    """實際被 `build()` 讀取到的設定 key，供漂移檢查使用。"""
    tracked = _Tracked(raw)
    build(tracked)
    return tracked.seen


def load(path: str = _SETTINGS_PATH) -> Config:
    return build(read_raw(path))


def read_env(name: str, path: str = _ENV_PATH) -> str:
    """從 .env 讀單一變數。刻意不引入 dotenv——只為了讀幾個字串不值得多一個相依。

    找不到檔案或找不到變數都回傳空字串，由呼叫端決定那是不是錯誤。
    """
    if not os.path.exists(path):
        return ""
    prefix = f"{name}="
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(prefix):
                return line[len(prefix):].strip()
    return ""
