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

# **每一則要人手動平倉的訊息都附這兩句**（2026-11-15）。
#
# 帳戶 APP 看到的是淨部位：使用者自己長期持有的微台、OS、PTS 都在同一個合約裡，
# 看不出哪幾口是 OS 的。所以訊息一律寫明「買進／賣出 幾口」，而且要講清楚只平這些——
# 照 APP 上的部位全平，會連使用者自己的部位和 PTS 的口數一起平掉。
# 使用者的原話：「只要明確的寫要平幾口，手動處理就不會報錯」。
_ONLY_THESE_LOTS = ("只平這個口數，不要照 APP 上的部位全平："
                    "帳上可能還有你自己的部位或其他程式的口數。")
_DEADLINE = ("請在 13:45 日盤收盤前送出；來不及就在夜盤（15:00 開盤）送出。"
             "之後不會有程式再處理這筆。")


def _contract(record) -> str:
    return f"{record.order_code}（{record.contract_month}）"


def _look_up(seq: str, which: str) -> str:
    """叫人去券商 APP 找那一筆委託。**沒有序號時講怎麼找**，不印空白的序號。"""
    if seq:
        return f"到券商 APP 用委託序號 {seq} 查這筆"
    return f"這筆沒有拿到委託序號：到券商 APP 的委託查詢，找{which}"


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

    ⚠️ **只在「那天沒收到任何告警」時才叫人送單**（2026-11-15）。那天 13:40 若發過
       告警（出場失敗、只成交一部分、不確定……），使用者可能已經照著平了——
       記錄沒有因此改變，再叫他平一次就是反向新倉。
    ⚠️ **合約已經結算就不叫人送單**：交易所已經平掉了，送單只會在下一個月份開新倉。
    """
    day = str(record.trading_day)
    day_text = f"{day[:4]}/{day[4:6]}/{day[6:]}"
    side = _SIDE_TEXT.get(record.side, record.side)
    close = _SIDE_TEXT.get(record.exit_side, record.exit_side)
    seq_text = f"委託序號 {record.order_seq}" if record.order_seq else "沒有拿到委託序號"
    if record.uncertain_entry:
        what = (f"{day_text} 的 {side} {record.requested_lots} 口 {_contract(record)}"
                f"（{seq_text}，成交幾口當時就沒問到）")
        action = (_look_up(record.order_seq,
                           f"{day_text} 08:50 左右{side} {record.requested_lots} 口 "
                           f"{record.order_code} 的那一筆")
                  + f"成交了幾口，請自行{close}那個口數（0～{record.requested_lots} 口）。")
    elif record.uncertain_exit:
        what = f"{day_text} 的 {_contract(record)}：出場委託（{seq_text}）不知道成交幾口"
        action = (_look_up(record.order_seq,
                           f"{day_text} 13:40 左右{close} {record.order_code} 的那一筆")
                  + f"：委託口數減掉成交口數，就是還要自行{close}的口數。")
    else:
        what = f"{day_text} 的 {side} {record.lots} 口 {_contract(record)}"
        action = f"請自行送出：**{close} {record.lots} 口 {record.order_code}**（{record.contract_month}）。"

    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "⚠️ **前一個交易日的部位沒有收掉**",
        "",
        what,
        "",
    ]
    if record.last_trading_day < int(trading_date.strftime("%Y%m%d")):
        lines.append(f"這個合約 {record.last_trading_day} 已經結算，部位由交易所以結算價平掉了，"
                     "**不用送任何委託**。")
    else:
        lines += [
            "那天 13:40 OS 如果發過告警（出場失敗、只成交一部分、不確定……），照那則處理；"
            "已經照著平了，就什麼都不用做。",
            f"那天沒有收到 OS 的任何告警（例如電腦沒開），就是還在帳上。{action}",
            _ONLY_THESE_LOTS,
        ]
    lines += [
        "",
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

    與「下單失敗」刻意用不同的字眼與圖示：那一則確定沒有部位。

    ⚠️ **這一則叫人「現在不要平」**（2026-11-15）。原本寫「今天下午不會自動平倉——
       需要你自己處理」，但 ticket 09 之後 13:40 會先用委託序號查，查到就照常平倉。
       照著先手動平的話，13:40 會再平一次，開出一個反向新倉。
       查不到時 13:40 會發 `build_exit_blocked_payload`，那一則才寫明要平幾口——
       **要人動手的指示只有一個出處**，兩則都叫人平，照做兩次就是反向新倉。
       等到 13:40 不吃虧：OS 本來就持有到 13:40。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "❓ 已送出委託，但**收不到成交回報**",
        f"商品：{_contract(record)}",
        f"委託：{_SIDE_TEXT.get(record.side, record.side)} {record.requested_lots} 口，"
        f"委託序號 {record.order_seq or '（未取得）'}",
        f"原因：{reason}",
        "",
        "**現在不用處理，也不要自己平。**",
    ]
    if record.last_trading_day == record.trading_day:
        # 結算日出場那班不送單也不查（`main.run_exit`），部位由交易所結算。
        lines.append("今天是結算日：OS 的部位會由交易所以最後結算價平掉，不必送任何委託。"
                     "出場那班會再發一則，到時用委託序號對帳就好。")
    elif record.order_seq:
        lines += [
            "13:40 出場時會先用委託序號向券商查這筆成交了幾口：查到就照常平倉"
            "（和平常一樣不發訊息）；查不到會再發一則「不確定持有幾口」，"
            "寫明要平幾口，到時照那則做。",
            "先自己平的話，13:40 查到成交會再平一次，開出一個反向新倉。",
        ]
    else:
        lines.append(
            "這筆沒有拿到委託序號，13:40 查不到成交了幾口，會再發一則「不確定持有幾口」，"
            "寫明怎麼找這筆委託、要平幾口，到時照那則做。")
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
        _ONLY_THESE_LOTS,
        _DEADLINE,
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
        _ONLY_THESE_LOTS,
        _DEADLINE,
    ]
    return {"content": "\n".join(lines)}


def build_exit_blocked_payload(record, trading_date: date) -> dict:
    """早上那筆進場單是「不確定」，13:40 也沒能查到成交幾口，所以**刻意不出場**。

    這不是失敗，是拒絕猜測——不知道持有幾口就下單，可能開出反向新倉，
    比什麼都不做更糟。訊息要講清楚「系統為什麼不動」，否則看起來像壞掉。

    但「不知道」的只有程式：委託序號拿到券商 APP 一查就知道成交了幾口。
    所以訊息寫明要送的單——方向、合約、口數的範圍、去哪裡查確切的數字——
    而不是叫人「確認帳戶實際部位」：APP 上的淨部位混著使用者自己與 PTS 的口數
    （2026-11-15）。
    """
    side = _SIDE_TEXT.get(record.side, record.side)
    close = _SIDE_TEXT.get(record.exit_side, record.exit_side)
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "❓ **不確定持有幾口，因此沒有自動平倉**",
        "",
        f"早上送出過 {_contract(record)}{side} {record.requested_lots} 口的委託，"
        "但沒有收到成交回報，13:40 也沒能向券商查到成交了幾口。",
        f"委託序號：{record.order_seq or '（未取得）'}",
        "",
        f"**請自行{close} {_contract(record)}，口數＝這筆委託實際成交的口數"
        f"（0～{record.requested_lots} 口）。**",
        _look_up(record.order_seq,
                 f"今天 08:50 左右{side} {record.requested_lots} 口 {record.order_code} 的那一筆")
        + f"成交了幾口，就{close}幾口；一口都沒成交就什麼都不用做。",
        _ONLY_THESE_LOTS,
        _DEADLINE,
    ]
    return {"content": "\n".join(lines)}


def build_exit_switched_off_payload(record, trading_date: date) -> dict:
    """自動下單在進場之後被關掉，所以 13:40 沒有平倉。**部位確定還在。**

    原本這裡借用 `build_exit_blocked_payload`，講的是「早上的委託沒有收到成交回報」——
    那不是事實：這筆早上已經確認成交，口數是知道的。叫人去「確認帳戶」只會讓他
    在一個混著別人口數的淨部位裡找答案（2026-11-15 拆開）。
    """
    close = _SIDE_TEXT.get(record.exit_side, record.exit_side)
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "🚨 **自動下單已關閉，13:40 沒有平倉，部位還在**",
        "",
        f"請自行送出：**{close} {record.lots} 口 {record.order_code}**（{record.contract_month}）",
        f"如果你已經自己平掉這 {record.lots} 口，就什麼都不用做。",
        _ONLY_THESE_LOTS,
        _DEADLINE,
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

def build_exit_unknown_payload(record, reason: str, trading_date: date, *,
                               exit_seq: str, known_filled: int = 0) -> dict:
    """出場單送出去了，但**收不到回報**——不知道平掉沒有。

    與「出場失敗」刻意分開：那一則的意思是「確定沒平，請照著送這張單」，
    這一則是「**可能平了也可能沒平，查過再補**」。
    照著失敗那則的指示盲送，若原本已經平掉就是開出反向新倉。

    要補的口數＝出場單沒成交的口數，拿出場單的委託序號到券商 APP 一查就知道
    （委託口數減成交口數）。**`exit_seq` 是出場單的序號，刻意由呼叫端給**：
    第一次發的時候 `record.order_seq` 還是早上**進場單**的序號，拿那個去查，
    會查到早上全部成交，算出「0 口要補」，部位就這樣過夜。
    """
    close = _SIDE_TEXT.get(record.exit_side, record.exit_side)
    left = ("**還沒平的口數＝這筆出場委託沒有成交的口數"
            + (f"（0～{record.lots - known_filled} 口）" if record.lots is not None else "")
            + "。**")
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
           else "口數見第一次那則通知（狀態檔不留不確定的口數）")
        + f"，委託序號 {exit_seq or '（未取得）'}",
        f"原因：{reason}",
        "",
        left,
        _look_up(exit_seq, f"今天 13:40 左右{close} {record.order_code} 的那一筆")
        + f"：委託口數減掉成交口數，就是還要自行{close}的口數；全部成交就什麼都不用做。",
        "不要照原本的口數再送一次：若原本已經平掉，就是開出反向新倉。",
        _ONLY_THESE_LOTS,
        _DEADLINE,
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

    口數從 log 找：每次寫狀態檔都會記一行「OS 部位：」，直接寫著要送的單
    （`state.position_line`）。不叫人「確認帳戶」：APP 上的淨部位混著使用者自己
    與 PTS 的口數，看不出哪幾口是 OS 的（2026-11-15）。
    先看出場、再看進場：同一天出場一定在進場之後，最後寫的那一行才是現況。
    """
    day = trading_date.strftime("%Y%m%d")
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "🚨 **出場時讀不到部位記錄**",
        f"原因：{reason}",
        "",
        "系統無法判斷今天有沒有進場，因此沒有送出任何委託。",
        "",
        f"**OS 今天的部位記在 log 裡：**先看 `logs\\exit-{day}.log`，"
        f"沒有再看 `logs\\entry-{day}.log`，找最後一行「OS 部位：」，照那一行寫的做：",
        "・寫「還沒平——要平就買進／賣出 N 口」：自行送出那張單。",
        "・寫「不確定」：照那一行，到券商 APP 用委託序號查要平幾口。",
        "・寫「已了結」或「沒有」，或兩份 log 都找不到這一行：今天沒有 OS 的部位，什麼都不用做。",
        _ONLY_THESE_LOTS,
        _DEADLINE,
    ]
    return {"content": "\n".join(lines)}


