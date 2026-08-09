"""OS 策略的進入點。

依 ADR-0001，本程式由排程各叫醒一次、跑完就結束，不是常駐服務。
目前只實作進場流程；出場在後續 ticket 加入。

外部相依（broker、通知、sleep）由呼叫端傳入，不在此處建立——
測試因此能在沒有群益 COM、沒有帳號的機器上驗證整條流程。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date

import strategy
from broker import LoginFailed, OpenPrices, QuoteNotReady
from notifiers.discord import build_no_signal_payload, build_signal_payload
from settings import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryOutcome:
    """進場流程的結果。

    `signal` 為 None 代表**無法判斷**（資料拿不到），與「不動作」是兩回事——
    後者是判斷出來的結果，前者是根本沒判斷。混為一談會讓故障被當成正常。
    """

    signal: str | None
    opens: OpenPrices | None
    notified: bool
    exit_code: int
    failure: str | None = None


def _fetch_open_prices(broker, attempts: int, interval: int, sleep) -> OpenPrices:
    """取開盤價，未就緒就重試。全部用完仍未就緒 → 讓最後一個 QuoteNotReady 冒出去。

    重試存在的理由是 08:50 的報價偶爾晚幾十秒才齊；間隔比次數重要，
    因為「未就緒」的時間尺度是秒到分，不是毫秒。
    """
    last_error: QuoteNotReady | None = None
    for attempt in range(1, attempts + 1):
        try:
            return broker.get_open_prices()
        except QuoteNotReady as exc:
            last_error = exc
            logger.warning("第 %d/%d 次取開盤價未就緒：%s", attempt, attempts, exc)
            if attempt < attempts:
                sleep(interval)
    raise last_error


def run_entry(config: Config, today: date, broker, notify, sleep=time.sleep) -> EntryOutcome:
    """進場流程：登入 → 取開盤價（含重試）→ 算訊號 → 發報。

    `notify` 是一個吃 payload、回傳是否成功的可呼叫物件。
    """

    def _send(payload) -> bool:
        if not config.discord_enabled:
            logger.info("Discord 已關閉，不發送")
            return False
        return bool(notify(payload))

    try:
        broker.login()
    except LoginFailed as exc:
        # 登入失敗重試無用（密碼、憑證、聲明書都要人處理），直接以錯誤結束。
        # 但一定要發通知——靜默會讓人以為今天只是沒訊號。
        logger.error("登入失敗：%s", exc)
        notified = _send(build_no_signal_payload(f"登入失敗：{exc}", today))
        return EntryOutcome(
            signal=None, opens=None, notified=notified, exit_code=1, failure=str(exc)
        )

    try:
        opens = _fetch_open_prices(
            broker,
            attempts=config.quote_retry_attempts,
            interval=config.quote_retry_interval_seconds,
            sleep=sleep,
        )
    except QuoteNotReady as exc:
        # 拿不到資料是預期內的情況（報價主機、商品下架、颱風），不是程式錯誤，
        # 所以 exit_code 為 0。但今天沒有訊號，要說清楚是哪個商品出問題。
        logger.error("開盤價始終未就緒：%s", exc)
        notified = _send(build_no_signal_payload(str(exc), today))
        return EntryOutcome(
            signal=None, opens=None, notified=notified, exit_code=0, failure=str(exc)
        )

    logger.info("開盤價 大台=%s 小台=%s 微台=%s", opens.tx, opens.mtx, opens.tmf)
    result = strategy.compute(tx=opens.tx, mtx=opens.mtx, tmf=opens.tmf)
    logger.info("訊號=%s", result.signal)

    notified = _send(build_signal_payload(result, today))
    return EntryOutcome(signal=result.signal, opens=opens, notified=notified, exit_code=0)


def main() -> int:
    """正式進入點：組裝真實相依後執行進場流程。

    這裡是**唯一**建立真實 broker 與 Discord 連線的地方；run_entry 本身不知道
    自己拿到的是真的還是假的，測試因此能在沒有 COM 的機器上驗證整條流程。
    """
    import settings
    from broker.capital import CapitalBroker
    from notifiers.discord import send

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = settings.load()
    user_id = settings.read_env("CAPITAL_USER_ID")
    password = settings.read_env("CAPITAL_PASSWORD")
    if not user_id or not password:
        logger.error("找不到 CAPITAL_USER_ID / CAPITAL_PASSWORD，請檢查 .env")
        return 1

    webhook = settings.read_env("DISCORD_WEBHOOK_URL")
    outcome = run_entry(
        config,
        today=date.today(),
        broker=CapitalBroker(user_id, password),
        notify=lambda payload: send(payload, webhook),
    )
    logger.info("結束：訊號=%s exit_code=%s", outcome.signal, outcome.exit_code)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
