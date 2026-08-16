"""OS 策略的進入點。

依 ADR-0001，本程式由排程各叫醒一次、跑完就結束，不是常駐服務。

    python main.py entry    08:50 進場
    python main.py exit     13:40 出場（結算日 13:30）

兩班是**獨立的執行**，中間靠狀態檔溝通。早上那班異常結束時，
下午那班仍然會被排程觸發——部位不會因為早上出事就沒人管。

外部相依（broker、通知、sleep）由呼叫端傳入，不在此處建立——
測試因此能在沒有群益 COM、沒有帳號的機器上驗證整條流程。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import date

import strategy
from broker import (
    BUY,
    EXIT,
    SELL,
    FillUnknown,
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
    UNCERTAIN,
    PositionRecord,
    StateCorrupted,
    read_position,
    write_position,
)
from notifiers.discord import (
    build_exit_blocked_payload,
    build_exit_failed_payload,
    build_fill_unknown_payload,
    build_partial_exit_payload,
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

    ⚠️ **這是整個系統唯一會動到錢的地方。**

    兩道關卡在這裡：自動下單開關關閉、訊號是不動作。
    另外兩道（今天已經進過場、狀態檔讀不懂）在 `run_entry` 的更前面——
    依 SPEC 的進場流程，重複執行保護是第 2 步，排在登入與發報之前。
    放到這裡才檢查的話，重跑會再發一則一模一樣的 Discord。
    """
    if not config.auto_order_enabled:
        logger.info("自動下單已關閉，只發訊號")
        return None

    side = _SIDE_BY_SIGNAL.get(signal)
    if side is None:
        logger.info("訊號為不動作，不送出委託")
        return None

    contract = contracts[config.order_product]
    if contract.has_expired(today):
        # 下單代碼是「商品代號＋月份兩碼」的形式，群益在該月過期時會自動改送
        # 隔年同月且不報錯。寧可今天不交易，也不要交易到隔年的合約。
        raise OrderFailed(
            f"{contract.code} 的最後交易日 {contract.last_trading_day} 已過，"
            "不送出委託（過期的月份代碼會被自動改成隔年同月）"
        )

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
    try:
        result = broker.place_order(request)
    except FillUnknown as exc:
        # **委託送出去了，但不知道成交幾口。** 先把「我送了單」這件事寫下來，
        # 再讓例外往上走。順序不能反——寫檔在後的話，中途出事就什麼記錄都沒有，
        # 下午那班會以為今天沒進場，帳上的部位就直接進夜盤。
        record = PositionRecord(
                trading_day=to_yyyymmdd(today),
                product=request.product,
                order_code=request.order_code,
                contract_month=request.contract_month,
                side=request.side,
                lots=None,                       # 成交幾口不知道，不是 0
                requested_lots=request.lots,     # 但送出去幾口是知道的——曝險上界
                last_trading_day=contract.last_trading_day,
                status=UNCERTAIN,
                order_seq=exc.order_seq,         # 讓使用者能在券商 APP 直接查到那一筆
        )
        write_position(record, path=state_path)
        # 把記錄掛在例外上帶給呼叫端。在 except 區段裡重新讀檔的話，
        # 那次讀本身可能拋 StateCorrupted，於是例外逃出 run_entry 而一則通知都不發。
        exc.record = record
        raise

    if result.filled_lots <= 0:
        # 完全沒成交就沒有部位。寫下記錄的話，下午會去平一個不存在的東西，
        # 而那筆反向委託會變成新倉。
        logger.warning("委託未成交（0 口），不寫入狀態檔")
        return result

    write_position(
        PositionRecord(
            trading_day=to_yyyymmdd(today),
            product=request.product,
            order_code=request.order_code,     # 出場要用這個送反向委託
            contract_month=request.contract_month,
            side=request.side,
            lots=result.filled_lots,      # 實際成交，不是委託口數
            requested_lots=request.lots,
            # 出場那班靠這個判斷今天是不是結算日（結算日不重試）。
            # 現在寫下來，13:40 就不必再連一次報價主機去查商品清單。
            last_trading_day=contract.last_trading_day,
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
    *,
    sleep=time.sleep,
    state_path: str,
) -> EntryOutcome:
    """進場流程：登入 → 取開盤價（含重試）→ 算訊號 → 發報。

    `notify` 是一個吃 payload、回傳是否成功的可呼叫物件。

    `state_path` **刻意沒有預設值**。給了預設值的話，忘記傳的測試會靜靜地
    讀寫專案裡真正的 `state/position.json`——那既會污染開發機的部位記錄，
    也會讓測試結果取決於那個檔案當下的內容。現在忘記傳就是 TypeError。
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

    def _order_failed(reason: str, result, opens, notified: bool, **extra) -> EntryOutcome:
        """訊號算出來也發出去了，但委託沒送成功。

        `signal` 與 `opens` **照實填上**——今天確實有訊號，而且已經發出去了。
        填 None 會與 CONTEXT.md 的「無法判斷」（根本沒算出訊號）撞在一起，
        那是兩種完全不同的處境。

        `notified` 沿用訊號那一則的實際結果，不寫死——Discord 關閉時它是 False。
        `exit_code` 非 0，因為這不是預期內的一天，需要有人去看帳戶。
        """
        logger.error("%s", reason)
        _send(build_order_failed_payload(reason, today))
        return EntryOutcome(
            signal=result.signal, opens=opens, notified=notified,
            exit_code=1, failure=reason, **extra,
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

    # SPEC 進場流程第 2 步：重複執行保護，排在登入與發報**之前**。
    # 放到下單那一步才檢查的話，重跑不會重複下單，卻會再發一則一模一樣的訊號。
    #
    # 讀壞掉時不在這裡爆——訊號本身還是算得出來也值得發，
    # 只是不能下單。留到第 7 步再處理（見下方 state_error）。
    try:
        existing = read_position(path=state_path)
        state_error = ""
    except StateCorrupted as exc:
        existing, state_error = None, str(exc)

    if existing is not None and existing.trading_day == to_yyyymmdd(today):
        logger.info("今日已有部位記錄（%s %s %s 口，狀態 %s），不重複進場",
                    existing.product, existing.side, existing.lots, existing.status)

        if existing.is_uncertain:
            # ⚠️ **不確定的時候不可以靜默。** 人會再跑一次，多半正是因為想知道
            #    現在怎麼了；這時什麼都不說，看起來就像「已經沒事了」，
            #    但帳上可能還有一個沒人管的部位。
            reason = "早上送出的委託仍未確認成交，狀態尚未解決"
            logger.error("%s", reason)
            _send(build_fill_unknown_payload(existing, reason, today))
            return EntryOutcome(
                signal=None, opens=None, notified=True, exit_code=1, failure=reason
            )

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
    failed = lambda reason: _order_failed(  # noqa: E731
        reason, result=result, opens=opens, notified=notified,
        contracts=contracts, is_settlement_day=settlement,
    )

    if state_error:
        # 讀不懂就不知道帳上有沒有部位，這時候下單可能變成加倉或反向新倉。
        # 不確定的時候什麼都不做，比猜一個好。
        return failed(f"狀態檔異常，未下單：{state_error}")

    try:
        _place_entry_order(
            config, broker, result.signal, contracts,
            today=today, state_path=state_path,
        )
    except FillUnknown as exc:
        # 委託送出去了但回報沒到。記錄已經在 _place_entry_order 裡寫好了，
        # 這裡只負責叫人去看帳戶——訊息刻意與「下單失敗」不同：
        # 那一則的正確反應是「不用管」，這一則是「馬上去確認部位」。
        logger.error("成交回報未確認：%s", exc)
        _send(build_fill_unknown_payload(exc.record, str(exc), today))
        return EntryOutcome(
            signal=result.signal, opens=opens, notified=notified, exit_code=1,
            failure=f"成交回報未確認：{exc}",
            contracts=contracts, is_settlement_day=settlement,
        )
    except OrderFailed as exc:
        return failed(f"委託送出失敗：{exc}")
    except Exception as exc:  # noqa: BLE001
        # 與登入那一段同樣的理由：COM 壞掉時拋的不是 OrderFailed，
        # 少了這一條，例外會逃出去而使用者只看到訊號、以為單下好了。
        return failed(f"下單時發生非預期錯誤（{type(exc).__name__}）：{exc}")

    return EntryOutcome(
        signal=result.signal, opens=opens, notified=notified, exit_code=0,
        contracts=contracts, is_settlement_day=settlement,
    )


@dataclass(frozen=True)
class ExitOutcome:
    """出場流程的結果。

    | 結局 | `exited` | `remaining` | `exit_code` |
    |------|----------|-------------|-------------|
    | 沒事做（無記錄／已出場／非交易日） | False | 0 | 0 |
    | 完全平掉 | **True** | 0 | 0 |
    | 部分平掉 | False | 剩幾口 | 1 |
    | 完全失敗 | False | 全部 | 1 |
    | 不確定，拒絕出場 | False | 不知道（None） | 1 |

    `exited` 只有**全部平掉**才是 True。部分成交不算——標成 True 的話，
    隔日對帳會以為一切正常，而殘留的口數還在帳上。
    """

    exited: bool
    remaining: int | None
    notified: bool
    exit_code: int
    failure: str | None = None
    skipped: bool = False


_OPPOSITE = {BUY: SELL, SELL: BUY}


def run_exit(
    config: Config,
    today: date,
    broker,
    notify,
    *,
    sleep=time.sleep,
    state_path: str,
) -> ExitOutcome:
    """出場流程：讀狀態檔 → 送出等量反向委託 → 更新狀態檔。

    刻意**不查商品清單**：下單代碼與最後交易日在 08:50 就寫進狀態檔了。
    13:40 再去連一次報價主機只是多一個會失敗的地方，而那時候失敗的代價是部位過夜。
    """

    def _send(payload) -> bool:
        if not config.discord_enabled:
            logger.info("Discord 已關閉，不發送")
            return False
        return bool(notify(payload))

    def _quiet() -> ExitOutcome:
        return ExitOutcome(exited=False, remaining=0, notified=False,
                           exit_code=0, skipped=True)

    if not is_trading_day(
        today,
        extra_closures=config.calendar_extra_closures,
        extra_openings=config.calendar_extra_openings,
    ):
        logger.info("%s 非交易日，靜默結束", today)
        return _quiet()

    try:
        record = read_position(path=state_path)
    except StateCorrupted as exc:
        # 讀不懂就不知道帳上有沒有部位。這時候送單可能平掉不存在的東西
        # （＝開出反向新倉），也可能什麼都不做而讓部位過夜。交給人。
        reason = f"狀態檔異常，未出場：{exc}"
        logger.error("%s", reason)
        notified = _send(build_no_signal_payload(reason, today))
        return ExitOutcome(exited=False, remaining=None, notified=notified,
                           exit_code=1, failure=reason)

    if record is None or record.trading_day != to_yyyymmdd(today):
        logger.info("沒有今日的部位記錄，不做任何事")
        return _quiet()
    if record.exited:
        logger.info("今日部位已出場，不重複送單")
        return _quiet()

    if record.is_uncertain:
        # ticket 05 定的契約：不知道持有幾口就不准下單。
        reason = "不確定持有幾口，未自動出場"
        logger.error("%s", reason)
        notified = _send(build_exit_blocked_payload(record, today))
        return ExitOutcome(exited=False, remaining=None, notified=notified,
                           exit_code=1, failure=reason)

    if not config.auto_order_enabled:
        logger.info("自動下單已關閉，不送出場委託")
        return _quiet()

    request = OrderRequest(
        product=record.product,
        order_code=record.order_code,
        contract_month=record.contract_month,
        side=_OPPOSITE[record.side],
        lots=record.lots,
        intent=EXIT,
    )
    # 結算日合約 13:30 就停止交易，重試必然失敗、只會拖延告警。
    # 未平倉部位由交易所現金結算，所以重點是**趕快通知人**，不是多試兩次。
    settlement = record.is_settlement_day(today)
    attempts = 1 if settlement else config.quote_retry_attempts
    if settlement:
        logger.info("今天是結算日，出場不重試")

    result, last_error = None, None
    for attempt in range(1, attempts + 1):
        try:
            broker.login()
            result = broker.place_order(request)
            break
        except (OrderFailed, FillUnknown) as exc:
            last_error = exc
            logger.warning("第 %d/%d 次出場失敗：%s", attempt, attempts, exc)
            if attempt < attempts:
                sleep(config.quote_retry_interval_seconds)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.error("出場時發生非預期錯誤（%s）：%s", type(exc).__name__, exc)
            break

    if result is None:
        reason = f"出場委託送出失敗：{last_error}"
        notified = _send(build_exit_failed_payload(record, str(last_error), today))
        return ExitOutcome(exited=False, remaining=record.lots, notified=notified,
                           exit_code=1, failure=reason)

    remaining = record.lots - result.filled_lots
    if remaining > 0:
        # 部分成交**不算出場成功**。狀態檔記下還剩幾口，隔日對帳才看得到真相。
        reason = f"出場只成交 {result.filled_lots} 口，還剩 {remaining} 口"
        logger.error("%s", reason)
        write_position(replace(record, lots=remaining), path=state_path)
        notified = _send(build_partial_exit_payload(record, remaining, today))
        return ExitOutcome(exited=False, remaining=remaining, notified=notified,
                           exit_code=1, failure=reason)

    logger.info("出場完成，平掉 %d 口", result.filled_lots)
    write_position(replace(record, exited=True), path=state_path)
    # 例行出場不發 Discord——使用者要求每天只有一則訊息（早上那則訊號）。
    return ExitOutcome(exited=True, remaining=0, notified=False, exit_code=0)


def _build_runtime():
    """組裝真實相依。**這裡是唯一建立真實 broker 與 Discord 連線的地方**——

    `run_entry` / `run_exit` 都不知道自己拿到的是真的還是假的，
    測試因此能在沒有 COM 的機器上驗證整條流程。

    回傳 `(config, broker, notify)`，或在設定不全時回傳 `None`。
    """
    import settings
    from broker.capital import CapitalBroker
    from notifiers.discord import send

    config = settings.load()
    user_id = settings.read_env("CAPITAL_USER_ID")
    password = settings.read_env("CAPITAL_PASSWORD")
    if not user_id or not password:
        logger.error("找不到 CAPITAL_USER_ID / CAPITAL_PASSWORD，請檢查 .env")
        return None

    account = settings.read_env("CAPITAL_FUTURES_ACCOUNT")
    if config.auto_order_enabled and not account:
        # 開著開關卻沒帳號，等於每天跑到最後一步才失敗。早點講。
        logger.error(
            "自動下單已開啟但找不到 CAPITAL_FUTURES_ACCOUNT，"
            "請執行 tools/verify_login.py --show-account 查出後填入 .env"
        )
        return None

    logger.info("連線環境=%s 自動下單=%s 標的=%s %d 口",
                config.capital_environment,
                "開啟" if config.auto_order_enabled else "關閉",
                config.order_product, config.order_lots)

    webhook = settings.read_env("DISCORD_WEBHOOK_URL")
    broker = CapitalBroker(
        user_id, password,
        environment=config.capital_environment,
        account=account,
        fill_timeout=config.order_fill_timeout_seconds,
    )
    return config, broker, (lambda payload: send(payload, webhook))


def main(argv=None) -> int:
    """排程的進入點。兩班各叫一次：

        python main.py entry    08:50 進場
        python main.py exit     13:40 出場（結算日 13:30）

    ⚠️ **刻意做成兩個獨立的執行**（ADR-0001）。早上那班異常結束時，
    下午那班仍然會被排程觸發——部位不會因為早上出事就沒人管。
    """
    import argparse

    parser = argparse.ArgumentParser(description="OS 策略")
    parser.add_argument("stage", choices=("entry", "exit"),
                        help="entry=08:50 進場，exit=13:40 出場")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    runtime = _build_runtime()
    if runtime is None:
        return 1
    config, broker, notify = runtime

    today = date.today()
    if args.stage == "entry":
        outcome = run_entry(config, today=today, broker=broker, notify=notify,
                            state_path=STATE_PATH)
        logger.info("進場結束：訊號=%s exit_code=%s", outcome.signal, outcome.exit_code)
    else:
        outcome = run_exit(config, today=today, broker=broker, notify=notify,
                           state_path=STATE_PATH)
        logger.info("出場結束：已出場=%s 殘留=%s exit_code=%s",
                    outcome.exited, outcome.remaining, outcome.exit_code)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
