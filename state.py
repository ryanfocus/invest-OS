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


@dataclass(frozen=True)
class PositionRecord:
    """OS 開出的一筆部位。

    `lots` 是**實際成交口數**，不是委託口數——市價 IOC 可能部分成交，
    而出場要平的是實際持有的量。

    `trading_day` 讓出場那一班能確認「這筆是今天的」。隔夜殘留的舊記錄
    不該被當成今天的部位去平（那會開出反向新倉）。
    """

    trading_day: int          # yyyymmdd
    product: str              # 報價代碼
    contract_month: str       # yyyymm
    side: str                 # BUY / SELL（進場方向）
    lots: int                 # 實際成交口數
    order_seq: str = ""
    exited: bool = False


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
