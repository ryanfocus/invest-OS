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
from state import (
    CONFIRMED,
    UNCERTAIN,
    PositionRecord,
    StateCorrupted,
    read_position,
    write_position,
)

RECORD = PositionRecord(
    trading_day=20260810,
    product=MTX_CODE,
    order_code="MTX08",
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


def test_the_record_carries_the_order_code_the_exit_will_need(tmp_path):
    """出場要送反向委託，而委託帶的是**下單代碼**（MTX08），不是報價代碼。

    不存的話，出場那一班只能重新查商品清單再推導一次。而近月連續代碼
    在結算之後就指向次月了——隔夜殘留的部位若在那時重推，會拿到次月合約，
    「平倉」單就變成開一個新部位。進場時已經知道答案，就不該讓出場再猜一次。
    """
    path = tmp_path / "position.json"
    write_position(RECORD, path=str(path))
    assert read_position(path=str(path)).order_code == "MTX08"


def test_the_recorded_lots_are_the_filled_lots_not_the_requested_ones(tmp_path):
    """市價 IOC 可能部分成交。出場要平的是**實際持有**的量。"""
    path = tmp_path / "position.json"
    partial = PositionRecord(
        trading_day=20260810, product=MTX_CODE, order_code="MTX08",
        contract_month="202608", side=SELL, lots=1, order_seq="SEQ2",   # 委託 3 口、只成交 1 口
    )
    write_position(partial, path=str(path))
    assert read_position(path=str(path)).lots == 1


# --- 「不確定」：已送單，但不知道成交幾口 ---
#
# 這是 ticket 05 的核心。它與「沒有部位」是兩件完全不同的事，而且長得很像：
#
#   確定沒有部位 → 下午什麼都不用做
#   不知道有沒有 → 下午**絕不可以**自動送單（可能是加倉、也可能開出反向新倉），
#                  要發 Discord 叫人去看帳戶
#
# 所以「不確定」不是用 lots=0 表示的——那個值的意思是「確定 0 口」。


def test_an_uncertain_record_can_be_written_and_read_back(tmp_path):
    """回報沒到時，狀態檔仍要留下「我送了單」這件事實。

    什麼都不寫的話，下午那班會以為今天沒進場而完全不動作，
    但帳上可能已經有部位了——那正是這張 ticket 要擋的事。
    """
    path = tmp_path / "position.json"
    record = PositionRecord(
        trading_day=20260810, product=MTX_CODE, order_code="MTX08",
        contract_month="202608", side=BUY, lots=None, status=UNCERTAIN,
        order_seq="SEQ0000000001",
    )
    write_position(record, path=str(path))
    assert read_position(path=str(path)) == record


def test_uncertain_lots_are_none_not_zero(tmp_path):
    """**0 口的意思是「確定沒成交」，不是「不知道」。**

    用 0 表示不確定的話，任何「lots > 0 才處理」的判斷都會靜靜地跳過這筆記錄，
    而那正是最需要有人來看的一筆。
    """
    with pytest.raises(ValueError, match="不確定"):
        PositionRecord(
            trading_day=20260810, product=MTX_CODE, order_code="MTX08",
            contract_month="202608", side=BUY, lots=0, status=UNCERTAIN,
        )


def test_a_confirmed_record_must_have_a_real_lot_count():
    """反過來也要擋：確定的記錄不可以說「幾口不知道」。"""
    with pytest.raises(ValueError, match="CONFIRMED"):
        PositionRecord(
            trading_day=20260810, product=MTX_CODE, order_code="MTX08",
            contract_month="202608", side=BUY, lots=None, status=CONFIRMED,
        )


def test_an_unknown_status_is_rejected():
    with pytest.raises(ValueError, match="status"):
        PositionRecord(
            trading_day=20260810, product=MTX_CODE, order_code="MTX08",
            contract_month="202608", side=BUY, lots=1, status="MAYBE",
        )


def test_uncertain_records_are_flagged_for_the_exit_flow():
    """出場那班要問的問題是「這筆記錄可不可以照著下單」。

    讓它問一個明確的問題，而不是自己去比對 status 字串——
    比對字串的地方一多，遲早有一處會漏掉。
    """
    uncertain = PositionRecord(
        trading_day=20260810, product=MTX_CODE, order_code="MTX08",
        contract_month="202608", side=BUY, lots=None, status=UNCERTAIN,
    )
    assert uncertain.is_uncertain is True
    assert RECORD.is_uncertain is False


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
        trading_day=20260811, product=MTX_CODE, order_code="MTX09",
        contract_month="202609", side=SELL, lots=9, order_seq="SEQ_NEW",
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
