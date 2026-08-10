import os
import sys

# 讓測試能 import 專案根目錄的模組，不必在每個測試檔重複 sys.path 手續
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import settings as settings_module  # noqa: E402


class RecordingNotifier:
    """取代真正的 Discord，記錄被要求送出什麼。

    測試斷言的是**內容**與**有沒有送**，不是送出的機制。
    """

    def __init__(self):
        self.sent = []

    def __call__(self, payload) -> bool:
        self.sent.append(payload)
        return True

    @property
    def text(self) -> str:
        return "\n".join(p["content"] for p in self.sent)


def make_config(**overrides) -> settings_module.Config:
    """組一份測試用設定。預設值與 config/settings.yaml 一致。

    集中在這裡的好處：日後新增設定項時只有一個地方要改，
    否則每個測試檔都會各自炸掉一次。
    """
    base = {
        "discord_enabled": True,
        "quote_retry_attempts": 3,
        "quote_retry_interval_seconds": 60,
        "calendar_extra_closures": frozenset(),
        "calendar_extra_openings": frozenset(),
    }
    base.update(overrides)
    return settings_module.Config(**base)
