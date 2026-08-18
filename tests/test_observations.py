"""當日觀測記錄 —— 隔日對帳唯一的依據。

與部位記錄（`state.py`）是**兩件不同的事**，所以是兩個檔案：

  部位記錄  「OS 帳上有什麼」  只有真的下單那天才寫，覆蓋，13:40 讀
  觀測記錄  「今天看到什麼」    每個交易日都寫，累積，隔天早上讀

分開的理由不只是語意。`PositionRecord` 的不變量是繞著「有部位」建立的
（`lots` 與 `status` 綁死、`CONFIRMED` 必須有口數 ≥ 1），
而觀測記錄最常見的一天恰恰是「沒有部位」——不動作、或開關關著。
硬塞進同一個型別會把 ticket 05 建起來的防線弄壞。

這個檔案守的是「**每個交易日都留得下一筆，而且隔天讀得回來**」。
沒有它，ticket 07 的對帳無事可對——那正是 2026-08-17 盤點時發現的洞。
"""

import json

import pytest

from observations import (
    Observation,
    append_observation,
    read_observation_before,
    read_observations,
)
from strategy import LONG, NO_TRADE, SHORT

OBS = Observation(
    trading_day=20260817,
    tx=45850.0,
    mtx=45812.0,
    tmf=45863.0,
    signal=LONG,
    contract_month="202608",
)


def path(tmp_path) -> str:
    return str(tmp_path / "observations.jsonl")


# --- 寫進去讀得回來 ---


def test_a_written_observation_can_be_read_back(tmp_path):
    p = path(tmp_path)
    append_observation(OBS, path=p)
    assert read_observation_before(20260818, path=p) == OBS


def test_the_file_is_created_if_it_does_not_exist(tmp_path):
    """第一個交易日不該因為檔案還不存在就失敗。"""
    p = str(tmp_path / "nested" / "observations.jsonl")
    append_observation(OBS, path=p)
    assert read_observation_before(20260818, path=p) == OBS


def test_no_file_yet_reads_as_nothing_to_reconcile(tmp_path):
    """開機第一天。**不可以是例外**——對帳沒東西可對是正常的，不是故障。"""
    assert read_observation_before(20260818, path=path(tmp_path)) is None


# --- 累積，不覆蓋 ---


def test_appending_keeps_the_earlier_days(tmp_path):
    """部位記錄是覆蓋的，觀測記錄不是。覆蓋掉的話就只剩一天，對帳的價值大半沒了。"""
    p = path(tmp_path)
    append_observation(OBS, path=p)
    append_observation(_at(20260818), path=p)
    lines = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
    assert [x["trading_day"] for x in lines] == [20260817, 20260818]


def test_reading_skips_days_on_or_after_the_cutoff(tmp_path):
    """對帳要拿的是**前一天**的觀測，不是今天剛寫的那一筆。

    今天早上 08:50 才寫的那行，期交所根本還沒公布當日資料——
    拿它去對只會每天都「查無資料」而靜默跳過，對帳等於從來沒跑過。
    """
    p = path(tmp_path)
    append_observation(OBS, path=p)                 # 08/17
    append_observation(_at(20260818), path=p)       # 08/18 ← 今天
    assert read_observation_before(20260818, path=p).trading_day == 20260817


def test_only_today_recorded_means_nothing_to_reconcile(tmp_path):
    p = path(tmp_path)
    append_observation(_at(20260818), path=p)
    assert read_observation_before(20260818, path=p) is None


def test_a_gap_still_reconciles_the_most_recent_earlier_day(tmp_path):
    """連假、或上次沒跑。隔了幾天的觀測**照樣值得對**——

    官方資料查得到就查得到，日期久遠不影響正確性。
    跳過的話，「上週五取到錯誤盤別」就永遠沒人發現。
    """
    p = path(tmp_path)
    append_observation(OBS, path=p)                 # 08/17
    assert read_observation_before(20260824, path=p).trading_day == 20260817


# --- 同一天不重複寫 ---


def test_running_twice_in_a_day_does_not_append_twice(tmp_path):
    """重複執行保護在開關關著時**攔不住**——那條防線看的是部位記錄，
    而開關關著時根本不寫部位記錄。所以這裡要自己擋。
    """
    p = path(tmp_path)
    append_observation(OBS, path=p)
    append_observation(OBS, path=p)
    lines = [x for x in open(p, encoding="utf-8") if x.strip()]
    assert len(lines) == 1


def test_a_rerun_with_different_numbers_replaces_nothing_and_warns(tmp_path):
    """同一天第二次跑，數字卻不一樣——那本身就是個訊號（報價來源不穩）。

    行為上仍然只留一行（第一次那筆，它比較接近 08:45），
    但不可以靜悄悄——所以拋例外讓呼叫端決定怎麼講。
    """
    p = path(tmp_path)
    append_observation(OBS, path=p)
    from observations import ObservationConflict
    with pytest.raises(ObservationConflict):
        append_observation(Observation(
            trading_day=20260817, tx=1.0, mtx=2.0, tmf=3.0,
            signal=LONG, contract_month="202608",
        ), path=p)
    assert len([x for x in open(p, encoding="utf-8") if x.strip()]) == 1


