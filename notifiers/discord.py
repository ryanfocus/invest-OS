"""Discord 發報。

訊息組裝（`build_signal_payload`）是純函式，方便直接斷言內容；
實際送出（`send`）獨立成一個函式，測試在此處置換。
"""

from __future__ import annotations

import logging
from datetime import date

import requests

from broker import BUY, SELL, format_yyyymmdd
from strategy import LONG, NO_TRADE, SHORT, SignalResult

logger = logging.getLogger(__name__)

# 訊號方向對外顯示用的中文。程式內用英文常數，訊息用中文——
# 使用者在手機上看到的是「做多」，不是 LONG。
_SIGNAL_TEXT = {
    LONG: "做多",
    SHORT: "做空",
    NO_TRADE: "不動作",
}

# 委託買賣別。與訊號方向是不同層次的東西——訊號說「該做多」，委託說「買進」——
# 所以分成兩張表，不共用。
_SIDE_TEXT = {BUY: "買進", SELL: "賣出"}


def build_signal_payload(result: SignalResult, trading_date: date) -> dict:
    """把訊號結果組成 Discord webhook 的 payload。純函式，無 I/O。"""
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        f"大台開盤：{result.tx:.0f}",
        f"小台開盤：{result.mtx:.0f}",
        f"微台開盤：{result.tmf:.0f}",
        f"訊號：{_SIGNAL_TEXT[result.signal]}",
    ]
    return {"content": "\n".join(lines)}


def build_no_signal_payload(reason: str, trading_date: date) -> dict:
    """今天**無法判斷**時的訊息。純函式，無 I/O。

    刻意不使用「不動作」三個字——那是判斷出來的結果，這裡是根本沒判斷成功。
    兩者長得像的話，故障就會被當成正常。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "⚠️ 今日無訊號",
        f"原因：{reason}",
    ]
    return {"content": "\n".join(lines)}


def build_order_failed_payload(reason: str, trading_date: date) -> dict:
    """委託送不出去時的告警。純函式，無 I/O。

    與「今日無訊號」是不同的事：訊號有算出來也發出去了，是**下單那一步**失敗。

    ⚠️ **不要叫人去看帳戶。** 這則訊息對應的是 `OrderFailed`，而它的定義是
    「委託沒有送出去，**確定沒有部位產生**」——分不清有沒有部位時用的是
    `FillUnknown`，那是另一則訊息。

    這裡原本寫「請確認帳戶部位」，與 `OrderFailed` 的定義互相矛盾
    （2026-08-25 寫訊息對照表時才發現）。每次都叫人白跑一趟券商 APP 的代價
    是真的：跑幾次都沒事之後，真的要看的那一則就不會有人看了。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "🚨 下單失敗",
        f"原因：{reason}",
        "訊號已發出，但**委託沒有送出去，帳上不會有部位**——今天不用做任何事。",
        "下午那班也不會有東西要平。",
    ]
    return {"content": "\n".join(lines)}


_POSITION_TEXT = {"N": "新倉", "O": "平倉"}


def build_exit_did_not_offset_payload(record, exit_type: str, trading_date: date) -> dict:
    """**下午那筆沒有平到倉。** 純函式，無 I/O。

    券商每一筆回報都會說它對淨部位做了什麼（新倉／平倉）。早上開了就下午平、
    早上平掉使用者的部位（跨越零）就下午開一口還回去——**必然相反**。
    兩邊一樣代表下午那筆碰不到早上那口，於是開出一個沒人管的新部位。

    最常見的成因：早上那口在盤中被手動平掉了。

    ⚠️ 說「很可能」不說「一定」：程式看不到帳戶，它只知道兩個回報欄位不對勁。
    台指期是淨額計算，帳上實際剩什麼還牽涉到使用者自己的部位。
    把話說死，錯一次使用者就不會再信這則訊息了。
    """
    side = _SIDE_TEXT.get(record.side, record.side)
    back = _SIDE_TEXT.get(record.exit_side, record.exit_side)
    lots = record.lots if record.lots is not None else record.requested_lots
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "🚨 **下午那筆沒有平到倉**",
        "",
        f"早上 {side} {lots} 口 → 券商記「{_POSITION_TEXT.get(record.entry_position_type, record.entry_position_type)}」",
        f"下午 {back} {lots} 口 → 券商記「{_POSITION_TEXT.get(exit_type, exit_type)}」",
        "",
        f"兩筆一樣，代表下午沒平到早上那口。帳上很可能多一口沒人管的 {record.order_code}，"
        "而今天已經沒有排程會處理它了——請自行確認並平掉。",
    ]
    return {"content": "\n".join(lines)}


