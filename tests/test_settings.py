"""設定檔與程式不得漂移。

invest-hm 踩過的教訓：settings.yaml 加了 key 但程式沒讀，或程式讀了不存在的 key，
兩種都不會在測試中現形，直到線上壞掉。這裡兩個方向都擋，而且是結構性的——
沒有一份需要人工同步的 key 清單。
"""

import datetime

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


# --- 進場界線：YAML 的六十進位陷阱 ---


def test_the_shipped_cutoff_is_a_real_time_not_a_number():
    """**出貨的設定必須真的是時間。**

    這條守的是 repo 的狀態，不是程式邏輯：`entry_cutoff` 只要寫成
    `9:00`（少一個前導零），YAML 就會把它當成六十進位數字交出 540——
    載入不會出錯，關卡卻變成拿時間跟一個整數比大小。
    """
    cutoff = settings_module.load().entry_cutoff
    assert isinstance(cutoff, datetime.time), f"讀到的是 {cutoff!r}"
    assert cutoff == datetime.time(9, 0)


def test_the_word_none_switches_the_time_gate_off():
    """`entry_cutoff: none` = 不設時間界線，什麼時候跑都下單。

    給「排程補跑也想照樣進場」的人用。
    """
    assert settings_module._parse_clock("none", "order.entry_cutoff") is None
    assert settings_module._parse_clock("NONE", "order.entry_cutoff") is None


def test_an_empty_value_is_an_error_not_an_off_switch():
    """**空值必須是錯誤，不可以被當成「關掉」。**

    關掉一道安全關卡必須是**打得出來的字**。做成「留白＝關掉」的話，
    任何一次手滑刪掉那個值，都會靜靜地把它關掉——而那道關卡擋的是
    「補跑的那一班在中午開倉，然後沒有東西會去平它」。

    ⚠️ 這也是為什麼不用 YAML 的 `null`／`~`：那兩個看起來就像「還沒填」。
    """
    import pytest as _p
    for empty in (None, "", "   "):
        with _p.raises(ValueError, match="entry_cutoff"):
            settings_module._parse_clock(empty, "order.entry_cutoff")


def test_a_cutoff_without_a_leading_zero_is_rejected_with_the_reason():
    """**`9:00` 在 YAML 裡是整數 540，不是時間。**

    這是整個 `_parse_clock` 存在的理由。YAML 1.1 把 `分:秒` 當成六十進位，
    而**有沒有前導零決定了型別**——`09:00` 是字串、`9:00` 是 540。
    使用者手改設定時很容易寫掉那個零。

    錯誤訊息必須指名道姓講出原因：只說「型別不對」的話，使用者會盯著
    一個看起來完全正常的 `9:00` 找不出哪裡錯。
    """
    import yaml
    assert yaml.safe_load("entry_cutoff: 9:00")["entry_cutoff"] == 540, (
        "PyYAML 的行為變了，這條測試守的前提不成立了"
    )
    with pytest.raises(ValueError, match="六十進位") as exc:
        settings_module._parse_clock(540, "order.entry_cutoff")
    assert "09:00" in str(exc.value), "沒有告訴使用者正確寫法"


def test_a_yaml_boolean_cutoff_does_not_get_the_sexagesimal_lecture():
    """`entry_cutoff: yes` 是 bool，講六十進位是答非所問。

    ⚠️ `bool` 是 `int` 的子類，所以判斷順序反了的話這個分支永遠到不了，
       使用者會拿到一段跟他的輸入毫無關係的說明。
    """
    with pytest.raises(ValueError) as exc:
        settings_module._parse_clock(True, "order.entry_cutoff")
    assert "六十進位" not in str(exc.value)


def test_a_malformed_cutoff_is_rejected_at_load_time():
    """看不懂的字串要在載入時就炸，不可以拖到 08:50 才變成看不懂的錯誤。"""
    for bad in ("09", "abc", "9點", "", "25:00", "09:99"):
        with pytest.raises(ValueError, match="entry_cutoff"):
            settings_module._parse_clock(bad, "order.entry_cutoff")


def test_a_config_built_with_a_non_time_cutoff_is_rejected():
    """繞過 `_parse_clock` 直接建 Config 也擋——測試就是這樣建的。"""
    with pytest.raises(ValueError, match="entry_cutoff"):
        _config(entry_cutoff="09:00")


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
