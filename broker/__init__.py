"""broker —— 與券商往來的唯一出入口。

這一層存在的理由是可測試性：群益的 API 是 Windows COM 元件，無法在測試環境執行。
把它隔離在這裡，策略、狀態、通知等所有其他部分都能在沒有 COM、沒有帳號的機器上驗證。

實作有兩個：
  - `broker.fake`    測試用，回放預先指定的資料並記錄收到的指令
  - `broker.capital` 群益 COM（後續 ticket 實作）

⚠️ 任何真實 COM 的初始化都必須延遲到函式內執行，不可在模組載入時做——
   否則未註冊 COM 的機器連 import 都會失敗，測試一行都跑不了。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OpenPrices:
    """大台、小台、微台的當日 AM 盤開盤價。

    數值已還原小數（群益回傳為整數且放大 100 倍，換算在 broker 層完成，
    不外洩到策略層）。
    """

    tx: float
    mtx: float
    tmf: float
