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
    BUY,
    SELL,
    LoginFailed,
    OpenPrices,
    OrderFailed,
    OrderRequest,
    ProductListUnavailable,
    QuoteNotReady,
    to_yyyymmdd,
)
from calendar_tw import is_trading_day
from state import (
    STATE_PATH,
    PositionRecord,
    StateCorrupted,
    read_position,
    write_position,
)
from notifiers.discord import (
    build_no_signal_payload,
    build_order_failed_payload,
    build_signal_payload,
)
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


_SIDE_BY_SIGNAL = {strategy.LONG: BUY, strategy.SHORT: SELL}


def _place_entry_order(
    config: Config, broker, signal: str, contracts: dict, today: date, state_path: str
):
    """依訊號送出進場委託，成交後寫入狀態檔。

    ⚠️ **這是整個系統唯一會動到錢的地方。** 所有「不下單」的條件都集中在這裡，
    不散到呼叫端——「什麼情況下會下單」只該有一個地方要讀。

    四道關卡，任何一道不通過就完全不碰下單 API：
      1. 自動下單開關關閉
      2. 訊號是不動作
      3. 狀態檔讀不懂（拋 `StateCorrupted`，由呼叫端告警）
      4. 今天已經進過場（排程重試或人工重跑，不可以變成兩倍部位）
    """
    if not config.auto_order_enabled:
        logger.info("自動下單已關閉，只發訊號")
        return None

    side = _SIDE_BY_SIGNAL.get(signal)
    if side is None:
        logger.info("訊號為不動作，不送出委託")
        return None

    # 讀不懂就停手。分不出「確實沒有部位」與「檔案壞了」時下單，
    # 可能變成加倉或反向新倉——不確定的時候什麼都不做比猜一個安全。
    existing = read_position(path=state_path)
    if existing is not None and existing.trading_day == to_yyyymmdd(today):
        logger.info("今日已有部位記錄（%s %s %d 口），不重複下單",
                    existing.product, existing.side, existing.lots)
        return None

    contract = contracts[config.order_product]
    request = OrderRequest(
        product=config.order_product,
        # 下單代碼與報價代碼是兩回事（MTX08 vs MTX00AM），來源同樣是商品清單。
        order_code=contract.order_code,
        # 年月來自商品清單。群益的月份代碼過期時會自動改送隔年同月，指名年月才擋得住。
        contract_month=contract.contract_month,
        side=side,
        lots=config.order_lots,
    )
    logger.info("送出委託 %s（%s）%s %d 口 %s",
                request.order_code, request.product, request.side,
                request.lots, request.contract_month)
    result = broker.place_order(request)

    if result.filled_lots <= 0:
        # 完全沒成交就沒有部位。寫下記錄的話，下午會去平一個不存在的東西，
        # 而那筆反向委託會變成新倉。
        logger.warning("委託未成交（0 口），不寫入狀態檔")
        return result

    write_position(
        PositionRecord(
            trading_day=to_yyyymmdd(today),
            product=request.product,
            contract_month=request.contract_month,
            side=request.side,
            lots=result.filled_lots,      # 實際成交，不是委託口數
            order_seq=result.order_seq,
        ),
        path=state_path,
    )
    return result


def run_entry(
    config: Config,
    today: date,
    broker,
    notify,
    sleep=time.sleep,
    state_path: str | None = None,
) -> EntryOutcome:
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

    def _order_failed(reason: str) -> EntryOutcome:
        """訊號算出來也發出去了，但委託沒送成功。

        `signal` 仍然填上——那是事實，今天確實有訊號；
        `exit_code` 非 0 因為這不是預期內的一天，需要有人去看帳戶。
        """
        logger.error("%s", reason)
        _send(build_order_failed_payload(reason, today))
        return EntryOutcome(
            signal=None, opens=None, notified=True, exit_code=1, failure=reason
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

    # 發報在下單**之前**。訊號是這個系統的主要產出，下單是附加的；
    # 下單那一步失敗時，使用者至少已經收到今天該做什麼。
    notified = _send(build_signal_payload(result, today))

    try:
        _place_entry_order(
            config, broker, result.signal, contracts,
            today=today,
            # 模組層級的 STATE_PATH 在這裡取值（而不是預設參數），
            # 測試才能用 monkeypatch 換掉它，不會寫到真正的狀態檔。
            state_path=state_path or STATE_PATH,
        )
    except StateCorrupted as exc:
        return _order_failed(f"狀態檔異常，未下單：{exc}")
    except OrderFailed as exc:
        return _order_failed(f"委託送出失敗：{exc}")
    except Exception as exc:  # noqa: BLE001
        # 與登入那一段同樣的理由：COM 壞掉時拋的不是 OrderFailed，
        # 少了這一條，例外會逃出去而使用者只看到訊號、以為單下好了。
        return _order_failed(f"下單時發生非預期錯誤（{type(exc).__name__}）：{exc}")

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

    account = settings.read_env("CAPITAL_FUTURES_ACCOUNT")
    if config.auto_order_enabled and not account:
        # 開著開關卻沒帳號，等於每天跑到最後一步才失敗。早點講。
        logger.error(
            "自動下單已開啟但找不到 CAPITAL_FUTURES_ACCOUNT，"
            "請執行 tools/verify_login.py --show-account 查出後填入 .env"
        )
        return 1

    logger.info("連線環境=%s 自動下單=%s 標的=%s %d 口",
                config.capital_environment,
                "開啟" if config.auto_order_enabled else "關閉",
                config.order_product, config.order_lots)

    webhook = settings.read_env("DISCORD_WEBHOOK_URL")
    outcome = run_entry(
        config,
        today=date.today(),
        broker=CapitalBroker(
            user_id, password,
            environment=config.capital_environment,
            account=account,
        ),
        notify=lambda payload: send(payload, webhook),
    )
    logger.info("結束：訊號=%s exit_code=%s", outcome.signal, outcome.exit_code)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
