"""真實 broker 的可測部分。

COM 呼叫本身沒辦法在測試環境驗證——那正是 broker 接縫存在的理由。
但有一件事一定要守住而且測得了：**載入模組不可以碰 COM**。
群益官方範例在模組層級就建立 COM 物件，照抄的話未註冊 COM 的機器連
`pytest` 收集測試都會失敗，整個專案在拿到憑證之前一行都測不了。
"""

import importlib

import pytest


def test_importing_capital_broker_does_not_initialise_com(monkeypatch):
    """把 GetModule 換成會爆的版本，再重新載入模組——沒炸就代表初始化是延遲的。"""
    comtypes_client = pytest.importorskip("comtypes.client")

    def explode(*args, **kwargs):
        raise AssertionError("COM 在模組載入時就被初始化了，違反 ticket 02 的驗收條件")

    monkeypatch.setattr(comtypes_client, "GetModule", explode)

    import broker.capital

    importlib.reload(broker.capital)


def test_capital_broker_reuses_the_shared_product_codes():
    """真假 broker 必須指向同一組代碼，否則 ADR-0005 的保護只擋得住其中一個。"""
    import broker
    import broker.capital as capital

    assert capital.PRODUCT_CODES is broker.PRODUCT_CODES