def build_stale_position_payload(record, trading_date: date) -> dict:
    """**前一個交易日的部位沒有收掉。** 純函式，無 I/O。

    只在狀態檔裡有「過去日期 ＋ 未出場」的記錄時發。那個條件精準地
    只認**程式自己的部位**：使用者手動買回來的那些從來沒進過狀態檔，
    所以不會誤報。假警報的代價是真的——使用者習慣忽略某一類訊息之後，
    那類訊息就再也擋不住事情了。

    ⚠️ **這則不叫人停手。** 沒收掉的部位不會讓今天的交易變錯：SPEC 的
    部位隔離設計本來就假設帳上有別人的部位，程式進出等量、算術自然回復。
    它只會讓那一口沒人知道——所以要補的是知情，不是停手。

    為什麼需要它：2026-08-24 進場倉別改成「自動」之後，反向委託會把
    帳上的舊部位**安靜地**吃掉。在那之前「新倉」會退單（代碼 980），
    大聲失敗——但那是券商規則的副作用，不是我們的設計。
    """
    lots = record.lots if record.lots is not None else record.requested_lots
    known = record.lots is not None
    day = str(record.trading_day)
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "⚠️ **前一個交易日的部位沒有收掉**",
        "",
        f"{day[:4]}/{day[4:6]}/{day[6:]} 的 "
        f"{_SIDE_TEXT.get(record.side, record.side)} {lots} 口 {record.order_code}"
        + ("" if known else "（委託量，實際成交幾口當時就沒問到）"),
        "",
        "**這一口可能還在你帳上**，請確認並自行處理。",
        "今天照常交易——程式進出等量，不會動到它。",
        "",
        "今天若有成交，這筆紀錄會被覆蓋，之後就不會再提醒。",
    ]
    return {"content": "\n".join(lines)}


def build_entry_too_late_payload(signal: str, ran_at, cutoff, trading_date: date) -> dict:
    """**這班太晚了，所以沒有下單。** 純函式，無 I/O。

    與「下單失敗」刻意分成兩則：那一則的意思是「單可能出了問題，去看帳戶」，
    這一則是「**確定什麼都沒送**，帳上乾淨」。講混了的代價是使用者白跑一趟
    券商 APP——而白跑幾次之後，真的出事那一則他就不會認真看了。

    訊息裡放實際執行時刻與界線兩個數字，因為使用者要做的判斷是
    「排程慢了多久、要不要調界線」，那兩個數字缺一個都判斷不了。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "⏰ **這班太晚了，沒有下單**",
        "",
        f"訊號：{_SIGNAL_TEXT.get(signal, signal)}",
        f"實際執行 {ran_at.strftime('%H:%M')}，已過 {cutoff.strftime('%H:%M')} 的界線。",
        "",
        "**帳上沒有部位**——什麼都沒有送出去，下午也沒有東西要平。",
        "排程大概是因為關機或重開機而補跑的，確認一下電腦有沒有意外重啟。",
    ]
    return {"content": "\n".join(lines)}


def build_fill_unknown_payload(record, reason: str, trading_date: date) -> dict:
    """**委託送出去了，但不知道成交幾口。** 純函式，無 I/O。

    這是所有訊息裡最需要使用者立刻行動的一則，所以講的是「去做什麼」，
    不是「發生了什麼錯誤」。與「下單失敗」刻意用不同的字眼與圖示——
    那一則的正確反應是「不用管」，這一則是「馬上去看帳戶」。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "❓ 已送出委託，但**收不到成交回報**",
        f"商品：{record.order_code}（{record.contract_month}）",
        f"方向：{_SIDE_TEXT.get(record.side, record.side)}",
        f"原因：{reason}",
        "",
        "**請人工確認帳戶實際部位。** 系統不知道成交了幾口，",
        "因此今天下午不會自動平倉——需要你自己處理。",
    ]
    return {"content": "\n".join(lines)}


