"""Discord 發報。

訊息組裝（`build_signal_payload`）是純函式，方便直接斷言內容；
實際送出（`send`）獨立成一個函式，測試在此處置換。
"""

from __future__ import annotations

import logging
from datetime import date

import requests

from broker import BUY, SELL
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
    使用者看到這則的正確反應是「去看一下帳戶」，看到無訊號的正確反應是「今天沒事」。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "🚨 下單失敗",
        f"原因：{reason}",
        "訊號已發出，但委託沒有送出去，請確認帳戶部位。",
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


def build_settlement_payload(record, trading_date: date) -> dict:
    """結算日**刻意不送出場委託**——合約到期，部位由交易所現金交割。

    這一則是**告知，不是告警**。與「出場失敗」那一則刻意用不同的語氣與圖示：
    那一則的意思是「部位還在，請立刻手動送出這張單」，這一則是
    「什麼都不用做」。兩者長得像的話，使用者遲早會在 13:30 之後衝去下一張
    送不出去的單——合約那時已經停止交易。

    訊息一定要寫結算價的來源。最後結算價是**現貨指數**的平均，不是期貨價，
    所以當天的實際損益本來就會與回測對不起來；不先講，日後對帳時
    那個落差看起來會像程式算錯。
    """
    lines = [
        f"{trading_date.strftime('%Y/%m/%d')} OS",
        "📅 **結算日，部位交由交易所現金結算**",
        "",
        f"未送出出場委託。{record.order_code}（{record.contract_month}）"
        f"{_SIDE_TEXT.get(record.side, record.side)}的部位會以最後結算價交割。",
        "最後結算價＝當日 13:00–13:30 加權指數每筆成交價的簡單算術平均"
        "（**現貨指數，不是期貨價**），所以當日損益會與回測有落差。",
    ]
    if record.is_uncertain:
        # 部位照樣會被結算，但我們從早上就不知道成交幾口——結算不會補上這個資訊。
        lines += [
            "",
            "⚠️ 但早上沒收到成交回報，**系統不知道實際成交了幾口**"
            f"（委託 {record.requested_lots} 口，序號 {record.order_seq or '未取得'}）。",
            "部位會照樣被結算，請自行對帳確認。",
        ]
    else:
        lines += ["", f"實際持有 {record.lots} 口。不需要處理。"]
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
        f"送出的是：{_SIDE_TEXT.get(record.exit_side, record.exit_side)} "
        f"{record.requested_lots} 口",
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
    day = f"{trading_day // 10000:04d}/{trading_day // 100 % 100:02d}/{trading_day % 100:02d}"
    lines = [
        f"{day} OS 對帳",
        "⚠️ **開盤價與期交所對不上**",
        "",
    ]
    for m in mismatches:
        lines.append(f"{m.product}：我們記的 **{m.ours:.0f}**，期交所 **{m.official:.0f}**")
    if len(mismatches) == 3:
        lines += [
            "",
            "**三個商品同時對不上**——這比較像取到錯誤的盤別（夜盤而非 AM 盤），"
            "不像單純的數字誤差。當日訊號可能是錯的。",
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
