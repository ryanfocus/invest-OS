"""收尾：讓 `logs/` 不會無限長大。

## 為什麼這件事比「省空間」重要

`logs/` 裡有三種來源的東西，而我們只控制得了其中一種：

| 來源 | 例子 | 我們控制得了嗎 |
|------|------|--------------|
| 我們自己寫的執行紀錄 | `entry-20260821.log` | ✅ |
| 券商回覆的原始存檔 | `20260821-134301-GetFulfillReport.txt` | ✅ 但**含未遮罩的期貨帳號** |
| **群益元件自己寫的** | `Center.log`、`20260806_Reply.log` | ❌ **含使用者的身分證字號** |

最後那一種是 2026-08-21 才發現的：群益的 COM 元件會自己往工作目錄寫日誌，
檔名與內容我們都插不上手。它會一直長，而且帶著個資。

所以清理的目的是**限制敏感資料留在磁碟上的時間**，省空間只是附帶。

## 兩條不可動搖的原則

**一、只刪檔案，不刪目錄。** 群益的元件會在工作目錄下建子資料夾
（`CapitalLog`）。遞迴刪目錄的風險與收益完全不成比例。

**二、絕不拋例外。** 這件事跑在當日訊號、下單、對帳全部完成之後。
那時候拋例外只會讓一天原本成功的執行以 traceback 收場，而清理本身一點都不重要。
"""

from __future__ import annotations

import logging
import os
import time
import paths

logger = logging.getLogger(__name__)

_ROOT = paths.app_root()
LOGS_PATH = os.path.join(_ROOT, "logs")

# 保留天數。2026-08-21 與使用者確認。
#
# 三十天的理由：出事回頭查通常不會查超過一個月，而超過一個月的個資
# 留著沒有任何用途。邊界刻意**留**不刪——「保留 30 天」的自然理解是
# 「30 天內的都在」，砍掉邊界會讓最舊、最難重建的那一天撲空。
LOG_RETENTION_DAYS = 30

_SECONDS_PER_DAY = 86400


def purge_old_logs(
    directory: str = LOGS_PATH,
    *,
    retention_days: int = LOG_RETENTION_DAYS,
    now: float | None = None,
) -> int:
    """刪掉 `directory` 底下超過保留期限的**檔案**，回傳刪了幾個。

    **這個函式永遠不拋例外。** 任何問題都只記進 log——它是收尾動作，
    不值得讓一天原本成功的執行以錯誤收場。

    ⚠️ **只碰傳進來的那一個目錄，不遞迴。** `state/` 與 `logs/` 是兄弟目錄，
    而 `state/observations.jsonl` 是累積型的稽核歷史——刪掉就重建不回來。
    """
    cutoff = (now if now is not None else time.time()) - retention_days * _SECONDS_PER_DAY
    removed = 0
    try:
        with os.scandir(directory) as entries:
            targets = [e.path for e in entries
                       # 只看檔案。群益的元件會建子資料夾，那不是我們的東西。
                       if e.is_file(follow_symlinks=False)
                       and e.stat(follow_symlinks=False).st_mtime < cutoff]
    except FileNotFoundError:
        # 第一次執行時 logs/ 可能還不存在。那是正常的，不是故障。
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理日誌時列不出 %s（%s）：%s", directory, type(exc).__name__, exc)
        return 0

    for path in targets:
        try:
            os.remove(path)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            # Windows 上檔案被別的行程開著就刪不掉——群益的元件正是會這樣。
            # 一個刪不掉不該讓其餘的都留下來，所以繼續。
            logger.info("清理日誌：%s 刪不掉（%s），略過",
                        os.path.basename(path), type(exc).__name__)

    if removed:
        logger.info("清理日誌：刪除 %d 個超過 %d 天的檔案", removed, retention_days)
    return removed
