import os
import sys

import pytest

# 讓測試能 import 專案根目錄的模組，不必在每個測試檔重複 sys.path 手續
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import settings as settings_module  # noqa: E402
from broker import MTX_CODE, TMF_CODE, TX_CODE, ContractInfo  # noqa: E402

# 三個商品的近月合約，數值取自 2026-08-07 實際抓到的商品清單。
# 放在這裡而不是各測試檔各寫一份：欄位變動時只有一個地方要改。
CONTRACTS = {
    TX_CODE: ContractInfo(code=TX_CODE, last_trading_day=20260819, order_code="TX08"),
    MTX_CODE: ContractInfo(code=MTX_CODE, last_trading_day=20260819, order_code="MTX08"),
    TMF_CODE: ContractInfo(code=TMF_CODE, last_trading_day=20260819, order_code="TM2608"),
}


_state_path = ""


@pytest.fixture(autouse=True)
def isolated_state_file(tmp_path):
    """每個測試都有自己的狀態檔路徑，由 `state_path()` 取用。

    `run_entry` 的 `state_path` 是必填參數，所以忘記傳會直接是 TypeError；
    這個 fixture 負責提供那個路徑，讓測試不必各自處理。
    """
    global _state_path
    _state_path = str(tmp_path / "position.json")
    yield tmp_path / "position.json"
    _state_path = ""


def state_path() -> str:
    """本次測試專用的狀態檔路徑。"""
    return _state_path


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
        # 每個測試預設都是「不下單」——想測下單的必須自己寫明 auto_order_enabled=True。
        # 這樣「哪些測試會下單」在測試碼裡看得見，不是靠預設值躲起來。
        "auto_order_enabled": False,
        "order_product": "MTX00AM",
        "order_lots": 1,
        "order_fill_timeout_seconds": 10,
        "capital_environment": "test",
        "calendar_extra_closures": frozenset(),
        "calendar_extra_openings": frozenset(),
    }
    base.update(overrides)
    return settings_module.Config(**base)
