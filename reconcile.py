"""隔日對帳 —— 拿前一交易日的觀測跟期交所官方資料比一次。

**它存在是為了抓一種其他地方全部抓不到的錯誤：開盤價取到錯誤的盤別。**

台指期一天有兩個盤。報價代碼少一個 `AM` 後綴就會拿到全盤（前一日 17:25 夜盤）
的開盤價，而程式**不會報錯**——它拿到的是一個完全合理的數字。
45850 跟 46200 一樣像個開盤價。結果是每天的訊號都錯，而且沒有任何跡象。

期交所是這條管線之外唯一的真相：群益的報價與策略的判斷都在同一條管線上，
管線本身錯了兩邊會一起錯。

## 兩條不可動搖的原則

**一、絕不礙事。** 對帳掛掉的代價是「少檢查一天」，08:50 那班掛掉的代價是
「整天沒有訊號」。所以 `run_reconciliation` **永遠不拋例外**——
壞掉的觀測檔、斷線的網路、500 的 webhook，一律記進 log 然後安靜結束。

**二、「沒東西可對」不是失敗。** 開機第一天、連假、上次沒跑、期交所還沒公布，
都會走到「安靜跳過」。把它們當成不一致的話，每天都會收到一則假警，
而假警收久了真的那則就會被忽略。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from observations import read_observation_before

logger = logging.getLogger(__name__)

# 三個商品在訊息裡的中文名，順序固定為大台→小台→微台。
# 順序固定的理由：三個都不一致時（那正是取到錯誤盤別的樣子），
# 每次都用同樣的順序列出來，人一眼就認得出「又是全部」。
_PRODUCTS = (("tx", "大台"), ("mtx", "小台"), ("tmf", "微台"))

# 浮點數表示誤差的容許量。
#
# ⚠️ **這不是「容差」。** 指數點位的差異最小單位遠大於它——2026-08-10 那次
#    群益與期交所差 2 點，用這個值照樣會被報出來。
#    真正的容差問題（那 2 點是常態還是雜訊）證據還不足，**刻意不處理**：
#    預先加上容忍值會把真正的錯誤一起放過。累積幾週再決定。
_EPSILON = 1e-6


@dataclass(frozen=True)
class Mismatch:
    """某個商品兩邊對不上。訊息要足以讓人不必自己去查就知道發生什麼事。"""

    product: str        # 中文名，直接進訊息
    ours: float         # 我們當天記下來的
    official: float     # 期交所的


@dataclass(frozen=True)
class ReconcileOutcome:
    """對帳結果。**沒有 exit_code**——對帳不改變程式的結束狀態。

    `checked_day` 為 `None` 代表沒對到（跳過），此時 `skipped` 說明原因。
    跳過與「對完了、都一樣」是不同的事：前者什麼都沒驗，後者驗過了。
    """

    checked_day: int | None
    mismatches: tuple = ()
    notified: bool = False
    skipped: str = ""


def compare_opens(observation, official: dict) -> tuple[Mismatch, ...]:
    """純函式：比對三個開盤價，回傳對不上的那些。

    比對在**數值域**進行。比字串的話 `45850` 與 `45850.0` 會被判成不一致，
    每天都報假警。
    """
    missing = [key for key, _ in _PRODUCTS if key not in official]
    if missing:
        # 缺商品時整份放棄，不比剩下的——見 `taifex.fetch_official_opens` 的說明。
        raise ValueError(f"官方資料缺少商品：{missing}")

    results = []
    for key, label in _PRODUCTS:
        ours = float(getattr(observation, key))
        theirs = float(official[key])
        if abs(ours - theirs) > _EPSILON:
            results.append(Mismatch(product=label, ours=ours, official=theirs))
    return tuple(results)


def run_reconciliation(
    *,
    today: int,
    notify,
    fetch_official,
    path: str,
    discord_enabled: bool,
) -> ReconcileOutcome:
    """對一次帳。**這個函式永遠不拋例外。**

    `today` 是 yyyymmdd。對的是 `today` **之前**最近的那一筆觀測——
    今天的觀測 08:50 才寫進去，期交所還沒公布當日資料。

    `fetch_official` 傳進來而不是直接呼叫 `taifex`：那是網路 I/O，
    測試要能餵它壞東西（缺商品、拋例外、回 None）而不需要真的連線。
    """
    try:
        observation = read_observation_before(today, path=path)
    except Exception as exc:  # noqa: BLE001
        # 觀測檔讀不懂。這是真的壞了，要在 log 裡看得見——
        # 但不發 Discord、不影響結束狀態（ticket 07 的驗收條件）。
        logger.error("對帳略過：觀測記錄讀不懂：%s", exc)
        return ReconcileOutcome(checked_day=None, skipped=f"觀測記錄讀不懂：{exc}")

    if observation is None:
        logger.info("對帳略過：沒有 %s 之前的觀測記錄", today)
        return ReconcileOutcome(checked_day=None, skipped="沒有可對帳的觀測記錄")

    try:
        official = fetch_official(observation.trading_day)
    except Exception as exc:  # noqa: BLE001
        logger.warning("對帳略過：取官方資料失敗（%s）：%s", type(exc).__name__, exc)
        return ReconcileOutcome(checked_day=None, skipped=f"取官方資料失敗：{exc}")

    if not official:
        # 期交所還沒公布那天的資料。**這不是不一致**，只是還沒到。
        logger.info("對帳略過：期交所查無 %s 的資料", observation.trading_day)
        return ReconcileOutcome(checked_day=None, skipped="期交所查無資料")

    try:
        mismatches = compare_opens(observation, official)
    except Exception as exc:  # noqa: BLE001
        # 缺商品、值不是數字（期交所改格式）。比一半比不比更糟——
        # 「兩個商品都對」看起來像沒事，其實根本沒驗完。
        logger.warning("對帳略過：官方資料看不懂（%s）：%s", type(exc).__name__, exc)
        return ReconcileOutcome(checked_day=None, skipped=f"官方資料看不懂：{exc}")

    if not mismatches:
        logger.info("對帳一致：%s 大台=%s 小台=%s 微台=%s",
                    observation.trading_day, observation.tx,
                    observation.mtx, observation.tmf)
        return ReconcileOutcome(checked_day=observation.trading_day)

    for m in mismatches:
        logger.error("對帳不一致：%s %s 我們=%s 期交所=%s",
                     observation.trading_day, m.product, m.ours, m.official)

    notified = False
    if discord_enabled:
        from notifiers.discord import build_reconciliation_payload
        try:
            notified = bool(notify(build_reconciliation_payload(
                observation.trading_day, mismatches)))
        except Exception as exc:  # noqa: BLE001
            # webhook 掛掉不該把整班拖下水。不一致本身已經記進 log 了。
            logger.error("對帳告警送不出去：%s", exc)

    return ReconcileOutcome(
        checked_day=observation.trading_day,
        mismatches=mismatches,
        notified=notified,
    )
