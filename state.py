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

# 「不確定」的是**哪一筆委託**。這個區分是 2026-08-19 code-review 抓到的：
#
#   進場不確定 → 早上開了幾口不知道 → 下午查得到就照常平倉
#   出場不確定 → 下午平掉幾口不知道 → **絕對不可以再送一次**
#
# 少了它，兩種「不確定」長得一模一樣，而出場那筆的 `order_seq` 記的是
# **出場單**的序號。重跑時拿它去查，會查到出場單成交的 N 口，程式卻把它
# 當成「早上成交 N 口」，於是再送一筆等量反向委託——帳上早就平掉了，
# 第二筆是裸露的反向新倉，沒人管地進夜盤，而狀態檔還說「已了結」。
UNCERTAIN_ENTRY = "ENTRY"
UNCERTAIN_EXIT = "EXIT"
_UNCERTAIN_STAGES = (UNCERTAIN_ENTRY, UNCERTAIN_EXIT)

# 部位是**怎麼**了結的。兩種結局在帳上都是「沒有部位」，但價格來源不同：
#
# `BY_EXIT`        我們自己送出反向委託平掉，成交價是期貨價——回測假設的就是這個。
# `BY_SETTLEMENT`  結算日沒送單，合約到期由交易所現金交割。結算價是
#                  **13:00–13:30 加權指數每筆成交價的簡單算術平均**，那是現貨指數。
#
# ⚠️ 分開記是為了日後查帳：結算日的實際損益本來就會與用期貨價計算的回測有落差，
#    沒有這個欄位的話，那個落差看起來會像程式算錯。
BY_EXIT = "EXIT"
BY_SETTLEMENT = "SETTLEMENT"
_CLOSE_REASONS = (BY_EXIT, BY_SETTLEMENT)


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
    # 委託口數。成交幾口可能不知道，但**送出去幾口一定知道**——
    # 那是曝險的上界，「可能有 1 口」與「可能有 10 口」是完全不同的緊急程度。
    # 對已確認的記錄它還能讓 07 對帳看出部分成交。
    requested_lots: int
    # 這個合約的最後交易日（yyyymmdd）。**出場那班靠它判斷今天是不是結算日**，
    # 而結算日的規則是「不重試」。存在這裡而不是讓出場再查一次商品清單：
    # 那個資訊 08:50 就知道了，13:40 再去連報價主機只是多一個會失敗的地方——
    # 而那時候失敗的代價是部位過夜。
    #
    # ⚠️ 刻意**沒有預設值**。給 0 當預設的話，缺這個欄位的舊狀態檔會讓
    #    `is_settlement_day` 永遠回 False——結算日的「不重試」規則被靜默關掉。
    #    現在缺欄位會在 `read_position` 就拋 StateCorrupted。
    #
    #    ⚠️ 這一招只對**沒有預設值**的欄位有效。有預設值的欄位（如
    #    `uncertain_stage`）缺了不會拋 TypeError，會一路走到不變量檢查——
    #    所以 `read_position` 兩種例外都要攔（見那裡的說明）。
    last_trading_day: int
    status: str = CONFIRMED
    order_seq: str = ""
    # 已經平倉了嗎。**部分成交不算**——只平掉一部分時這裡維持 False，
    # 否則隔日對帳會以為一切正常，而殘留的口數還在帳上。
    exited: bool = False
    # 了結的方式（`BY_EXIT` / `BY_SETTLEMENT`），未了結時為空字串。
    # `exited` 回答「還要不要送單」，這個欄位回答「當天的出場價是哪來的」。
    close_reason: str = ""
    # 不確定的是**哪一筆委託**（`UNCERTAIN_ENTRY` / `UNCERTAIN_EXIT`）。
    # 只有 status 為 UNCERTAIN 時才有值——見上方常數的說明。
    uncertain_stage: str = ""
    # 券商說**早上那筆**對淨部位做了什麼：`N` 新倉、`O` 平倉。
    #
    # 存下來只為了一件事：下午出場後拿它跟出場那筆比。兩者**必然相反**
    # （早上開了就下午平；早上平掉使用者的部位（跨越零）就下午開一口還回去）。
    # 一樣代表下午沒平到任何東西，帳上多一口沒人管的部位。
    #
    # ⚠️ **空字串是「不知道」，不是某個值。** 2026-08-25 之前寫下的記錄
    # 沒有這一格，看不懂券商編碼時也是空的。拿「不知道」去比對只會得到
    # 假警報，所以上層遇到空字串要**跳過檢查**。
    entry_position_type: str = ""

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
        # `exited` 與 `close_reason` 必須同進同退。少了理由的已了結記錄，
        # 日後查帳分不出「我們平掉的」與「交易所結算掉的」；而有理由卻沒了結，
        # 出場那班會照常送單去平一個記錄說已經沒有的部位。
        if self.exited and self.close_reason not in _CLOSE_REASONS:
            raise ValueError(
                f"已了結的記錄必須註明方式，只能是 {list(_CLOSE_REASONS)}，"
                f"目前是 {self.close_reason!r}"
            )
        if not self.exited and self.close_reason:
            raise ValueError(
                f"尚未了結的記錄不該有了結方式（目前是 {self.close_reason!r}）"
            )
        # 「不確定」一定要講清楚是哪一筆不確定。少了它，出場那筆會被當成
        # 進場那筆處理——重跑時再送一次等量反向委託，開出反向新倉。
        if self.status == UNCERTAIN and self.uncertain_stage not in _UNCERTAIN_STAGES:
            raise ValueError(
                f"不確定的記錄必須註明是哪一筆委託，只能是 {list(_UNCERTAIN_STAGES)}，"
                f"目前是 {self.uncertain_stage!r}"
            )
        if self.status != UNCERTAIN and self.uncertain_stage:
            raise ValueError(
                f"已確認的記錄不該有 uncertain_stage（目前是 {self.uncertain_stage!r}）"
            )

    @property
    def exit_side(self) -> str:
        """平掉這個部位要送的買賣別。

        **只有這一個地方做方向反轉。** 這個對應寫反的後果是部位加倍而不是平倉，
        而它原本散在三處（`main` 一處、Discord 訊息兩處）——
        散開的話遲早有一處會跟其他的不一致。
        """
        from broker import BUY, SELL
        return SELL if self.side == BUY else BUY

    def is_settlement_day(self, day) -> bool:
        """今天是不是這個合約的最後交易日。**結算日不送出場委託**——
        合約 13:30 停止交易，未平倉部位由交易所以最後結算價現金交割。"""
        from broker import to_yyyymmdd
        return self.last_trading_day == to_yyyymmdd(day)

    @property
    def uncertain_entry(self) -> bool:
        """不確定的是**早上那筆進場單**——查得到就能自動復原、照常平倉。"""
        return self.status == UNCERTAIN and self.uncertain_stage == UNCERTAIN_ENTRY

    @property
    def uncertain_exit(self) -> bool:
        """不確定的是**下午那筆出場單**。

        ⚠️ **這種情況絕對不可以自動送單。** 那筆可能已經成交了，再送一次
        就是在已經平掉的帳上繼續反向賣（或買），開出一個沒人管的新倉。
        `order_seq` 記的是出場單的序號，拿去查只會查到出場單自己的成交——
        看起來像「早上成交了 N 口」，那正是危險的地方。
        """
        return self.status == UNCERTAIN and self.uncertain_stage == UNCERTAIN_EXIT

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
    except ValueError as exc:
        # ⚠️ **不變量違反也是「讀不懂」，不可以讓它逃出去。**
        #
        # `__post_init__` 的每一條檢查都是防線，但它們同時在讀取路徑上開了
        # 一個崩潰面：`ValueError` 逃出 `read_position` 的話，早班與午班都會
        # 直接以 traceback 結束，**連一則 Discord 都發不出去**——
        # 而這份檔案開頭就寫著讀不懂要「大聲失敗，交給人處理」，
        # 大聲失敗到告警都發不出來就不是那個意思了。
        #
        # 兩種來源都真實存在：
        #   1. **舊版程式寫的檔案**。有預設值的新欄位（如 `uncertain_stage`）
        #      不會觸發上面的 TypeError，而是在不變量那關才炸。
        #   2. **使用者手改**。本模組的設計說明明講「必要時手改」，
        #      改出自相矛盾的內容是預期內的事，不是異常。
        #
        # 訊息與缺欄位刻意分開：對照著告警排查的人來說，
        # 「少了東西」與「內容互相矛盾」要找的地方不一樣。
        raise StateCorrupted(f"狀態檔 {path} 的內容自相矛盾：{exc}") from exc


