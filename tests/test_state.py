"""狀態檔 —— 下午出場時唯一的依據。

這份檔案回答一個問題：「現在帳上有沒有 OS 開的部位，幾口？」
答錯的代價不對稱：

  以為有、其實沒有 → 送出一筆反向委託，開出一個沒人管的新倉
  以為沒有、其實有 → 13:40 不會去平它，部位進夜盤過夜

所以這裡沒有「讀不到就當作沒有」這種寬容——分不出「確實沒有」與「讀壞了」時，
一律大聲失敗，交給人處理。

用真實檔案測試（`tmp_path`），不模擬檔案系統：原子寫入本來就是檔案系統的行為，
把它換掉就等於沒測到。
"""

import json

import pytest

from broker import BUY, MTX_CODE, SELL
from state import PositionRecord, StateCorrupted, read_position, write_position

RECORD = PositionRecord(
    trading_day=20260810,
    product=MTX_CODE,
    contract_month="202608",
    side=BUY,
    lots=2,
    order_seq="SEQ0000000001",
)


# --- 寫進去讀得回來 ---


def test_a_written_record_reads_back_identical(tmp_path):
    """出場那一班是另一個行程，它只認得檔案裡的東西。"""
    path = tmp_path / "position.json"
    write_position(RECORD, path=str(path))
    assert read_position(path=str(path)) == RECORD


def test_absent_file_means_no_position(tmp_path):
    """檔案不存在是正常起始狀態，不是錯誤。"""
    assert read_position(path=str(tmp_path / "nothing.json")) is None


def test_the_recorded_lots_are_the_filled_lots_not_the_requested_ones(tmp_path):
    """市價 IOC 可能部分成交。出場要平的是**實際持有**的量。"""
    path = tmp_path / "position.json"
    partial = PositionRecord(
        trading_day=20260810, product=MTX_CODE, contract_month="202608",
        side=SELL, lots=1, order_seq="SEQ2",     # 委託 3 口、只成交 1 口
    )
    write_position(partial, path=str(path))
    assert read_position(path=str(path)).lots == 1


# --- 壞掉的檔案不可以看起來像「沒有部位」 ---


def test_corrupted_file_raises_instead_of_looking_empty(tmp_path):
    """半個 JSON 讀出 None 的話，出場那一班會以為今天沒進場而什麼都不做，
    帳上的部位就這樣過夜——這正是這個系統最貴的失效模式。
    """
    path = tmp_path / "position.json"
    path.write_text('{"trading_day": 202608', encoding="utf-8")   # 被截斷
    with pytest.raises(StateCorrupted):
        read_position(path=str(path))


def test_file_missing_required_fields_raises(tmp_path):
    path = tmp_path / "position.json"
    path.write_text('{"trading_day": 20260810}', encoding="utf-8")
    with pytest.raises(StateCorrupted):
        read_position(path=str(path))


# --- 原子寫入 ---


def test_an_interrupted_write_leaves_the_previous_record_intact(tmp_path, monkeypatch):
    """寫到一半被中斷時，舊記錄必須原封不動。

    做法是先寫暫存檔、成功後才 `os.replace`（同一個檔案系統上是原子操作）。
    這裡讓 replace 失敗來模擬中斷，然後確認舊記錄還讀得出來。
    """
    import os

    path = tmp_path / "position.json"
    write_position(RECORD, path=str(path))

    def _boom(*_args, **_kwargs):
        raise OSError("模擬寫入中斷")

    monkeypatch.setattr(os, "replace", _boom)
    newer = PositionRecord(
        trading_day=20260811, product=MTX_CODE, contract_month="202609",
        side=SELL, lots=9, order_seq="SEQ_NEW",
    )
    with pytest.raises(OSError):
        write_position(newer, path=str(path))

    assert read_position(path=str(path)) == RECORD, "舊記錄被破壞了"


def test_no_temp_file_is_left_behind_after_a_successful_write(tmp_path):
    """暫存檔留著會讓目錄越來越髒，也會讓人搞不清楚哪個才是真的。"""
    path = tmp_path / "position.json"
    write_position(RECORD, path=str(path))
    assert [p.name for p in tmp_path.iterdir()] == ["position.json"]


def test_the_file_is_readable_json_for_a_human(tmp_path):
    """出事的時候使用者要能自己打開來看，所以不是 pickle 也不是二進位。"""
    path = tmp_path / "position.json"
    write_position(RECORD, path=str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["lots"] == 2 and data["side"] == BUY
