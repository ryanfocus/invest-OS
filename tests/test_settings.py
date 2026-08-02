"""設定檔與程式不得漂移。

invest-hm 踩過的教訓：settings.yaml 加了 key 但程式沒讀，或程式讀了不存在的 key，
兩種都不會在測試中現形，直到線上壞掉。這裡兩個方向都擋，而且是結構性的——
沒有一份需要人工同步的 key 清單。
"""

import pytest

import settings as settings_module


def test_settings_yaml_loads():
    assert isinstance(settings_module.load(), settings_module.Config)


def test_no_orphan_settings_and_no_phantom_reads():
    """yaml 的 key 集合必須與 build() 實際讀取的集合完全相同。

    多出來 → 有設定沒人讀（改了以為有效，其實沒有）
    少掉了 → 程式讀了 yaml 沒有的 key（load() 會 KeyError）
    """
    raw = settings_module.read_raw()
    assert settings_module.consumed_keys(raw) == settings_module.flatten_keys(raw)


def test_orphan_key_is_detected():
    """加一個沒人讀的設定，漂移檢查必須抓到。"""
    raw = {"discord": {"enabled": True}, "unused": {"knob": 1}}
    assert settings_module.consumed_keys(raw) != settings_module.flatten_keys(raw)


def test_missing_key_is_a_loud_failure_not_a_silent_default():
    with pytest.raises(KeyError):
        settings_module.build({})


def test_partially_missing_key_also_fails_loudly():
    with pytest.raises(KeyError):
        settings_module.build({"discord": {}})


# --- .env 讀取 ---


def test_read_env_returns_empty_when_file_absent(tmp_path):
    assert settings_module.read_env("ANYTHING", path=str(tmp_path / "nope.env")) == ""


def test_read_env_returns_empty_when_name_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n", encoding="utf-8")
    assert settings_module.read_env("MISSING", path=str(env)) == ""


def test_read_env_reads_value_and_skips_comments_and_blanks(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# 註解\n\nDISCORD_WEBHOOK_URL=https://example/hook\n", encoding="utf-8")
    assert settings_module.read_env("DISCORD_WEBHOOK_URL", path=str(env)) == "https://example/hook"


def test_read_env_does_not_match_a_name_that_is_only_a_prefix(tmp_path):
    """讀 FOO 不可以撈到 FOO_BAR 的值。"""
    env = tmp_path / ".env"
    env.write_text("FOO_BAR=wrong\nFOO=right\n", encoding="utf-8")
    assert settings_module.read_env("FOO", path=str(env)) == "right"
