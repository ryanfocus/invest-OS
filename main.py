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
    ENTRY,
    EXIT,
    SELL,
    FillUnknown,
    LoginFailed,
    OpenPrices,
    OrderFailed,
    OrderRequest,
    OrderResult,
    ProductListUnavailable,
    QuoteNotReady,
    TX_CODE,
    to_yyyymmdd,
)
from calendar_tw import is_trading_day
from observations import (
    OBSERVATIONS_PATH,
    Observation,
    ObservationConflict,
    append_observation,
)
from reconcile import run_reconciliation
from state import (
    BY_EXIT,
    BY_SETTLEMENT,
    CONFIRMED,
    STATE_PATH,
    UNCERTAIN,
    UNCERTAIN_ENTRY,
    UNCERTAIN_EXIT,
    PositionRecord,
    StateCorrupted,
    clear_position,
    read_position,
    write_position,
)
from notifiers.discord import (
    build_exit_blocked_payload,
    build_exit_failed_payload,
    build_exit_state_broken_payload,
    build_exit_unknown_payload,
    build_fill_unknown_payload,
    build_observation_conflict_payload,
    build_partial_exit_payload,
    build_settlement_payload,
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
        intent=ENTRY,
    )
    logger.info("送出委託 %s（%s）%s %d 口 %s",
                request.order_code, request.product, request.side,
                request.lots, request.contract_month)
    try:
        result = broker.place_order(request)
    except FillUnknown as exc:
        # **委託送出去了，但不知道成交幾口。**
        #
        # ⚠️ **先寫檔，再查詢。順序不可以反。**
        #    查詢是補救措施，而寫檔是「我送了一張單」這個既成事實的唯一記錄。
        #    先查的話，查詢那一步若拋出任何東西（連線、COM、假 broker 的斷言），
        #    例外會帶著整個函式離開而**什麼都沒寫**——13:40 那班會以為今天沒進場，
        #    帳上的部位直接進夜盤，而早上的 Discord 還說「委託沒有送出去」。
        #    （2026-08-19 code-review 抓到：ticket 09 初版把順序寫反了。）
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
                uncertain_stage=UNCERTAIN_ENTRY,  # 不確定的是**進場**那一筆
                order_seq=exc.order_seq,         # 讓使用者能在券商 APP 直接查到那一筆
        )
        write_position(record, path=state_path)

        # 記錄寫好了，現在才問（ticket 09）。推播與查詢走不同元件、不同連線、
        # 不同通訊方式，失效原因幾乎不重疊——推播沒來最可能的解釋是
        # 「推播管線壞了」，而那正是查詢救得起來的情況。
        lots = broker.query_filled_lots(
            order_seq=exc.order_seq,
            trading_day=to_yyyymmdd(today),
            requested_lots=request.lots,
        )
        if lots is not None:
            logger.info("成交查詢補上了答案：%d 口", lots)
            if lots == 0:
                # **確定沒成交**——與「查不到」是完全不同的答案。
                # 清掉剛才那筆記錄：下午沒有東西要平，留著它會去平一個
                # 不存在的部位，那筆反向委託會變成新倉。
                logger.warning("成交查詢確認未成交（0 口），清除狀態檔")
                clear_position(path=state_path)
                return OrderResult(filled_lots=0, order_seq=exc.order_seq)
            write_position(
                replace(record, lots=lots, status=CONFIRMED, uncertain_stage=""),
                path=state_path,
            )
            return OrderResult(filled_lots=lots, order_seq=exc.order_seq)

        # 查詢也答不出來 → 維持「不確定」。
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


def _fetch_official_opens(trading_day: int):
    """對帳的預設取數來源：期交所。

    刻意做成一個函式而不是直接 import 到模組頂層——`taifex` 會拉進 `requests`，
    而這個 import 若失敗，放在頂層會讓**整個 main.py 載不起來**（連訊號都發不出去）。
    放在這裡，失敗就只是「今天沒對到帳」。
    """
    from taifex import fetch_official_opens
    return fetch_official_opens(trading_day)


