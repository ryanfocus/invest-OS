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
    raw = {"discord": {"enabled": True},
           "quote": {"retry_attempts": 3, "retry_interval_seconds": 60},
           "calendar": {"extra_closures": [], "extra_openings": []},
           "unused": {"knob": 1}}
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


# --- 重試設定的合理性（code-review 2026-08-09：漂移檢查只比 key 名不比值）---


def test_retry_interval_is_long_enough_to_be_worth_retrying():
    """間隔設成 0 等於取消整個重試機制——三次會在毫秒內燒完。

    「未就緒」的時間尺度是秒到分（報價主機延遲、資料尚未公布），不是毫秒。
    30 秒是這個下限的保守值；真正的設定是 60。
    """
    cfg = settings_module.load()
    assert cfg.quote_retry_interval_seconds >= 30, (
        "間隔太短，重試會在資料有機會到齊之前就全部用完"
    )


def test_retry_attempts_is_at_least_two():
    """只試一次就不叫重試了。"""
    assert settings_module.load().quote_retry_attempts >= 2


# --- 日期解析（code-review 2026-08-10：加引號會靜默失效）---


def test_unquoted_yaml_date_becomes_a_real_date():
    import datetime
    parsed = settings_module._parse_dates([datetime.date(2026, 8, 10)])
    assert parsed == frozenset({datetime.date(2026, 8, 10)})


def test_quoted_yaml_date_is_also_accepted():
    """yaml 的 `- "2026-08-10"` 會是字串。實測過：不處理的話颱風假被靜默無視。"""
    import datetime
    parsed = settings_module._parse_dates(["2026-08-10"])
    assert parsed == frozenset({datetime.date(2026, 8, 10)})


def test_datetime_is_narrowed_to_date():
    import datetime
    parsed = settings_module._parse_dates([datetime.datetime(2026, 8, 10, 9, 0)])
    assert parsed == frozenset({datetime.date(2026, 8, 10)})


def test_unparseable_date_raises_at_load_time_not_silently():
    with pytest.raises(ValueError, match="2026/08/10"):
        settings_module._parse_dates(["2026/08/10"], "calendar.extra_closures")


def test_non_date_value_raises():
    with pytest.raises(ValueError):
        settings_module._parse_dates([12345], "calendar.extra_closures")


def test_empty_list_is_an_empty_set():
    assert settings_module._parse_dates([]) == frozenset()
