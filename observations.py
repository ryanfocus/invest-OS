"""當日觀測記錄 —— 「今天看到了什麼」。

**兩份記錄的分工與生命週期見 [SPEC](docs/SPEC.md) 的「兩份記錄，兩種生命週期」**，
詞彙定義見 `CONTEXT.md` 的「當日觀測」。這裡只寫程式碼層面的三個決定：

**一、為什麼不把欄位加進 `PositionRecord` 就好。** 那個型別的不變量是繞著
「有部位」建立的（`lots` 與 `status` 綁死、`CONFIRMED` 必須有口數 ≥ 1、
`exited` 必須有 `close_reason`）。而觀測記錄最常見的一天恰恰是**沒有部位**——
不動作佔 23.2% 的交易日，開關關著更是目前的每一天。
硬塞會迫使那些不變量放寬，而它們正是 ticket 05 整張票建立起來的防線。

**二、為什麼用 JSONL 而不是單一 JSON 陣列。** append 是一次寫入，
不必先讀進整份再整份寫回去——後者在中途斷電時會毀掉**歷史**，
而這份檔案的價值正是歷史。

**三、為什麼壞掉的行是跳過而不是拋例外。** 見 `read_observations`。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields

# `strategy` 不 import 任何專案內模組，所以放在頂層不會有循環依賴。
from strategy import LONG, NO_TRADE, SHORT
import paths

logger = logging.getLogger(__name__)

_ROOT = paths.app_root()
OBSERVATIONS_PATH = os.path.join(_ROOT, "state", "observations.jsonl")


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


def read_observations(
    path: str = OBSERVATIONS_PATH,
) -> tuple[list[Observation], list[str]]:
    """讀出全部觀測，回傳 `(讀得懂的記錄, 讀不懂的行的問題描述)`。

    **壞掉的行跳過，但一定要回報。** 這兩件事缺一不可，而初版兩件都做錯了：
    它讀到壞行就拋例外，於是

      一行壞掉 → append 讀不到既有記錄 → 之後**再也寫不進任何觀測**
               → 對帳每天在「讀不懂」返回 → **永遠不再對帳**
               → 而且全程只進 log，一則告警都不發

    整個稽核能力會安靜地、永久地消失，而系統看起來完全正常——
    那正是本模組要防的那種錯誤，發生在它自己身上（code-review 2026-08-18 實測）。

    現在壞行只影響它自己那一天，其餘照常；而 `damaged` 讓呼叫端發得出告警。
    「讀不懂不可以看起來像沒有觀測」這個原本的顧慮，由**回報**滿足，
    不必靠**停擺**。
    """
    if not os.path.exists(path):
        # 檔案不存在就是「還沒有觀測」——開機第一天，正常，不是故障。
        return [], []

    records: list[Observation] = []
    damaged: list[str] = []
    known = {f.name for f in fields(Observation)}
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            problem = ""
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                problem = f"解析失敗：{exc}"
            else:
                if not isinstance(data, dict):
                    problem = f"不是物件：{type(data).__name__}"
                elif set(data) - known:
                    # 欄位改名時不可以靜靜地少對一個商品。
                    problem = f"有不認得的欄位：{sorted(set(data) - known)}"
                else:
                    try:
                        records.append(Observation(**data))
                    except (TypeError, ValueError) as exc:
                        # 檔案是人可以手改的，所以建構時的不變量在這裡要再驗一次。
                        problem = f"內容有問題：{exc}"
            if problem:
                damaged.append(f"第 {number} 行{problem}")
    return records, damaged


def read_observation_before(
    day: int, path: str = OBSERVATIONS_PATH
) -> Observation | None:
    """取 `day` **之前**最近的一筆觀測。沒有則回 `None`。

    為什麼是「之前」而不是「最後一筆」：對帳跑在 08:50 那班的最後面，
    而今天的觀測**剛剛才寫進去**。拿今天那筆去對，期交所還沒公布當日資料，
    每天都會「查無資料」而靜默跳過——對帳等於從來沒跑過。

    隔了幾天（連假、上次沒跑）照樣回傳。官方資料查得到就查得到，
    日期久遠不影響正確性；跳過的話「上週五取到錯誤盤別」就永遠沒人發現。

    ⚠️ 這個便利函式**丟掉了損壞資訊**。在意的呼叫端（對帳）要用
    `read_observations`，否則檔案壞掉這件事又會沒有人知道。
    """
    records, _ = read_observations(path)
    before = [r for r in records if r.trading_day < day]
    return max(before, key=lambda r: r.trading_day) if before else None


def _read_day(day: int, path: str) -> Observation | None:
    records, _ = read_observations(path)
    for record in records:
        if record.trading_day == day:
            return record
    return None
