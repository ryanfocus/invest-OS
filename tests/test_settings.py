"""設定檔與程式不得漂移。

invest-hm 踩過的教訓：settings.yaml 加了 key 但程式沒讀，或程式讀了不存在的 key，
兩種都不會在測試中現形，直到線上壞掉。這裡兩個方向都擋。
"""

import settings as settings_module


def test_settings_yaml_loads():
    cfg = settings_module.load()
    assert isinstance(cfg, settings_module.Config)


def test_every_key_in_settings_yaml_is_consumed():
    """yaml 裡的每個設定項都必須有程式讀取，沒有孤兒設定。"""
    raw_keys = settings_module.flatten_keys(settings_module.read_raw())
    assert raw_keys == settings_module.KNOWN_KEYS


def test_missing_key_is_a_loud_failure_not_a_silent_default():
    """設定缺漏要立刻炸，不要靜默套用預設值後跑出錯誤行為。"""
    import pytest

    with pytest.raises(KeyError):
        settings_module.build({})
