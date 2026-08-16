"""設定檔與程式不得漂移。

invest-hm 踩過的教訓：settings.yaml 加了 key 但程式沒讀，或程式讀了不存在的 key，
兩種都不會在測試中現形，直到線上壞掉。這裡兩個方向都擋，而且是結構性的——
沒有一份需要人工同步的 key 清單。
"""

import pytest

import settings as settings_module
from conftest import make_config as _config


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
    """加一個沒人讀的設定，漂移檢查必須抓到。

    刻意從真實設定檔長出來，而不是手寫一份：手寫的那份每次加設定項都要跟著改，
    忘了改就變成「測試在驗一個已經不存在的設定形狀」。
    """
    raw = settings_module.read_raw()
    raw["unused"] = {"knob": 1}
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


# --- 下單設定（ticket 04）---
#
# 這一組的期望值來自 SPEC 與 ticket 04 的驗收條件，不是從程式反推的。


def test_shipped_config_has_auto_ordering_switched_off():
    """**進版控的設定檔必須是未武裝狀態。**

    這條守的不是程式邏輯而是 repo 的狀態：任何人不小心把開著的設定 commit 上來，
    這裡就會紅。ticket 04 的第一條驗收條件（「預設為關閉」）真正的意思就是這個——
    程式碼裡的 fallback 值保護不了任何人，設定檔的實際內容才會。
    """
    assert settings_module.load().auto_order_enabled is False


def test_shipped_config_points_at_the_production_environment():
    """報價必須是真的，所以連線環境是正式環境。

    這與上一條合起來才是安全的組合：正式環境 + 下單關閉 = 只發訊號不下單。
    要驗倉別參數時改成 test，但那個狀態不該進版控。
    """
    assert settings_module.load().capital_environment == "production"


def test_lot_size_below_one_is_rejected_at_load_time():
    """0 口的委託送出去只會拿到看不懂的錯誤，不如在載入時就講清楚。"""
    with pytest.raises(ValueError, match="lots"):
        _config(order_lots=0)


def test_lot_size_above_the_exchange_market_order_cap_is_rejected():
    """**期交所限制每筆市價委託最多 10 口**（一般交易時段，自 108/5/27 起）。

    我們送的是市價 IOC（ADR-0003），所以超過就會被退單。設定成 11 口的話，
    每天早上都會收到一個看不出原因的失敗——不如在載入時就講清楚。

    期望值來自期交所的委託單種說明，不是從程式反推的。
    """
    with pytest.raises(ValueError, match="10"):
        _config(order_lots=11)


def test_the_cap_itself_is_accepted():
    """10 口是上限本身，合法。"""
    assert _config(order_lots=10).order_lots == 10


def test_unknown_product_is_rejected_at_load_time():
    """打錯商品代碼會下到別的東西上——這是必須在載入時就攔下的錯誤。"""
    with pytest.raises(ValueError, match="product"):
        _config(order_product="TX00")     # 少了 AM，是全盤代碼


def test_a_quoted_false_does_not_arm_the_switch():
    """**yaml 的 `auto_enabled: "false"` 是字串，而非空字串一律為真。**

    與 ticket 03 那個「加引號的日期被靜默無視」是同一類錯誤，但方向相反、代價更大：
    那次是該做的事沒做，這次是**不該下單的時候下單**。
    使用者以為自己把開關關著，程式卻照常送出真單。
    """
    with pytest.raises(ValueError, match="auto_enabled"):
        _config(auto_order_enabled="false")


def test_a_quoted_true_is_also_rejected_rather_than_quietly_accepted():
    """「剛好會動」比「明確失敗」更糟——它會讓人以為字串是合法寫法。"""
    with pytest.raises(ValueError, match="auto_enabled"):
        _config(auto_order_enabled="true")


def test_discord_switch_is_type_checked_too():
    """同一類錯誤，同一個防線。"""
    with pytest.raises(ValueError, match="enabled"):
        _config(discord_enabled="false")


# --- 成交回報逾時（ticket 05）---


def test_shipped_fill_timeout_is_long_enough_that_a_normal_fill_arrives_in_time():
    """守的是**出貨設定的值**，不是程式的合法範圍——兩者是不同的事。

    設太短的代價：回報其實會到，只是慢了半秒，程式卻已經記成「不確定」。
    而「不確定」會讓下午那班拒絕自動平倉、改要求人工處理——
    每天都要人介入的系統等於沒有自動化。5 秒是這個判斷的保守下限。

    （設太長沒有對稱的風險：08:50 距離 13:40 還有好幾個小時。
    所以程式只擋 < 1 那種「等於關掉機制」的值，不把 5 秒訂成硬性下限。）
    """
    assert settings_module.load().order_fill_timeout_seconds >= 5


def test_zero_fill_timeout_is_rejected_at_load_time():
    """0 秒等於「不等回報」，每一筆委託都會變成不確定——那是關掉機制，不是設定。"""
    with pytest.raises(ValueError, match="fill_timeout"):
        _config(order_fill_timeout_seconds=0)


def test_unknown_environment_is_rejected_at_load_time():
    """環境只有正式與測試兩種。拼錯時絕不可以「猜一個」——猜錯就是真錢。"""
    with pytest.raises(ValueError, match="environment"):
        _config(capital_environment="prod")