# --- 壞掉的行：**跳過它，但要講出來** ---
#
# 初版是「讀到壞行就拋例外」，理由是「讀不懂當成沒有的話，對帳會靜默跳過
# 而沒有人知道它壞了」。那個顧慮對，但那個解法**更糟**。code-review 實測：
#
#   一行壞掉 → append 讀不到既有記錄 → 之後**再也寫不進任何觀測**
#            → 對帳每天都在「讀不懂」返回 → **永遠不再對帳**
#            → 而且全程只進 log，**一則 Discord 都不發**
#
# 整個稽核能力會安靜地、永久地消失，而系統看起來完全正常——
# 那正是本張票要防的那種錯誤，發生在它自己身上。
#
# 現在的設計：**壞行跳過（其餘照常運作），並把問題回報出去讓呼叫端發告警。**
# 原本的顧慮由「回報」滿足，不必靠「停擺」。


def test_a_damaged_line_does_not_hide_the_good_ones(tmp_path):
    p = path(tmp_path)
    append_observation(OBS, path=p)
    _damage(p)
    records, damaged = read_observations(path=p)
    assert [r.trading_day for r in records] == [20260817]
    assert len(damaged) == 1


def test_a_damaged_line_is_reported_not_swallowed(tmp_path):
    """跳過而不講的話，就變成當初擔心的那件事：壞掉跟正常長得一樣。"""
    p = path(tmp_path)
    _damage(p)
    _, damaged = read_observations(path=p)
    assert damaged and "1" in damaged[0], "要講得出是第幾行"


def test_writing_still_works_after_a_line_is_damaged(tmp_path):
    """**這是初版最嚴重的後果。** 壞一行就再也記不下任何東西，

    而且沒有人會知道——隔日對帳只會顯示「沒東西可對」，跟休假第一天一樣。
    """
    p = path(tmp_path)
    append_observation(OBS, path=p)
    _damage(p)
    append_observation(_at(20260818), path=p)
    records, _ = read_observations(path=p)
    assert [r.trading_day for r in records] == [20260817, 20260818]


def test_reading_before_still_works_around_a_damaged_line(tmp_path):
    p = path(tmp_path)
    append_observation(OBS, path=p)
    _damage(p)
    assert read_observation_before(20260819, path=p).trading_day == 20260817


def test_an_unknown_field_is_reported_as_damage(tmp_path):
    """欄位改名時不可以靜靜地少對一個商品。"""
    _write_raw(path(tmp_path), {**OBS.__dict__, "extra": 1})
    records, damaged = read_observations(path=path(tmp_path))
    assert records == [] and len(damaged) == 1


def test_a_missing_field_is_reported_as_damage(tmp_path):
    data = dict(OBS.__dict__)
    del data["tmf"]
    _write_raw(path(tmp_path), data)
    records, damaged = read_observations(path=path(tmp_path))
    assert records == [] and len(damaged) == 1


def test_a_zero_open_price_on_disk_is_reported_as_damage(tmp_path):
    """建構時擋得住，但檔案是人可以手改的。讀回來時也要擋。"""
    _write_raw(path(tmp_path), {**OBS.__dict__, "tx": 0})
    records, damaged = read_observations(path=path(tmp_path))
    assert records == [] and len(damaged) == 1


def _damage(p: str) -> None:
    """在檔尾追加一行讀不懂的內容——寫到一半斷電就長這樣。"""
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{截斷的" + chr(10))


def _write_raw(p: str, data: dict) -> None:
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data) + chr(10))


# --- 型別上就不可能記錄一筆沒有意義的觀測 ---


@pytest.mark.parametrize("field", ["tx", "mtx", "tmf"])
def test_a_zero_open_price_is_rejected(field):
    """**0 是「報價還沒就緒」的值，不是價格。**

    指數的開盤價不可能是 0。讓 0 寫得進去的話，隔天對帳會拿 0 去比對、
    報一個假的不一致；而真正的問題（報價沒就緒卻照樣算了訊號）被蓋掉。
    """
    with pytest.raises(ValueError, match="開盤價"):
        Observation(**{**OBS.__dict__, field: 0.0})


def test_an_unknown_signal_is_rejected():
    with pytest.raises(ValueError, match="訊號"):
        Observation(**{**OBS.__dict__, "signal": "MAYBE"})


@pytest.mark.parametrize("signal", [LONG, SHORT, NO_TRADE])
def test_all_three_signals_are_recordable(signal):
    """**「不動作」也要記得下來。** 它佔 23.2% 的交易日，
    而那些日子的開盤價一樣需要對帳——訊號是不動作，不代表數字是對的。
    """
    assert Observation(**{**OBS.__dict__, "signal": signal}).signal == signal


def test_a_blank_contract_month_is_rejected():
    """這一欄是為了每個月自動回答「結算日當天選到本月還是次月」。
    空字串會讓那個問題靜靜地沒有答案。
    """
    with pytest.raises(ValueError, match="合約月份"):
        Observation(**{**OBS.__dict__, "contract_month": ""})


def _at(day: int) -> Observation:
    return Observation(**{**OBS.__dict__, "trading_day": day})