def build_exit_failed_payload(record, reason: str, trading_date: date) -> dict:
    """出場失敗 —— **部位還在帳上，而且快要進夜盤了。**

    這是所有訊息裡最急的一則。所以它不講「發生什麼錯誤」，而是直接寫出
    **使用者要送出什麼單**：商品、方向、口數。看到訊息的人可能在開會、在路上，
    要能照著念給營業員聽或直接在 APP 上點完。
    """
    exit_side = record.exit_side
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "🚨 **出場失敗，部位還在**",
        "",
        f"請立刻手動送出：**{_SIDE_TEXT.get(exit_side, exit_side)} {record.lots} 口 "
        f"{record.order_code}**（{record.contract_month}）",
        "",
        f"原因：{reason}",
    ]
    return {"content": "\n".join(lines)}


def build_partial_exit_payload(record, remaining: int, trading_date: date) -> dict:
    """只平掉一部分 —— 剩下的口數會過夜。

    與「完全失敗」分開，是因為使用者要送的單不一樣：這裡只剩 `remaining` 口，
    照原本的口數再送一次會多平。
    """
    exit_side = record.exit_side
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "⚠️ **出場只成交一部分**",
        "",
        f"還剩 **{remaining} 口**未平。請手動送出："
        f"**{_SIDE_TEXT.get(exit_side, exit_side)} {remaining} 口 "
        f"{record.order_code}**（{record.contract_month}）",
    ]
    return {"content": "\n".join(lines)}


def build_exit_blocked_payload(record, trading_date: date) -> dict:
    """狀態是「不確定」，所以**刻意不出場**。

    這不是失敗，是拒絕猜測——不知道持有幾口就下單，可能開出反向新倉，
    比什麼都不做更糟。訊息要講清楚「系統為什麼不動」，否則看起來像壞掉。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "❓ **不確定持有幾口，因此沒有自動平倉**",
        "",
        f"早上送出過 {record.order_code}（{record.contract_month}）"
        f"{_SIDE_TEXT.get(record.side, record.side)} {record.requested_lots} 口的委託，"
        "但沒有收到成交回報。",
        f"委託序號：{record.order_seq or '（未取得）'}",
        "",
        "**請人工確認帳戶實際部位並自行平倉。**",
        "系統不知道成交了幾口，猜一個數字下單可能開出反向新倉。",
    ]
    return {"content": "\n".join(lines)}


def build_settlement_uncertain_payload(record, trading_date: date) -> dict:
    """結算日，而且**不知道早上成交了幾口**。

    ⚠️ **結算日本身不發訊息**（2026-08-21 定案：出場那班只有「有事不對勁」
    才推播）。合約到期、部位一定會被交易所平掉、不需要人做任何事——
    那不算不對勁。當日損益與回測的落差記在 log 與狀態檔的 `close_reason`。

    但「不知道持有幾口」是真的不對勁：部位會照樣被結算沒錯，可是那筆帳
    使用者永遠對不起來——結算不會補上早上漏掉的資訊。

    ⚠️ 訊息**不可以叫人去平倉**。合約已經停止交易，那是做不到的事；
    照著做只會在別的月份開新倉。要講的是「去對帳」。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "❓ **結算日，但不知道早上成交了幾口**",
        "",
        f"{record.order_code}（{record.contract_month}）"
        f"{_SIDE_TEXT.get(record.side, record.side)}——"
        f"委託 {record.requested_lots} 口，序號 {record.order_seq or '未取得'}。",
        "早上沒收到成交回報，**系統不知道實際成交了幾口**。",
        "",
        "部位會由交易所以最後結算價平掉，**不需要你送任何委託**"
        "（合約已停止交易，送也送不出去）。",
        "但請自行對帳確認那天實際持有幾口——結算不會補上這個資訊。",
    ]
    return {"content": "\n".join(lines)}

