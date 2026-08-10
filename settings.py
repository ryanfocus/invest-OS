"""設定載入。

兩個設計原則：

1. **缺 key 就大聲失敗，不套用預設值。** 靜默的預設值會讓「設定沒生效」變成
   要跑到線上才發現的問題，而這個系統的線上代表真錢。

2. **設定與程式不得漂移，且由結構保證而非人工維護清單。** `build()` 讀了哪些 key
   是被追蹤出來的，不是抄一份在旁邊——抄的那份遲早會忘記更新。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import yaml

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SETTINGS_PATH = os.path.join(_ROOT, "config", "settings.yaml")
_ENV_PATH = os.path.join(_ROOT, ".env")


@dataclass(frozen=True)
class Config:
    discord_enabled: bool
    quote_retry_attempts: int
    quote_retry_interval_seconds: int
    # 臨時休市（颱風假）與臨時開市（補班日）。holidays 套件不知道這兩種。
    calendar_extra_closures: frozenset = frozenset()
    calendar_extra_openings: frozenset = frozenset()

    def __post_init__(self) -> None:
        """設定錯誤要在**載入時**就炸，不可以偽裝成執行期的「今日無訊號」。

        放在這裡而不是 `build()`，是因為測試會直接建構 Config——
        兩條路都要被擋住，否則守不住的那條遲早會被用上。
        """
        if self.quote_retry_attempts < 1:
            raise ValueError(
                f"quote.retry_attempts 必須 ≥ 1，目前是 {self.quote_retry_attempts}"
            )
        if self.quote_retry_interval_seconds < 0:
            raise ValueError(
                f"quote.retry_interval_seconds 不可為負，目前是 "
                f"{self.quote_retry_interval_seconds}"
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


def _parse_dates(values) -> frozenset:
    """把 yaml 的日期清單轉成 date 集合。yaml 已經幫我們解析成 date 物件了。"""
    if not values:
        return frozenset()
    return frozenset(values)


def build(raw) -> Config:
    """從原始 mapping 組出 Config。缺 key 會拋 KeyError，不靜默補預設值。"""
    return Config(
        discord_enabled=raw["discord"]["enabled"],
        quote_retry_attempts=raw["quote"]["retry_attempts"],
        quote_retry_interval_seconds=raw["quote"]["retry_interval_seconds"],
        calendar_extra_closures=_parse_dates(raw["calendar"]["extra_closures"]),
        calendar_extra_openings=_parse_dates(raw["calendar"]["extra_openings"]),
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
