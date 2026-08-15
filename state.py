"""部位狀態檔 —— 進場那一班寫、出場那一班讀。

兩班是不同的行程，中間隔著幾個小時，唯一的溝通管道就是這個檔案。
所以它的正確性直接等於「13:40 會不會去平倉」。

三個設計選擇，各有理由：

1. **JSON 純文字。** 出事時使用者要能自己打開來看、必要時手改。
2. **先寫暫存檔再 `os.replace`。** 同一個檔案系統上 replace 是原子的，
   寫到一半斷電只會留下暫存檔，正式檔要嘛是舊的、要嘛是新的，不會是半個。
3. **讀不懂就拋例外，絕不回 None。** 「確實沒有部位」與「檔案壞了」若混為一談，
   出場那一班會以為今天沒進場而什麼都不做，帳上的部位就這樣進夜盤——
   這是本系統最貴的失效模式（見 ticket 05）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, fields

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_ROOT, "state", "position.json")


class StateCorrupted(Exception):
    """狀態檔存在但讀不懂。**不可以當成「沒有部位」處理。**"""


# 部位記錄的可信度。
#
# `CONFIRMED`  收到成交回報，口數是實際成交的數字。
# `UNCERTAIN`  **已經送出委託，但不知道成交幾口。** 回報沒回來、或認不出是哪一筆。
#
# ⚠️ 「不確定」與「確定沒有部位」是兩件完全不同的事，而且長得很像：
#    確定沒有 → 下午什麼都不用做
#    不知道   → 下午**絕不可以**自動送單。可能是加倉，也可能開出反向新倉，
#               比什麼都不做更糟。正確行為是發 Discord 叫人去看帳戶。
CONFIRMED = "CONFIRMED"
UNCERTAIN = "UNCERTAIN"
_STATUSES = (CONFIRMED, UNCERTAIN)


@dataclass(frozen=True)
class PositionRecord:
    """OS 開出的一筆部位。

    `lots` 是**實際成交口數**，不是委託口數——市價 IOC 可能部分成交，
    而出場要平的是實際持有的量。

    `trading_day` 讓出場那一班能確認「這筆是今天的」。隔夜殘留的舊記錄
    不該被當成今天的部位去平（那會開出反向新倉）。
    """

    trading_day: int          # yyyymmdd
    product: str              # 報價代碼（對帳用）
    # 下單代碼（MTX08）。**出場要靠它**：委託帶的是這個，不是報價代碼。
    # 不存的話出場得重查商品清單再推導一次，而近月連續代碼在結算之後
    # 就指向次月了——隔夜殘留的部位一重推就會拿到錯的合約，
    # 「平倉」單於是變成開一個新部位。進場時已經知道，不該讓出場再猜。
    order_code: str
    contract_month: str       # yyyymm
    side: str                 # BUY / SELL（進場方向）
    # 實際成交口數。**`None` 代表不知道**（status 為 UNCERTAIN 時），
    # 不是 0——0 的意思是「確定沒成交」，兩者的正確處理完全相反。
    lots: int | None
    status: str = CONFIRMED
    order_seq: str = ""

    def __post_init__(self) -> None:
        """殘缺或自相矛盾的記錄比沒有記錄更糟——出場那一班會拿著它去下單。

        口數與狀態的關係被綁死在這裡，讓「不確定」在型別上就不可能
        被誤認成「0 口」：任何 `lots > 0 才處理` 的判斷都會跳過 0，
        而不確定那一筆正是最需要有人來看的。
        """
        if not self.order_code:
            raise ValueError(f"{self.product} 的部位記錄缺少下單代碼，出場時無法送出委託")
        if self.status not in _STATUSES:
            raise ValueError(f"未知的 status {self.status!r}，只能是 {list(_STATUSES)}")
        if self.status == UNCERTAIN and self.lots is not None:
            raise ValueError(
                f"不確定的記錄口數必須是 None（目前是 {self.lots}）——"
                "填數字會讓它看起來像已知的結果"
            )
        if self.status == CONFIRMED and (self.lots is None or self.lots < 1):
            raise ValueError(
                f"CONFIRMED 的記錄必須有實際口數且 ≥ 1，目前是 {self.lots}"
            )

    @property
    def is_uncertain(self) -> bool:
        """這筆記錄能不能照著自動下單。

        出場那班問這個，而不是自己比對 status 字串——
        比對字串的地方一多，遲早有一處會漏掉，而漏掉的後果是自動送出一筆
        口數可能是錯的反向委託。
        """
        return self.status == UNCERTAIN


def read_position(path: str = STATE_PATH) -> PositionRecord | None:
    """讀出狀態檔。檔案不存在回 `None`，讀不懂則拋 `StateCorrupted`。"""
    if not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StateCorrupted(f"狀態檔 {path} 解析失敗：{exc}") from exc

    if not isinstance(data, dict):
        raise StateCorrupted(f"狀態檔 {path} 的內容不是物件：{type(data).__name__}")

    known = {f.name for f in fields(PositionRecord)}
    unexpected = set(data) - known
    if unexpected:
        raise StateCorrupted(f"狀態檔 {path} 有不認得的欄位：{sorted(unexpected)}")

    try:
        return PositionRecord(**data)
    except TypeError as exc:
        raise StateCorrupted(f"狀態檔 {path} 缺少欄位：{exc}") from exc


def write_position(record: PositionRecord, path: str = STATE_PATH) -> None:
    """原子寫入。

    暫存檔刻意建在**目標檔案的同一個目錄**——跨檔案系統的 `os.replace` 不是原子的，
    而系統暫存目錄與專案目錄很可能不在同一個磁碟上。
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".position-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(record), fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())     # 確認資料真的落磁碟，不只是進了作業系統快取
        os.replace(tmp, path)
    except BaseException:
        # 失敗時把暫存檔清掉，正式檔維持原樣。
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    logger.info("狀態檔已更新：%s %s %d 口", record.product, record.side, record.lots)