def build_exit_unknown_payload(record, reason: str, trading_date: date) -> dict:
    """出場單送出去了，但**收不到回報**——不知道平掉沒有。

    與「出場失敗」刻意分開：那一則的意思是「確定沒平，請照著送這張單」，
    這一則是「**可能平了也可能沒平，先去看帳戶再決定**」。
    照著失敗那則的指示盲送，若原本已經平掉就是開出反向新倉。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "❓ **出場委託已送出，但收不到回報**",
        "",
        f"商品：{record.order_code}（{record.contract_month}）",
        # ⚠️ **這裡是 `lots` 不是 `requested_lots`。** 出場單送的是早上
        #    **實際成交**的口數（見 main.py 的 `lots=record.lots`）。
        #    早上委託 3 口只成交 2 口時，下午送的是 2 口——而這則訊息的用途
        #    正是叫人「先確認帳戶實際部位再決定要不要補單」，數字錯在這裡，
        #    人會拿著錯的數字去對帳，然後補一筆錯的單。
        #
        #    對照 `build_exit_blocked_payload`：那一則講的是**早上那筆**委託
        #    （「早上送出過 N 口的委託」），所以它用 `requested_lots` 是對的。
        #    兩則描述的是不同的委託，不可以互相參照。
        f"送出的是：{_SIDE_TEXT.get(record.exit_side, record.exit_side)}，"
        # ⚠️ **重跑時這裡沒有數字。** 記成「不確定」的那一刻，狀態檔就依
        #    `PositionRecord` 的規定把口數抹掉了（`UNCERTAIN ⇒ lots is None`，
        #    理由是「填數字會讓它看起來像已知的結果」）。所以第一次發這則訊息
        #    時有數字（那時 record 還是 CONFIRMED），重跑讀回來就沒有了。
        #    印出 `None` 比不印更糟——這則訊息的用途正是叫人拿數字去對帳。
        + (f"{record.lots} 口" if record.lots is not None
           else "口數見第一次那則通知（狀態檔不留不確定的口數）"),
        f"原因：{reason}",
        "",
        "**請先確認帳戶實際部位再決定要不要補單。**",
        "系統不知道這筆平掉沒有——盲目再送一次，若原本已經平掉就是開出反向新倉。",
    ]
    return {"content": "\n".join(lines)}


def build_reconciliation_payload(trading_day: int, mismatches) -> dict:
    """隔日對帳發現前一交易日的開盤價與期交所對不上。

    **這一則是給人追查用的，所以兩邊的數字都要寫出來。**
    只說「不一致」的話，收到的人還是得自己去期交所查一次才知道差多少、
    差在哪個商品——那個摩擦足以讓這則訊息長期被忽略。

    三個商品同時不一致時特別要看：那正是**取到錯誤盤別**的樣子
    （單一商品差幾點比較像雜訊，三個一起偏掉不是）。
    """
    lines = [
        f"{format_yyyymmdd(trading_day)} OS 對帳",
        "⚠️ **開盤價與期交所對不上**",
        "",
    ]
    for m in mismatches:
        lines.append(
            f"{m.product}：我們記的 **{_price(m.ours)}**，"
            f"期交所 **{_price(m.official)}**（差 {_price(m.ours - m.official)}）"
        )
    if len(mismatches) == 3:
        lines += [
            "",
            "**三個商品同時對不上**——這比較像取到錯誤的盤別（夜盤而非 AM 盤），"
            "不像單純的數字誤差。當日訊號可能是錯的。",
        ]
    return {"content": "\n".join(lines)}


def _price(value: float) -> str:
    """指數點位。整數就印整數，有小數就把小數印出來。

    ⚠️ 初版寫死 `:.0f`（code-review 2026-08-18 抓到）：比對的門檻是 1e-6，
    所以小數位的差異**會**觸發告警，卻會印出兩個一模一樣的數字——
    收到的人完全無從追查。那正好是這則訊息最需要說清楚的情況
    （小數點跑出來多半代表期交所改了格式，不是市場的事）。

    ⚠️ 也**不可以用 `:g`**：那是 6 位有效數字，而指數已經 5 位，
    45812.25 會被印成 45812.2——訊息本身變成了一個新的誤差來源。
    """
    if float(value).is_integer():
        return f"{value:.0f}"
    # 補滿小數再把多餘的零去掉：`.` 會擋住 rstrip，所以整數部分不會被啃掉。
    return f"{value:.4f}".rstrip("0").rstrip(".")


def build_observation_conflict_payload(trading_day: int, detail: str) -> dict:
    """同一個交易日出現**兩組不同的開盤價**。

    重複執行本身很正常（人想再看一次），數字不同才是訊號——代表報價來源
    在同一天給了兩個答案，而這整個系統就是建立在那三個數字上的。

    ⚠️ 保留的是**第一筆**（它比較接近 08:45）。第二筆不寫，但也不能丟掉不講：
    「同一天兩個答案」正是隔日對帳想抓的那類問題，而對帳只看得到留下的那一筆。
    """
    lines = [
        f"{format_yyyymmdd(trading_day)} OS",
        "⚠️ **同一天出現兩組不同的開盤價**",
        "",
        detail,
        "",
        "已保留**先寫入的那一筆**（較接近 08:45），這次的沒有寫進去。",
        "請確認報價來源是否正常——這個系統的訊號完全建立在這三個數字上。",
    ]
    return {"content": "\n".join(lines)}


def build_observation_damaged_payload(path: str, damaged) -> dict:
    """觀測記錄有讀不懂的行。**這一則是告警，因為稽核歷史缺了一塊。**

    壞掉的行只影響它自己那一天，其餘照常運作——正因為如此，不講的話
    沒有任何跡象：對帳照跑、觀測照寫，只是歷史缺了幾天，
    而缺的那幾天看起來就跟「那天沒跑」一模一樣。

    ⚠️ 修好之前每天都會再發一次。那是刻意的——它是個未修復的真實缺陷，
    而這個檔案的價值正是它的完整性。
    """
    lines = [
        "OS 對帳",
        "⚠️ **觀測記錄有讀不懂的行**",
        "",
        f"檔案：`{path}`",
        "",
    ]
    lines += [f"・{d}" for d in damaged[:5]]
    if len(damaged) > 5:
        lines.append(f"・……另外還有 {len(damaged) - 5} 行")
    lines += [
        "",
        "那幾天的開盤價**無法對帳**，其餘日子不受影響。",
        "修好之前每天都會再提醒一次。",
    ]
    return {"content": "\n".join(lines)}


def build_exit_state_broken_payload(reason: str, trading_date: date) -> dict:
    """出場時狀態檔讀不懂。

    刻意不共用「今日無訊號」那則——那句話在 13:40 出現只會讓人以為今天沒事，
    而真相是「可能有部位，但我讀不到記錄」。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "🚨 **出場時讀不到部位記錄**",
        f"原因：{reason}",
        "",
        "**請人工確認帳戶是否有未平倉部位。**",
        "系統無法判斷今天有沒有進場，因此沒有送出任何委託。",
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
