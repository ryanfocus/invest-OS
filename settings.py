"""設定載入。

設計原則：**缺 key 就大聲失敗，不套用預設值**。
靜默的預設值會讓「設定沒生效」變成一個要跑到線上才發現的問題，
而這個系統的線上代表真錢。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

import yaml

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "settings.yaml")


@dataclass(frozen=True)
class Config:
    discord_enabled: bool


# settings.yaml 中所有被程式讀取的 key（扁平化為 "a.b" 形式）。
# 與 Config 欄位一一對應；新增設定時兩邊都要改，測試會檢查。
KNOWN_KEYS = {"discord.enabled"}


def read_raw(path: str = _SETTINGS_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def flatten_keys(data: dict, prefix: str = "") -> set:
    """把巢狀 dict 的 key 攤平成 {"a.b", "a.c"}，供漂移檢查使用。"""
    keys = set()
    for key, value in data.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= flatten_keys(value, prefix=f"{full}.")
        else:
            keys.add(full)
    return keys


def build(raw: dict) -> Config:
    """從原始 dict 組出 Config。缺 key 會拋 KeyError，不靜默補預設值。"""
    return Config(discord_enabled=raw["discord"]["enabled"])


def load(path: str = _SETTINGS_PATH) -> Config:
    return build(read_raw(path))


def _assert_fields_match_known_keys() -> None:
    """import 時就檢查 Config 欄位數與 KNOWN_KEYS 一致，避免只改一邊。"""
    if len(fields(Config)) != len(KNOWN_KEYS):
        raise RuntimeError(
            f"Config 有 {len(fields(Config))} 個欄位，但 KNOWN_KEYS 有 {len(KNOWN_KEYS)} 個。"
            " 新增設定時兩邊都要更新。"
        )


_assert_fields_match_known_keys()
