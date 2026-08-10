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
from broker import (
    LoginFailed,
    OpenPrices,
    ProductListUnavailable,
    QuoteNotReady,
    to_yyyymmdd,
)
from calendar_tw import is_trading_day
from notifiers.discord import build_no_signal_payload, build_signal_payload
from settings import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryOutcome:
    """進場流程的結果。四種結局要分得開：

    | 結局 | `signal` | `skipped` | `failure` | `exit_code` |
    |------|----------|-----------|-----------|-------------|
    | 正常 | LONG / SHORT / NO_TRADE | False | None | 0 |
    | 無法判斷（資料拿不到） | None | False | 有原因 | 0 |
    | 非交易日 | None | **True** | None | 0 |
    | 致命（登入失敗等） | None | False | 有原因 | 1 |

    「不動作」是判斷出來的結果，「無法判斷」是根本沒判斷，「非交易日」是今天不營業——
    混為一談會讓故障被當成正常的一天。
    """

    signal: str | None
    opens: OpenPrices | None
    notified: bool
    exit_code: int
    failure: str | None = None
    skipped: bool = False
    contracts: dict | None = None
    is_settlement_day: bool = False


def _fetch_open_prices(broker, attempts: int, interval: int, sleep, trading_day: int) -> OpenPrices:
    """取開盤價，未就緒就重試。全部用完仍未就緒 → 讓最後一個 QuoteNotReady 冒出去。

    重試存在的理由是 08:50 的報價偶爾晚幾十秒才齊；間隔比次數重要，
    因為「未就緒」的時間尺度是秒到分，不是毫秒。
    """
    last_error: QuoteNotReady | None = None
    for attempt in range(1, attempts + 1):
        try:
            return broker.get_open_prices(expected_trading_day=trading_day)
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

    def _fatal(reason: str) -> EntryOutcome:
        logger.error("致命錯誤：%s", reason)
        notified = _send(build_no_signal_payload(reason, today))
        return EntryOutcome(
            signal=None, opens=None, notified=notified, exit_code=1, failure=reason
        )

    def _no_signal(reason: str, **extra) -> EntryOutcome:
        """今天算不出訊號，但不是程式錯誤（報價未就緒、商品清單查不到等）。

        exit_code 為 0——這些是預期內的情況，用錯誤碼會讓排程誤報。
        """
        logger.error("無法判斷：%s", reason)
        notified = _send(build_no_signal_payload(reason, today))
        return EntryOutcome(
            signal=None, opens=None, notified=notified, exit_code=0,
            failure=reason, **extra,
        )

    # 非交易日：什麼都不做，連登入都不做。
    # 「連登入都不做」是刻意的——登入失敗會發告警，而 Discord 的沉默
    # 只准有一種解釋：今天休市。發任何訊息都會破壞這個約定。
    if not is_trading_day(
        today,
        extra_closures=config.calendar_extra_closures,
        extra_openings=config.calendar_extra_openings,
    ):
        logger.info("%s 非交易日，靜默結束", today)
        return EntryOutcome(
            signal=None, opens=None, notified=False, exit_code=0, skipped=True
        )

    try:
        broker.login()
    except LoginFailed as exc:
        # 登入失敗重試無用（密碼、憑證、聲明書都要人處理），直接以錯誤結束。
        return _fatal(f"登入失敗：{exc}")
    except Exception as exc:  # noqa: BLE001
        # ⚠️ 這一條不可以拿掉。COM 元件沒註冊會拋 ImportError、CreateObject 會拋 OSError，
        # 都不是 LoginFailed。少了它，例外會直接逃出 run_entry 而**一則通知都不發**——
        # 使用者會以為今天只是沒訊號，實際上程式根本沒跑起來。
        return _fatal(f"登入時發生非預期錯誤（{type(exc).__name__}）：{exc}")

    # 近月合約與結算日都來自商品清單，不由日期推算——
    # 最後交易日遇假日會順延，「每月第三個週三」那條算式抓不到（見 ticket 03）。
    try:
        contracts = broker.get_contracts()
    except ProductListUnavailable as exc:
        return _no_signal(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fatal(f"取商品清單時發生非預期錯誤（{type(exc).__name__}）：{exc}")

    settling = {code for code, c in contracts.items() if c.is_settlement_day(today)}
    settlement = bool(settling)
    if settling and len(settling) != len(contracts):
        # 三個台指商品的最後交易日理應相同。不同就是異常，要看得見。
        logger.warning("只有部分商品到期：%s，仍以結算日處理（提早出場較安全）", sorted(settling))
    logger.info(
        "近月合約 %s%s",
        {code: c.contract_month for code, c in contracts.items()},
        "（今天是結算日）" if settlement else "",
    )

    try:
        opens = _fetch_open_prices(
            broker,
            attempts=config.quote_retry_attempts,
            interval=config.quote_retry_interval_seconds,
            sleep=sleep,
            # 報價必須屬於今天。休市或尚未換日時群益會給上一交易日的價格，
            # 而那看起來完全正常——這是唯一 L1/L2 都攔不住的錯誤。
            trading_day=to_yyyymmdd(today),
        )
    except QuoteNotReady as exc:
        return _no_signal(str(exc), contracts=contracts, is_settlement_day=settlement)
    except Exception as exc:  # noqa: BLE001
        # 非 QuoteNotReady 的例外不重試——重試 ImportError 三次沒有意義，只是拖時間。
        return _fatal(f"取開盤價時發生非預期錯誤（{type(exc).__name__}）：{exc}")

    logger.info("開盤價 大台=%s 小台=%s 微台=%s", opens.tx, opens.mtx, opens.tmf)
    result = strategy.compute(tx=opens.tx, mtx=opens.mtx, tmf=opens.tmf)
    logger.info("訊號=%s", result.signal)

    notified = _send(build_signal_payload(result, today))
    return EntryOutcome(
        signal=result.signal, opens=opens, notified=notified, exit_code=0,
        contracts=contracts, is_settlement_day=settlement,
    )


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