def clear_position(path: str = STATE_PATH) -> None:
    """把狀態檔刪掉。**只在確定「今天沒有部位」時才可以呼叫。**

    為什麼是刪檔而不是寫一筆 `lots=0` 的記錄：`PositionRecord` 的不變量
    刻意規定 `CONFIRMED` 必須有口數 ≥ 1——這個型別表達的是「OS 開了一個部位」，
    而「確定沒有部位」在這套設計裡就是**沒有記錄**。進場那條路早就是這樣做的
    （成交 0 口時直接不寫檔）。

    會用到它的只有一種情況：早上記成「不確定」，下午查詢確認**確實 0 口**。
    留著那筆不確定的記錄等於讓檔案說謊——它說「不知道」，但我們已經知道了。

    檔案本來就不存在時什麼都不做。
    """
    if os.path.exists(path):
        os.remove(path)
        logger.info("狀態檔已清除（確認今日無部位）")


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

    # ⚠️ 用 %s 不是 %d。lots 在「不確定」時是 None，%d 會讓 logging 內部拋
    #    TypeError 並**把整行丟掉**——而那正是人在 13:40 排查時要找的那一行。
    logger.info("狀態檔已更新：%s %s %s 口（狀態 %s）",
                record.product, record.side, record.lots, record.status)