def _resolve_uncertain(broker, record, today: date) -> int | None:
    """出場前替一筆「不確定」的記錄問一次成交結果（ticket 09）。

    回 `None` 代表還是不知道 → 走 ticket 05 的老路（不送單、發 Discord 要人處理）。

    **登入放在這裡。** 查詢需要下單元件與帳號，而 `run_exit` 更前面那些路徑
    （非交易日、沒有今日記錄、結算日）根本不該連線。

    整段不拋例外：這是失敗路徑上的補救措施，自己再炸一次的話，
    原本只是「要人看一眼」的一天會變成整班掛掉。
    """
    try:
        broker.login()
        return broker.query_filled_lots(
            order_seq=record.order_seq,
            trading_day=record.trading_day,
            requested_lots=record.requested_lots,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("出場前的成交查詢失敗（%s）：%s", type(exc).__name__, exc)
        return None


def _record_observation(result, opens, contracts, today: date, path: str, send) -> None:
    """留下今天這一筆觀測。**寫失敗絕不往外拋。**

    寫不進去的代價是「少對一天的帳」；讓例外冒出去的代價是**整天沒有訊號**，
    而訊號才是這個系統的主要產出。兩者不成比例。

    ⚠️ 但也不可以靜悄悄——這條路徑一旦長期失敗，隔日對帳會每天「沒東西可對」
    而安靜跳過，看起來跟一切正常一模一樣。所以失敗記 ERROR。
    """
    try:
        append_observation(
            Observation(
                trading_day=to_yyyymmdd(today),
                tx=opens.tx, mtx=opens.mtx, tmf=opens.tmf,
                signal=result.signal,
                # 只記大台的月份。三個商品的近月理應相同，不同時上面
                # 「只有部分商品到期」那條已經會以 WARNING 記錄。
                contract_month=contracts[TX_CODE].contract_month,
            ),
            path=path,
        )
    except ObservationConflict as exc:
        # 同一天出現**兩組不同的開盤價**。這不是「重複執行」那麼單純——
        # 報價來源在同一天給了兩個答案，而這個系統整個是建立在那三個數字上的。
        #
        # ⚠️ 一定要發出去。初版只記 log（code-review 2026-08-18 抓到），
        #    於是這個例外形同虛設：唯一的呼叫端把它吞掉，沒有人會知道。
        logger.error("%s", exc)
        try:
            send(build_observation_conflict_payload(to_yyyymmdd(today), str(exc)))
        except Exception as send_exc:  # noqa: BLE001
            logger.error("觀測衝突告警送不出去：%s", send_exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("觀測記錄寫入失敗（%s）：%s", type(exc).__name__, exc)


def run_entry(
    config: Config,
    today: date,
    broker,
    notify,
    *,
    sleep=time.sleep,
    state_path: str,
    observations_path: str,
    fetch_official=None,
) -> EntryOutcome:
    """進場那一班的完整流程：策略跑完，再對前一交易日的帳。

    分成兩層是因為驗收條件要求對帳「在訊號發報與下單全部完成之後才執行」，
    而策略流程（`_run_strategy`）有 6 個提前返回的出口。逐一補會漏，
    包在外面只有一個地方要顧。兩者的差別就是**這一層多做了對帳**。

    `fetch_official` 是取官方資料的函式，預設連期交所。傳進來是為了讓測試
    餵它壞東西（斷線、缺商品、格式看不懂）而不需要真的連外。
    """
    outcome = _run_strategy(
        config, today, broker, notify,
        sleep=sleep, state_path=state_path, observations_path=observations_path,
    )

    # 非交易日與重複執行都不對帳（`skipped`）。
    # 非交易日的約定是「不登入、不發任何訊息」，而對帳會連外抓資料；
    # 重複執行那次的帳，同一天第一次跑的時候就對過了。
    if not outcome.skipped:
        # ⚠️ 預設取數函式**不在這裡 import**，而是包成一個延後 import 的小函式。
        #    在這裡 import 的話，`import taifex` 失敗（缺 requests 等）會直接
        #    冒出 run_entry——而那時候單已經送出去了，整班會以 traceback 結束。
        #    包進函式裡，那個失敗就落在 `run_reconciliation` 的保護傘底下，
        #    變成「對帳略過」而不是「當天掛掉」。驗收條件要的正是這個。
        #
        #    `run_reconciliation` 自己保證不拋例外，所以這裡不必再包一層 try。
        #    那個保證有測試守著（test_reconcile.py 的「絕不礙事」那一組）。
        result = run_reconciliation(
            today=to_yyyymmdd(today),
            notify=notify,
            fetch_official=fetch_official or _fetch_official_opens,
            path=observations_path,
            discord_enabled=config.discord_enabled,
        )
        if result.checked_day is None:
            logger.info("對帳略過：%s", result.skipped)
        elif result.mismatches:
            logger.error("對帳：%s 有 %d 項不一致", result.checked_day, len(result.mismatches))
        else:
            logger.info("對帳：%s 一致", result.checked_day)

    return outcome


def _run_strategy(
    config: Config,
    today: date,
    broker,
    notify,
    *,
    sleep=time.sleep,
    state_path: str,
    observations_path: str,
) -> EntryOutcome:
    """進場流程：登入 → 取開盤價（含重試）→ 算訊號 → 發報 → 寫觀測 → 下單。

    `notify` 是一個吃 payload、回傳是否成功的可呼叫物件。

    `state_path` / `observations_path` **刻意都沒有預設值**。給了預設值的話，
    忘記傳的測試會靜靜地讀寫專案裡真正的 `state/`——那既會污染開發機的記錄，
    也會讓測試結果取決於那些檔案當下的內容。現在忘記傳就是 TypeError。
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

        if existing.uncertain_entry:
            # ⚠️ **不確定的時候不可以靜默。** 人會再跑一次，多半正是因為想知道
            #    現在怎麼了；這時什麼都不說，看起來就像「已經沒事了」，
            #    但帳上可能還有一個沒人管的部位。
            #
            # 用 `uncertain_entry` 而不是 `is_uncertain`：13:40 之後重跑早班時，
            # 記錄上的不確定可能是**出場**那一筆，而這則訊息講的是
            # 「早上送出的委託仍未確認成交／方向：買進」——方向與委託都是錯的。
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

    # 觀測記錄：**每個交易日都要留下一筆**，不動作與開關關著的日子也一樣。
    # 那些日子沒有部位記錄，而隔日對帳唯一的依據就是這一筆（ticket 07）。
    #
    # 位置是刻意的：
    #   發報**之後**——訊號是主要產出，不該被一個檔案寫入擋住
    #   下單**之前**——觀測是既成事實，與後面下單成不成功無關；
    #                   不動作的日子流程走到這裡就結束了，但這一行已經寫好
    _record_observation(result, opens, contracts, today, observations_path, _send)

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
        notified = _send(build_exit_state_broken_payload(reason, today))
        return ExitOutcome(exited=False, remaining=None, notified=notified,
                           exit_code=1, failure=reason)

    if record is None or record.trading_day != to_yyyymmdd(today):
        logger.info("沒有今日的部位記錄，不做任何事")
        return _quiet()
    if record.exited:
        logger.info("今日部位已出場，不重複送單")
        return _quiet()

    if record.is_settlement_day(today):
        # **結算日不送出場委託。** 合約 13:30 就停止交易，13:40 這一班送什麼都會被拒，
        # 而未平倉部位本來就會由交易所以最後結算價現金交割——部位一定會平掉。
        #
        # 這一段刻意放在「不確定」與「開關關閉」的前面：那兩條的作用是**阻止送單**，
        # 而結算日本來就不送，走到那邊只會發出一則叫使用者去做一件做不到的事的告警
        # （「請立刻手動送出賣出 N 口」——合約已經不能交易了）。
        # 不確定的情況改由本則訊息一併講清楚。
        #
        # 代價是當日損益以現貨指數的結算價計算，與用期貨價的回測有落差。
        # 那是已知且有界的，不是風險——見 SPEC「結算日」。
        logger.info("今天是結算日，部位交由交易所現金結算，不送出場委託")
        write_position(
            replace(record, exited=True, close_reason=BY_SETTLEMENT),
            path=state_path,
        )
        notified = _send(build_settlement_payload(record, today))
        return ExitOutcome(exited=True, remaining=0, notified=notified, exit_code=0)

    if record.uncertain_exit:
        # ⚠️ **不確定的是出場那一筆——一張單都不准再送。**
        #
        # 那筆可能已經成交了。再送一次等量反向委託，就是在已經平掉的帳上
        # 繼續賣（或買），開出一個沒人管的反向新倉。
        #
        # 而且**查詢在這裡幫不上忙**：`order_seq` 記的是出場單的序號，
        # 拿去查只會查到出場單自己的成交——看起來像「早上成交了 N 口」，
        # 那正是危險的地方（2026-08-19 code-review 實測重現）。
        #
        # 這是 `broker/__init__.py` 的 `FillUnknown` docstring 寫死的規則：
        # 可能已經成交的單，絕不可以自動再送一次。
        reason = "出場委託的成交狀況不確定，未重複送單"
        logger.error("%s", reason)
        notified = _send(build_exit_unknown_payload(record, reason, today))
        return ExitOutcome(exited=False, remaining=None, notified=notified,
                           exit_code=1, failure=reason)

    if record.is_uncertain:
        # 走到這裡代表不確定的是**進場**那一筆（`uncertain_entry`）。
        # 舉手投降之前先問一次（ticket 09）——早上推播沒到不代表現在也查不到，
        # 券商主機的紀錄什麼時候問都在，而推播是一次性的。
        #
        # ⚠️ **開關關著就不查。** 查詢會登入並初始化下單元件
        #    （`_ensure_order_ready`），而 SPEC 使用者故事 38 要求
        #    「只發 Discord、不下單」的模式下**完全不呼叫任何下單 API**——
        #    使用者關掉開關通常正是想切斷程式與券商的連線。
        #    查不成就照舊發「不確定」那則，訊息本身不需要 broker。
        lots = (_resolve_uncertain(broker, record, today)
                if config.auto_order_enabled else None)
        if lots is None:
            # 查詢也答不出來 → 走 ticket 05 定的契約：不知道持有幾口就不准下單。
            reason = "不確定持有幾口，未自動出場"
            logger.error("%s", reason)
            notified = _send(build_exit_blocked_payload(record, today))
            return ExitOutcome(exited=False, remaining=None, notified=notified,
                               exit_code=1, failure=reason)
        if lots == 0:
            # **確定早上沒成交**——沒有部位，今天什麼都不用做，也不必打擾人。
            # 與「查不到」是完全不同的答案。
            # 刪掉那筆「不確定」的記錄，而不是改寫成 0 口——
            # `PositionRecord` 的不變量刻意規定 CONFIRMED 必須有口數 ≥ 1，
            # 「確定沒有部位」在這套設計裡就是**沒有記錄**（進場那條也是這樣）。
            # 留著它等於讓檔案說謊：它說「不知道」，但我們已經知道了。
            logger.info("成交查詢確認早上未成交，今日無部位")
            clear_position(path=state_path)
            return _quiet()
        logger.info("成交查詢補上了答案：早上成交 %d 口，照常出場", lots)
        record = replace(record, lots=lots, status=CONFIRMED, uncertain_stage="")
        write_position(record, path=state_path)

    if not config.auto_order_enabled:
        # 開關關著時進場不會寫記錄，所以「有記錄 + 開關關著」代表有人中途關掉了。
        # 不自作主張送單（使用者剛把開關關掉），但**必須講出來**——
        # 靜默結束等於讓部位過夜而沒有人知道。
        reason = "自動下單已關閉，但帳上仍有今日部位記錄，未自動出場"
        logger.error("%s", reason)
        notified = _send(build_exit_blocked_payload(record, today))
        return ExitOutcome(exited=False, remaining=record.lots, notified=notified,
                           exit_code=1, failure=reason)

    request = OrderRequest(
        product=record.product,
        order_code=record.order_code,
        contract_month=record.contract_month,
        side=record.exit_side,
        lots=record.lots,
        intent=EXIT,
    )
    # 結算日在上面就回去了，走到這裡一定是一般交易日。
    attempts = config.quote_retry_attempts

    # 登入放在重試迴圈**外面**：重試的是委託，不是身分驗證。
    # 而且真正的 login() 會重新驗證憑證並等聲明書，每次重試都做一遍很浪費。
    try:
        broker.login()
    except Exception as exc:  # noqa: BLE001
        reason = f"出場登入失敗：{exc}"
        logger.error("%s", reason)
        notified = _send(build_exit_failed_payload(record, str(exc), today))
        return ExitOutcome(exited=False, remaining=record.lots, notified=notified,
                           exit_code=1, failure=reason)

    result, last_error = None, None
    for attempt in range(1, attempts + 1):
        try:
            result = broker.place_order(request)
            break
        except FillUnknown as exc:
            # ⚠️ **絕不重試。** FillUnknown 的意思是「單送出去了，可能已經成交」。
            #    再送一筆同樣大小的反向單，若第一筆其實成交了，就是平完之後繼續賣——
            #    開出一個方向相反、沒人管的新倉。而出場倉別是「自動」，券商不會擋。
            #    這條規則寫在 broker/__init__.py 的 FillUnknown docstring 裡。
            reason = f"出場委託送出了但收不到回報：{exc}"
            logger.error("%s", reason)
            # 不知道平掉沒有 → 記成「不確定」。維持 CONFIRMED 的話，記錄上會是
            # 「有 N 口、還沒出場」，那是一個看起來很確定的錯誤。
            write_position(
                replace(record, lots=None, status=UNCERTAIN,
                        # **標明不確定的是出場那一筆。** 少了它，重跑時會被當成
                        # 進場的不確定去查詢與復原，然後再送一次反向委託。
                        uncertain_stage=UNCERTAIN_EXIT,
                        order_seq=exc.order_seq or record.order_seq),
                path=state_path,
            )
            notified = _send(build_exit_unknown_payload(record, str(exc), today))
            return ExitOutcome(exited=False, remaining=None, notified=notified,
                               exit_code=1, failure=reason)
        except OrderFailed as exc:
            # 確定沒送出去 → 重試是安全的，而且應該做。
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
    write_position(replace(record, exited=True, close_reason=BY_EXIT), path=state_path)
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
                            state_path=STATE_PATH,
                            observations_path=OBSERVATIONS_PATH)
        logger.info("進場結束：訊號=%s exit_code=%s", outcome.signal, outcome.exit_code)
    else:
        outcome = run_exit(config, today=today, broker=broker, notify=notify,
                           state_path=STATE_PATH)
        logger.info("出場結束：已出場=%s 殘留=%s exit_code=%s",
                    outcome.exited, outcome.remaining, outcome.exit_code)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
