import os
import sys
from datetime import time

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
_observations_path = ""


@pytest.fixture(autouse=True)
def isolated_state_file(tmp_path):
    """每個測試都有自己的狀態檔與觀測檔路徑。

    `run_entry` 的 `state_path` / `observations_path` 都是必填參數，
    所以忘記傳會直接是 TypeError；這個 fixture 負責提供那兩個路徑，
    讓測試不必各自處理。

    ⚠️ 兩個檔案**刻意分開命名**而不是共用一個路徑推導出來——
    它們是不同的東西（部位／觀測），生命週期也不同（覆蓋／累積）。
    """
    global _state_path, _observations_path
    _state_path = str(tmp_path / "position.json")
    _observations_path = str(tmp_path / "observations.jsonl")
    yield tmp_path / "position.json"
    _state_path = ""
    _observations_path = ""


@pytest.fixture(autouse=True)
def logs_never_touch_the_real_directory(tmp_path_factory, monkeypatch):
    """**測試絕不碰正式的 `logs/`。**

    那個目錄是真實交易的鑑識記錄：券商的原始回覆、群益元件自己寫的日誌。
    2026-08-23 發現測試同時在做兩件事——

      寫：`CapitalBroker` 的原始回覆存檔預設就指向那裡，累積了 46 個
          `SEQ0000000001` 的假檔案，和真的回覆混在同一個檔名格式裡
      刪：`run_entry` 的 `logs_path` 沒傳時會對那裡跑 `purge_old_logs`

    刪的範圍剛好與正式環境相同（30 天）所以沒有造成實害，但那是巧合——
    任何人把保留天數調小來測一下，就會清掉真的交易記錄。

    導開的是**兩個** `LOGS_PATH`：`housekeeping` 的（broker 在執行期查它）
    與 `main` 的（`from ... import` 複製了一份，改前者不會動到後者）。
    """
    import housekeeping
    import main as main_module

    # ⚠️ **不放在 `tmp_path` 底下。** 有測試會斷言 `tmp_path` 裡有哪些東西
    #    （狀態檔沒留下暫存檔、清理只碰指定目錄），多一個 `logs/` 就會讓
    #    它們無緣無故變紅——而紅的原因與它們要守的事情完全無關。
    logs = tmp_path_factory.mktemp("logs")
    monkeypatch.setattr(housekeeping, "LOGS_PATH", str(logs))
    monkeypatch.setattr(main_module, "LOGS_PATH", str(logs))
    return logs


def state_path() -> str:
    """本次測試專用的狀態檔路徑。"""
    return _state_path


def observations_path() -> str:
    """本次測試專用的觀測記錄路徑。"""
    return _observations_path


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


# 進場那班準時執行的時刻。測試傳這個等於說「這天一切正常」，
# 而想測時間關卡的就自己寫明幾點——「哪些測試在乎時間」因此在測試碼裡看得見。
ON_TIME = time(8, 50)


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
        "entry_cutoff": time(9, 0),
    }
    base.update(overrides)
    return settings_module.Config(**base)