_REDACTED = "<webhook 已遮蔽>"


def redact_webhook(text: str, webhook_url: str) -> str:
    """把 webhook 從訊息裡拿掉，但**留下主機名稱與失敗原因**。

    `requests` 的例外字串含完整的請求 URL，而 Discord 的 webhook 最後一段
    就是 token：

        HTTPSConnectionPool(host='discord.com', port=443):
        Max retries exceeded with url: /api/webhooks/<id>/<token> (Caused by ...)

    任何 DNS 失敗、逾時、斷線都會走到那裡，而那行文字會被寫進當日的日誌——
    正是出事時使用者會傳出來的檔案。`deploy/build.ps1` 把
    `DISCORD_WEBHOOK_URL` 列為四大機密之一、掃遍整個交付包確保它不外流，
    程式自己卻會把它寫進日誌（2026-09-02 查驗發現）。

    ⚠️ **只遮網址，不遮原因。** 「為什麼發不出去」是排查唯一的線索，
       整段吃掉的話這則日誌就沒有價值了。
    """
    if not webhook_url:
        return text
    out = text.replace(webhook_url, _REDACTED)
    # 例外訊息裡通常只有路徑那一段（不含 scheme 與主機），所以也要換掉它
    path = webhook_url.split("://", 1)[-1]
    if "/" in path:
        path = path[path.index("/"):]
        if len(path) > 1:
            out = out.replace(path, _REDACTED)
    return out


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
        # ⚠️ 一定要遮。例外字串含完整的請求 URL，而 webhook 的最後一段是 token——
        #    見 `redact_webhook`。
        logger.error("Discord 發送例外：%s", redact_webhook(str(exc), webhook_url))
        return False

    if resp.ok:
        logger.info("Discord 已發送")
        return True

    logger.error("Discord 發送失敗：HTTP %s", resp.status_code)
    return False
