"""Discord 發報。

訊息組裝（`build_signal_payload`）是純函式，方便直接斷言內容；
實際送出（`send`）獨立成一個函式，測試在此處置換。
"""

from __future__ import annotations

import logging
from datetime import date

import requests

from strategy import LONG, NO_TRADE, SHORT, SignalResult

logger = logging.getLogger(__name__)

# 訊號方向對外顯示用的中文。程式內用英文常數，訊息用中文——
# 使用者在手機上看到的是「做多」，不是 LONG。
_SIGNAL_TEXT = {
    LONG: "做多",
    SHORT: "做空",
    NO_TRADE: "不動作",
}


def build_signal_payload(result: SignalResult, d: date) -> dict:
    """把訊號結果組成 Discord webhook 的 payload。純函式，無 I/O。"""
    lines = [
        f"{d.strftime('%Y/%m/%d')} OS",
        f"大台開盤：{result.tx:.0f}",
        f"小台開盤：{result.mtx:.0f}",
        f"微台開盤：{result.tmf:.0f}",
        f"訊號：{_SIGNAL_TEXT[result.signal]}",
    ]
    return {"content": "\n".join(lines)}


def send(payload: dict, webhook_url: str, timeout=(5, 10)) -> bool:
    """送出 payload。回傳是否成功。

    發送失敗只 log 不拋例外——Discord 掛掉不該讓策略本身失敗。
    """
    if not webhook_url:
        logger.warning("Discord webhook URL 未設定，跳過發送")
        return False

    try:
        resp = requests.post(webhook_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("Discord 發送例外：%s", exc)
        return False

    if resp.ok:
        logger.info("Discord 已發送")
        return True

    logger.error("Discord 發送失敗：HTTP %s", resp.status_code)
    return False
