"""當日觀測記錄 —— 「今天看到了什麼」。

與部位狀態檔（`state.py`）刻意分成兩個檔案，因為它們是兩件不同的事：

| | 部位記錄 `position.json` | 觀測記錄 `observations.jsonl` |
|---|---|---|
| 內容 | OS 帳上有什麼部位 | 今天看到的三個開盤價與訊號 |
| 何時寫 | **只有真的下單那天** | **每個交易日，無例外** |
| 不動作的日子 | 不寫 | 寫 |
| 開關關閉時 | 不寫 | 寫 |
| 保存方式 | 覆蓋，只留當天 | 累積，一天一行 |
| 誰讀 | 當天 13:40 的出場 | 隔天早上的對帳（ticket 07） |

**為什麼不把欄位加進 `PositionRecord` 就好**：那個型別的不變量是繞著
「有部位」建立的（`lots` 與 `status` 綁死、`CONFIRMED` 必須有口數 ≥ 1、
`exited` 必須有 `close_reason`）。而觀測記錄最常見的一天恰恰是**沒有部位**——
不動作佔 23.2% 的交易日，開關關著更是目前的每一天。
硬塞會迫使那些不變量放寬，而它們正是 ticket 05 整張票建立起來的防線。

**這個檔案存在的理由**（2026-08-17 盤點時發現的洞）：ticket 07 的對帳
需要「前一交易日記錄的三個開盤價」，但那三個數字當時只活在記憶體、
Discord 的中文訊息、與日誌裡——**沒有一個程式讀得回來**。
於是 SPEC 承諾的「第一階段只跑訊號、零金錢風險驗證資料正確性」
在機制上並不存在。

用 JSONL（一行一筆 JSON）而不是單一 JSON 陣列：append 是一次寫入，
不必先讀進整份再整份寫回去——後者在中途斷電時會毀掉**歷史**，
而這份檔案的價值正是歷史。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.abspath(__file__))
OBSERVATIONS_PATH = os.path.join(_ROOT, "state", "observations.jsonl")


class ObservationUnreadable(Exception):
    """觀測記錄存在但讀不懂。**不可以當成「沒有觀測」處理。**

    對帳本來就設計成「沒東西可對就安靜結束」（開機第一天、連假都屬此類）。
    讀不懂若也走那條路，兩者長得一模一樣，檔案壞掉這件事就永遠不會浮出來。
    """


class ObservationConflict(Exception):
    """同一個交易日已經有記錄，而且**數字不一樣**。

    同一天跑第二次本身很正常（人想再看一次），數字不同才是訊號——
    代表報價來源在同一天給出了兩組不同的開盤價。
    保留第一筆（它比較接近 08:45），但要讓呼叫端有機會講出來。
    """


@dataclass(frozen=True)
class Observation:
    """某個交易日 08:50 看到的東西。

    刻意**不含**部位相關的任何欄位。這一筆在「今天不下單」的日子照樣要寫，
    而那正是它最有價值的時候——開關關著的期間沒有別的機制在驗證資料正確性。
    """

    trading_day: int          # yyyymmdd
    tx: float                 # 大台開盤價
    mtx: float                # 小台開盤價
    tmf: float                # 微台開盤價
    signal: str               # LONG / SHORT / NO_TRADE
    # 當日使用的近月合約月份（yyyymm，取自大台）。
    #
    # 記這一欄是為了讓「結算日當天選到的是本月還是次月」這個問題
    # **每個月自動回答一次**，不必再為它單獨排一次驗證班（ticket 03）。
    #
    # ⚠️ 只記大台的。三個商品的近月理應相同，不同時 `run_entry` 已經會
    #    以 WARNING 記錄（見「只有部分商品到期」那條）。
    contract_month: str

    def __post_init__(self) -> None:
        for name in ("tx", "mtx", "tmf"):
            value = getattr(self, name)
            if not value > 0:
                # **0 是「報價尚未就緒」的值，不是價格。** 指數開盤價不可能是 0。
                # 讓它寫得進去的話，隔天對帳會拿 0 去比、報一個假的不一致，
                # 而真正的問題（報價沒就緒卻照樣算了訊號）反而被蓋掉。
                raise ValueError(f"{name} 的開盤價必須大於 0，目前是 {value}")

        from strategy import LONG, NO_TRADE, SHORT
        if self.signal not in (LONG, SHORT, NO_TRADE):
            raise ValueError(f"未知的訊號 {self.signal!r}")

        if not self.contract_month:
            raise ValueError("缺少合約月份")


def append_observation(record: Observation, path: str = OBSERVATIONS_PATH) -> None:
    """把今天這一筆接在檔案最後面。

    同一個交易日已經有記錄時**不重複寫**：`run_entry` 的重複執行保護看的是
    部位記錄，而開關關著時根本不寫部位記錄——那條防線在這裡攔不住。

    數字不同時拋 `ObservationConflict`（檔案不動）。同一天兩組不同的開盤價
    本身就是要看的東西，靜靜地丟掉一組等於把證據銷毀。
    """
    existing = _read_day(record.trading_day, path=path)
    if existing is not None:
        if existing == record:
            logger.info("%s 的觀測記錄已存在，不重複寫入", record.trading_day)
            return
        raise ObservationConflict(
            f"{record.trading_day} 已有觀測記錄且數字不同："
            f"已記 {existing.tx}/{existing.mtx}/{existing.tmf}，"
            f"這次是 {record.tx}/{record.mtx}/{record.tmf}"
        )

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    # append 模式：一次 write 就是一整行，不必先讀進整份再寫回去。
    # 後者中途失敗會毀掉**歷史**，而這份檔案的價值正是歷史。
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())     # 確認真的落磁碟，不只是進了作業系統快取

    logger.info("觀測記錄已寫入：%s 大台=%s 小台=%s 微台=%s 訊號=%s 合約=%s",
                record.trading_day, record.tx, record.mtx, record.tmf,
                record.signal, record.contract_month)


def read_observation_before(
    day: int, path: str = OBSERVATIONS_PATH
) -> Observation | None:
    """取 `day` **之前**最近的一筆觀測。沒有則回 `None`。

    為什麼是「之前」而不是「最後一筆」：對帳跑在 08:50 那班的最後面，
    而今天的觀測**剛剛才寫進去**。拿今天那筆去對，期交所還沒公布當日資料，
    每天都會「查無資料」而靜默跳過——對帳等於從來沒跑過。

    隔了幾天（連假、上次沒跑）照樣回傳。官方資料查得到就查得到，
    日期久遠不影響正確性；跳過的話「上週五取到錯誤盤別」就永遠沒人發現。
    """
    latest = None
    for record in _iter_observations(path):
        if record.trading_day < day and (
            latest is None or record.trading_day > latest.trading_day
        ):
            latest = record
    return latest


def _read_day(day: int, path: str) -> Observation | None:
    for record in _iter_observations(path):
        if record.trading_day == day:
            return record
    return None


def _iter_observations(path: str):
    """逐行讀出。檔案不存在就是「還沒有觀測」——那是正常的，不是故障。"""
    if not os.path.exists(path):
        return

    known = {f.name for f in fields(Observation)}
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ObservationUnreadable(
                    f"{path} 第 {number} 行解析失敗：{exc}"
                ) from exc
            if not isinstance(data, dict):
                raise ObservationUnreadable(
                    f"{path} 第 {number} 行不是物件：{type(data).__name__}"
                )
            unexpected = set(data) - known
            if unexpected:
                raise ObservationUnreadable(
                    f"{path} 第 {number} 行有不認得的欄位：{sorted(unexpected)}"
                )
            try:
                yield Observation(**data)
            except (TypeError, ValueError) as exc:
                raise ObservationUnreadable(
                    f"{path} 第 {number} 行內容有問題：{exc}"
                ) from exc
