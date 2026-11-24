"""排程腳本的進場時間與程式的時間表**不可以漂移**（ADR-0006）。

進場時間分在兩個地方：`tools/setup_schedule.ps1` 決定幾點叫醒程式，
`main.FIRST_READ_AT` 決定幾點讀開盤價。只改一邊的後果都很安靜：

- 排程改晚、晚過 08:45:05 → 程式一起跑就直接讀，開盤前的準備變成開盤後才做，
  每天慢十幾秒，沒有任何東西會報錯。
- 排程改得太早 → 登入後要守好幾分鐘，連線在開盤前斷掉的機會變大。

SPEC 的「既有前例」要求這種「設定與程式常數不漂移」比照 invest-hm 的
`test_page_load_timeout.py`：直接讀檔比對，不靠註解提醒人同步。
"""

import pathlib
import re
from datetime import datetime, timedelta

import main
import settings as settings_module

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "tools" / "setup_schedule.ps1"

# 實測從起跑到準備好約 9 秒（登入 4、商品清單 1.6、訂閱 0.2、下單準備 3.2）。
# 留一分鐘讓開盤前的重試有空間；超過十分鐘就是守太久了。
MIN_LEAD = timedelta(seconds=60)
MAX_LEAD = timedelta(minutes=10)


def _scheduled_time(stage):
    text = SCHEDULE.read_text(encoding="utf-8-sig")
    m = re.search(rf"Stage\s*=\s*'{stage}';\s*Time\s*=\s*'(\d\d):(\d\d)'", text)
    assert m, f"在 setup_schedule.ps1 找不到 {stage} 的排程時間——格式改了的話這條測試也要跟著改"
    return datetime(2000, 1, 1, int(m.group(1)), int(m.group(2)))


def _scheduled_entry_time():
    return _scheduled_time("entry")


def test_the_entry_run_is_scheduled_early_enough_to_be_ready_before_the_first_read():
    first_read = datetime.combine(datetime(2000, 1, 1).date(), main.FIRST_READ_AT)
    lead = first_read - _scheduled_entry_time()
    assert lead >= MIN_LEAD, f"排程只比第一次讀價早 {lead}，開盤前來不及準備好"
    assert lead <= MAX_LEAD, f"排程比第一次讀價早 {lead}，守太久了"


def test_the_shipped_cutoff_leaves_time_to_retry_after_the_first_read():
    """界線比第一次讀價還早的話，每天都會「讀到了、但已過界線」而不下單。"""
    cutoff = settings_module.load().entry_cutoff
    assert cutoff is not None
    assert cutoff > main.FIRST_READ_AT


def test_the_exit_time_the_entry_run_assumes_is_the_scheduled_one():
    """早上那則「收不到成交回報」告訴使用者「13:40 會先查，現在不要平」；早班過了
    `main._EXIT_TIME` 才重跑時則改講「現在去跑出場」。排程的出場時間改了而它沒跟著改，
    那句話就會在出場那班還沒跑（或早就跑過）的時候說錯，叫人等一個不會來的查詢。
    """
    assert _scheduled_time("exit").time() == main._EXIT_TIME
