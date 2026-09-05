"""日誌清理 —— 讓 `logs/` 不會無限長大。

## 為什麼需要它

`logs/` 裡有三種來源的東西，而我們只控制得了其中一種：

  我們自己寫的      entry-YYYYMMDD.log / exit-YYYYMMDD.log
  券商回覆的原始存檔  含**未遮罩的期貨帳號**
  群益元件自己寫的   Center.log / Reply.log 等，**內含使用者的身分證字號**

最後那一種是 2026-08-21 才發現的：群益的 COM 元件會自己往工作目錄寫日誌，
檔名與內容我們都控制不了。它會一直長，而且帶著個資。

所以清理不只是「省空間」，是**限制敏感資料留在磁碟上的時間**。

## 為什麼用真實檔案測

與狀態檔、觀測記錄同一個理由：這裡驗的就是檔案系統的行為。
把它換成假的等於沒測到。
"""

import os
import time

import pytest

from housekeeping import LOG_RETENTION_DAYS, purge_old_logs

DAY = 86400


def _aged(directory, name: str, *, days_old: float, content: str = "x") -> str:
    """造一個「幾天前最後修改」的檔案。"""
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    stamp = time.time() - days_old * DAY
    os.utime(path, (stamp, stamp))
    return path


# --- 該刪的刪掉 ---


def test_a_file_older_than_the_retention_window_is_removed(tmp_path):
    old = _aged(tmp_path, "old.log", days_old=LOG_RETENTION_DAYS + 1)
    purge_old_logs(str(tmp_path))
    assert not os.path.exists(old)


def test_a_recent_file_is_kept(tmp_path):
    fresh = _aged(tmp_path, "today.log", days_old=0)
    purge_old_logs(str(tmp_path))
    assert os.path.exists(fresh)


def test_a_file_exactly_at_the_boundary_is_kept(tmp_path):
    """**邊界要留，不要刪。**

    「保留 30 天」的自然理解是「30 天內的都在」。邊界砍掉的話，
    出事回頭查剛好第 30 天的資料時會撲空——而那正是最舊、最難重建的一天。
    """
    edge = _aged(tmp_path, "edge.log", days_old=LOG_RETENTION_DAYS - 0.01)
    purge_old_logs(str(tmp_path))
    assert os.path.exists(edge)


def test_the_count_of_removed_files_is_returned(tmp_path):
    """呼叫端要記進 log。刪了幾個是唯一能事後確認「清理真的有在跑」的線索。"""
    for i in range(3):
        _aged(tmp_path, f"old{i}.log", days_old=LOG_RETENTION_DAYS + 5)
    _aged(tmp_path, "keep.log", days_old=1)
    assert purge_old_logs(str(tmp_path)) == 3


# --- 不該碰的不碰 ---


def test_subdirectories_are_left_alone(tmp_path):
    """**只刪檔案，不刪目錄。**

    群益的元件會在工作目錄下建子資料夾（例如 CapitalLog）。
    遞迴刪目錄的風險與收益完全不成比例——那個資料夾可能不是我們的。

    ⚠️ **突變測試誠實地告訴我這條殺不掉那個守衛**：拿掉 `is_file` 判斷，
    這條測試照樣過。因為 `os.remove` 本來就刪不掉目錄，會拋例外而被攔下來。
    所以那個守衛**不改變行為**，它防的是「每天為每個子資料夾印一行
    『刪不掉』」的噪音——而本專案的測試標準明訂不測 log 內容。
    留著這條測試是為了釘住「目錄要活著」這個結果本身。
    """
    sub = tmp_path / "CapitalLog"
    sub.mkdir()
    stamp = time.time() - (LOG_RETENTION_DAYS + 10) * DAY
    os.utime(str(sub), (stamp, stamp))
    purge_old_logs(str(tmp_path))
    assert sub.is_dir()


def test_a_missing_directory_is_not_an_error(tmp_path):
    """第一次執行時 `logs/` 可能還不存在。清理不該因此變成當天的失敗。"""
    assert purge_old_logs(str(tmp_path / "never-created")) == 0


def test_only_the_given_directory_is_touched(tmp_path):
    """**這條守的是最貴的誤刪。**

    `state/` 與 `logs/` 是兄弟目錄，而 `state/observations.jsonl` 是
    累積型的稽核歷史——刪掉就重建不回來了（`.gitignore` 那段有寫）。
    清理只准碰傳進來的那一個目錄。
    """
    logs = tmp_path / "logs"
    state = tmp_path / "state"
    logs.mkdir()
    state.mkdir()
    _aged(logs, "old.log", days_old=LOG_RETENTION_DAYS + 5)
    precious = _aged(state, "observations.jsonl", days_old=LOG_RETENTION_DAYS + 999)
    purge_old_logs(str(logs))
    assert os.path.exists(precious), "只准碰指定的目錄"


# --- 清不掉的時候，不可以拖垮當天 ---


def test_a_file_that_cannot_be_removed_does_not_stop_the_others(tmp_path):
    """Windows 上檔案被別的行程開著就刪不掉——群益的元件正是會這樣。

    一個刪不掉不該讓其餘的都留下來。
    """
    import housekeeping

    _aged(tmp_path, "a-locked.log", days_old=LOG_RETENTION_DAYS + 5)
    _aged(tmp_path, "b-fine.log", days_old=LOG_RETENTION_DAYS + 5)

    real_remove = os.remove

    def stubborn(path):
        if "locked" in os.path.basename(path):
            raise PermissionError("檔案正被使用中")
        real_remove(path)

    housekeeping.os.remove = stubborn
    try:
        removed = purge_old_logs(str(tmp_path))
    finally:
        housekeeping.os.remove = real_remove

    assert removed == 1
    assert os.path.exists(os.path.join(str(tmp_path), "a-locked.log"))
    assert not os.path.exists(os.path.join(str(tmp_path), "b-fine.log"))


def test_the_whole_thing_never_raises(tmp_path):
    """**清理是收尾動作，出事只准記 log。**

    它跑在當日訊號、下單、對帳全部完成之後——那時候拋例外只會讓
    一天原本成功的執行以 traceback 收場，而清理本身一點都不重要。
    """
    import housekeeping

    def explode(path):
        raise OSError("磁碟壞了")

    real = housekeeping.os.scandir
    housekeeping.os.scandir = explode
    try:
        assert purge_old_logs(str(tmp_path)) == 0
    finally:
        housekeeping.os.scandir = real
