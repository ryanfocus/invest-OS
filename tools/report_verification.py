"""把驗證排程的結果讀出來，發到 Discord。

用法：
    python tools/report_verification.py morning    08:44/08:50 那兩班跑起來了嗎
    python tools/report_verification.py closing    收盤後：抓到什麼

存在的理由：排程是無人值守跑的，log 寫在那台電腦上。人在上班，看不到。
而「listener 有沒有跑起來」直接決定「現在下單有沒有意義」——
沒跑起來還特地去下一筆單，是白花手續費。

本程式**只讀 log 與發訊息**，不碰群益 API、不下任何單。
"""

from __future__ import annotations

import datetime
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

LOG_DIR = os.path.join(_ROOT, "logs")


def _read(name: str, stamp: str) -> str | None:
    path = os.path.join(LOG_DIR, f"{name}-{stamp}.log")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _open_price_lines(text: str) -> list:
    """抓開盤價比對那張表的內容列。"""
    lines, grabbing = [], False
    for line in text.splitlines():
        if line.startswith("-" * 10):
            grabbing = True
            continue
        if grabbing:
            if not line.strip() or line.startswith("═"):
                break
            lines.append(line.rstrip())
    return lines


def morning_report(stamp: str) -> list:
    """兩班有沒有正常起來——決定「現在去下單值不值得」。"""
    out = ["**08:50 驗證排程狀態**", ""]

    compare = _read("compare_open", stamp)
    if compare is None:
        out.append("❌ **開盤價比對：沒有 log** —— 排程沒跑起來")
    elif "═══ 開盤價" in compare:
        out.append("✅ 開盤價比對：完成")
        for line in _open_price_lines(compare):
            out.append(f"`{line}`")
    else:
        out.append("⚠️ 開盤價比對：log 有但內容不完整")
        out.append(f"```{compare[-300:]}```")

    out.append("")
    listen = _read("verify_order_path", stamp)
    if listen is None:
        out.append("❌ **回報監聽：沒有 log** —— 排程沒跑起來")
        out.append("→ **現在下單不會被抓到**，不必特地去下")
    elif "開始監聽" in listen or "監聽 " in listen:
        out.append("✅ **回報監聽：正在聽**")
        out.append("→ 現在到 13:45 之間下任何一筆單都會被記錄")
    else:
        out.append("⚠️ 回報監聽：起來了但沒到監聽那一步")
        out.append(f"```{listen[-400:]}```")
    return out


def closing_report(stamp: str) -> list:
    """收盤後：到底抓到了什麼。"""
    out = ["**收盤後：驗證結果**", ""]

    listen = _read("verify_order_path", stamp)
    if listen is None:
        out.append("❌ 回報監聽沒有 log")
        return out

    if "第 1 則" in listen:
        count = listen.count("── 第 ")
        out.append(f"✅ **收到 {count} 則回報**")
        if "認不出來" in listen:
            out.append("⚠️ 但其中有「**認不出來**」——欄位位置推錯了")
            out.append("→ ticket 09 的前提要重寫")
        else:
            out.append("✅ 全部都解讀成功")
        out.append("→ 回家後把 log 貼給 Claude 對照原始字串")
    else:
        out.append("⚠️ **整段沒收到任何回報**")
        out.append("→ 若你今天有下單，代表回報這條線有問題（那本身是重要發現）")
        out.append("→ 若沒下單，那就是正常的，改天再試")

    compare = _read("compare_open", stamp)
    if compare and "═══ 開盤價" in compare:
        out.append("")
        out.append("**開盤價比對**")
        for line in _open_price_lines(compare):
            out.append(f"`{line}`")
    return out


def main() -> int:
    stage = sys.argv[1] if len(sys.argv) > 1 else "morning"
    stamp = (sys.argv[2] if len(sys.argv) > 2
             else datetime.date.today().strftime("%Y%m%d"))

    lines = morning_report(stamp) if stage == "morning" else closing_report(stamp)
    body = "\n".join(lines)
    print(body)

    import settings
    from notifiers.discord import send

    webhook = settings.read_env("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("\n（找不到 DISCORD_WEBHOOK_URL，只印出不發送）")
        return 1
    ok = send({"content": body}, webhook)
    print(f"\nDiscord 發送{'成功' if ok else '失敗'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
