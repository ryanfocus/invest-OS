"""OS 策略的進入點。

依 ADR-0001，本程式由排程各叫醒一次、跑完就結束，不是常駐服務。
目前只實作進場流程；出場在後續 ticket 加入。

外部相依（broker、通知）由呼叫端傳入，不在此處建立——
測試因此能在沒有群益 COM、沒有帳號的機器上驗證整條流程。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import strategy
from broker import OpenPrices
from notifiers.discord import build_signal_payload
from settings import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryOutcome:
    """進場流程的結果，供呼叫端與測試檢視。"""

    signal: str
    opens: OpenPrices
    notified: bool


def run_entry(config: Config, today: date, broker, notify) -> EntryOutcome:
    """進場流程：取開盤價 → 算訊號 → 發報。

    `notify` 是一個吃 payload、回傳是否成功的可呼叫物件。
    """
    opens = broker.get_open_prices()
    logger.info("開盤價 大台=%s 小台=%s 微台=%s", opens.tx, opens.mtx, opens.tmf)

    result = strategy.compute(tx=opens.tx, mtx=opens.mtx, tmf=opens.tmf)
    logger.info("訊號=%s", result.signal)

    notified = False
    if config.discord_enabled:
        notified = bool(notify(build_signal_payload(result, today)))
    else:
        logger.info("Discord 已關閉，不發送")

    return EntryOutcome(signal=result.signal, opens=opens, notified=notified)
