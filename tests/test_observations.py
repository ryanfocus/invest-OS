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
    ObservationUnreadable,
    append_observation,
    read_observation_before,
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


# --- 壞掉的行不可以看起來像「沒有觀測」 ---


def test_an_unreadable_line_raises_instead_of_looking_empty(tmp_path):
    """讀不懂就當成沒有的話，對帳會靜默跳過而沒有人知道它壞了——

    而對帳本來就設計成「沒東西對就安靜結束」，
    兩者長得一模一樣，故障因此永遠不會浮出來。
    """
    p = path(tmp_path)
    append_observation(OBS, path=p)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{壞掉的 JSON\n")
    with pytest.raises(ObservationUnreadable):
        read_observation_before(20260819, path=p)


def test_an_unknown_field_is_rejected(tmp_path):
    """欄位改名時要大聲失敗，不要靜靜地少對一個商品。"""
    p = path(tmp_path)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({**OBS.__dict__, "extra": 1}) + "\n")
    with pytest.raises(ObservationUnreadable):
        read_observation_before(20260819, path=p)


def test_a_missing_field_is_rejected(tmp_path):
    p = path(tmp_path)
    with open(p, "w", encoding="utf-8") as fh:
        data = dict(OBS.__dict__)
        del data["tmf"]
        fh.write(json.dumps(data) + "\n")
    with pytest.raises(ObservationUnreadable):
        read_observation_before(20260819, path=p)


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
