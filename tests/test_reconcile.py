"""隔日對帳 —— 唯一能抓到「開盤價取到錯誤盤別」的機制。

那種錯誤的可怕之處在於**它不會報錯**：報價代碼少一個 `AM` 後綴就會拿到全盤
（前一日 17:25 夜盤）的開盤價，而 45850 跟 46200 看起來一樣像個合理的數字。
每天的訊號都會錯，從數字本身完全看不出來。期交所是唯一的第三方真相。

這個檔案守兩件事：

1. **對得準**——不一致要報得出是哪個商品、兩邊各是多少
2. **絕不礙事**——對帳自己出任何問題都不可以影響當日策略

第 2 點比第 1 點重要。對帳是純加分功能，漏檢查一天沒事；
但它若讓 08:50 那班掛掉，代價是整天沒有訊號。
所以這裡有一整組「餵它壞東西」的測試。
"""

import pytest

from conftest import RecordingNotifier
from observations import Observation, append_observation
from reconcile import Mismatch, compare_opens, run_reconciliation
from strategy import LONG, NO_TRADE

OBS = Observation(
    trading_day=20260817,
    tx=45850.0,
    mtx=45812.0,
    tmf=45863.0,
    signal=LONG,
    contract_month="202608",
)

OFFICIAL = {"tx": 45850.0, "mtx": 45812.0, "tmf": 45863.0}


def path(tmp_path) -> str:
    return str(tmp_path / "observations.jsonl")


def _run(tmp_path, *, observations=(OBS,), official=OFFICIAL, today=20260818):
    p = path(tmp_path)
    for record in observations:
        append_observation(record, path=p)

    def fetch(day):
        return official(day) if callable(official) else official

    notifier = RecordingNotifier()
    outcome = run_reconciliation(
        today=today, notify=notifier, fetch_official=fetch,
        path=p, discord_enabled=True,
    )
    return outcome, notifier


# --- 比對本身（純函式，直接斷言）---


def test_matching_numbers_produce_no_mismatch():
    assert compare_opens(OBS, OFFICIAL) == ()


def test_a_differing_product_is_reported_with_both_values():
    """訊息要足以讓人**不必自己去查**就知道發生什麼事。"""
    result = compare_opens(OBS, {**OFFICIAL, "tx": 45848.0})
    assert result == (Mismatch(product="大台", ours=45850.0, official=45848.0),)


def test_every_differing_product_is_reported_not_just_the_first():
    """三個都錯時只報一個，會讓人以為另外兩個是對的——
    而「三個都錯」正是取到錯誤盤別的樣子。"""
    result = compare_opens(OBS, {"tx": 1.0, "mtx": 2.0, "tmf": 3.0})
    assert [m.product for m in result] == ["大台", "小台", "微台"]


def test_the_comparison_is_numeric_not_textual():
    """`45850` 與 `45850.0` 是同一個數字。比字串的話每天都會報假警。"""
    assert compare_opens(OBS, {"tx": 45850, "mtx": 45812, "tmf": 45863}) == ()


def test_a_two_point_difference_is_still_reported():
    """2026-08-10 群益 44987、期交所 44985。**不加容差**——

    4 個樣本裡只有 1 次不一致，證據不足以判斷是常態還是雜訊。
    先照原樣報，累積幾週再決定。預先加容忍值會把真正的錯誤一起放過。
    """
    assert len(compare_opens(OBS, {**OFFICIAL, "tx": 45848.0})) == 1


# --- 沒東西可對 → 安靜結束，不是失敗 ---


def test_no_observations_at_all_is_silent(tmp_path):
    """開機第一天。"""
    outcome, notifier = _run(tmp_path, observations=())
    assert outcome.checked_day is None
    assert notifier.sent == []


def test_only_todays_observation_is_silent(tmp_path):
    """08:50 才寫的那筆，期交所還沒公布當日資料。"""
    outcome, notifier = _run(tmp_path, observations=(_at(20260818),), today=20260818)
    assert outcome.checked_day is None
    assert notifier.sent == []


def test_official_data_not_published_yet_is_silent(tmp_path):
    """查無資料不是不一致。當成不一致會每天報一次假警。"""
    outcome, notifier = _run(tmp_path, official=None)
    assert outcome.checked_day is None
    assert notifier.sent == []


def test_not_published_yet_is_distinguished_from_a_broken_format(tmp_path):
    """兩者都會安靜跳過，但**意思完全相反**：

      查無資料   → 正常，明天再對就好
      看不懂     → 期交所改格式了，**程式要修**

    行為一樣的話，這個區別只剩下 `skipped` 這段文字與 log 等級撐著。
    不釘住它，「期交所改版」會被當成「還沒公布」而永遠沒人發現。
    """
    not_yet, _ = _run(tmp_path, official=None)
    broken, _ = _run(tmp_path, official={"tx": 1.0})
    assert "查無資料" in not_yet.skipped
    assert "查無資料" not in broken.skipped


def test_a_gap_of_several_days_still_gets_reconciled(tmp_path):
    """連假之後。跳過的話「上週五取到錯誤盤別」就永遠沒人發現。"""
    outcome, _ = _run(tmp_path, today=20260824)
    assert outcome.checked_day == 20260817


# --- 對得上 → 只寫 log，不打擾 ---


def test_agreement_notifies_nobody(tmp_path):
    """使用者要求每天只有一則訊息（早上那則訊號）。"""
    outcome, notifier = _run(tmp_path)
    assert outcome.checked_day == 20260817
    assert outcome.mismatches == ()
    assert notifier.sent == []


def test_a_no_trade_day_is_reconciled_too(tmp_path):
    """**不動作佔 23.2% 的交易日。** 那些日子的開盤價一樣需要對——

    訊號是不動作，不代表數字是對的。跳過它們等於放掉四分之一的樣本。
    """
    outcome, _ = _run(tmp_path, observations=(_signal(NO_TRADE),))
    assert outcome.checked_day == 20260817


# --- 對不上 → 發 Discord ---


def test_a_mismatch_is_reported_to_discord(tmp_path):
    outcome, notifier = _run(tmp_path, official={**OFFICIAL, "mtx": 45800.0})
    assert len(outcome.mismatches) == 1
    assert outcome.notified is True
    text = notifier.sent[0]["content"]
    assert "小台" in text


def test_the_alert_carries_both_numbers(tmp_path):
    """只說「不一致」的話，人還是得自己去查兩邊各是多少。"""
    _, notifier = _run(tmp_path, official={**OFFICIAL, "mtx": 45800.0})
    text = notifier.sent[0]["content"]
    assert "45812" in text and "45800" in text


def test_a_fractional_difference_is_not_rounded_away(tmp_path):
    """**比對的門檻是 1e-6，所以小數差異會觸發告警。**

    訊息若把兩邊都四捨五入成整數（初版就是 `{:.0f}`），使用者收到的是
    「45812 與 45812 對不上」——完全無從追查。而這正好是最需要說清楚的情況：
    指數點位跑出小數，多半代表期交所改了格式，不是市場的事。
    """
    _, notifier = _run(tmp_path, official={**OFFICIAL, "mtx": 45812.25})
    text = notifier.sent[0]["content"]
    assert "45812.25" in text, f"小數被吃掉了：{text}"


def test_the_alert_names_the_day_being_checked_not_today(tmp_path):
    """對的是**前一交易日**。寫成今天的日期會讓人去查錯的一天。"""
    _, notifier = _run(tmp_path, official={**OFFICIAL, "tx": 1.0}, today=20260818)
    text = notifier.sent[0]["content"]
    assert "08/17" in text or "0817" in text or "08-17" in text


def test_discord_disabled_still_completes_the_comparison(tmp_path):
    """關掉通知不該讓對帳整段跳過——log 裡仍要看得到結果。"""
    p = path(tmp_path)
    append_observation(OBS, path=p)
    notifier = RecordingNotifier()
    outcome = run_reconciliation(
        today=20260818, notify=notifier, fetch_official=lambda d: {**OFFICIAL, "tx": 1.0},
        path=p, discord_enabled=False,
    )
    assert len(outcome.mismatches) == 1
    assert outcome.notified is False
    assert notifier.sent == []


# --- 絕不礙事：餵它壞東西 ---
#
# 對帳掛掉的代價是「少檢查一天」，而 08:50 那班掛掉的代價是「整天沒有訊號」。
# 所以下面每一條的期望值都是「不拋例外」。


def test_a_damaged_line_does_not_stop_the_reconciliation(tmp_path):
    """**壞掉的行只影響它自己那一天，其他天照對。**

    初版是整段停擺（讀到壞行就拋例外 → 對帳永遠不再跑，而且不發任何通知）。
    那讓「稽核能力永久消失」跟「一切正常」長得一模一樣——
    正是本模組要防的那種錯誤，發生在它自己身上。
    """
    p = path(tmp_path)
    append_observation(OBS, path=p)
    _damage(p)
    outcome = run_reconciliation(
        today=20260818, notify=RecordingNotifier(), fetch_official=lambda d: OFFICIAL,
        path=p, discord_enabled=True,
    )
    assert outcome.checked_day == 20260817, "好的那筆照樣要對"


def test_a_damaged_line_raises_an_alert(tmp_path):
    """跳過壞行**必須配上告警**，否則就變回「安靜地少對幾天」。

    這是本檔唯一一則「對帳自己壞了」也要打擾使用者的訊息——
    因為那是個未修復的真實缺陷，不是網路抖一下。
    """
    p = path(tmp_path)
    append_observation(OBS, path=p)
    _damage(p)
    notifier = RecordingNotifier()
    run_reconciliation(
        today=20260818, notify=notifier, fetch_official=lambda d: OFFICIAL,
        path=p, discord_enabled=True,
    )
    assert len(notifier.sent) == 1
    assert "讀不懂" in notifier.sent[0]["content"]


def test_a_damaged_line_alert_does_not_fire_when_the_file_is_clean(tmp_path):
    """對照組：檔案好好的就不該有這則。天天狼來了會讓真的那則被忽略。"""
    _, notifier = _run(tmp_path)
    assert notifier.sent == []


def test_a_fetcher_that_explodes_does_not_raise(tmp_path):
    """網路斷線、期交所改格式、requests 拋任何東西。"""
    def boom(day):
        raise RuntimeError("連線被拒")
    outcome, notifier = _run(tmp_path, official=boom)
    assert outcome.checked_day is None
    assert notifier.sent == []


def test_a_notifier_that_explodes_does_not_raise(tmp_path):
    """Discord webhook 掛掉不該把整班拖下水。"""
    p = path(tmp_path)
    append_observation(OBS, path=p)

    def boom(payload):
        raise RuntimeError("webhook 500")

    outcome = run_reconciliation(
        today=20260818, notify=boom, fetch_official=lambda d: {**OFFICIAL, "tx": 1.0},
        path=p, discord_enabled=True,
    )
    assert outcome.notified is False


def test_official_data_missing_a_product_does_not_raise(tmp_path):
    """期交所改欄位、或只回了兩個商品。**不可以拿缺的那個去比。**"""
    outcome, notifier = _run(tmp_path, official={"tx": 45850.0, "mtx": 45812.0})
    assert outcome.checked_day is None
    assert notifier.sent == []


def test_official_data_with_a_junk_value_does_not_raise(tmp_path):
    outcome, notifier = _run(
        tmp_path, official={"tx": "四萬五", "mtx": 45812.0, "tmf": 45863.0})
    assert outcome.checked_day is None
    assert notifier.sent == []


def test_a_notifier_that_explodes_on_the_damage_alert_does_not_raise(tmp_path):
    """告警本身送不出去，也不可以把整班拖下水。

    這一條特別容易漏：損壞告警是在函式**前段**發的，比不一致那則早得多，
    只顧後面那則的話，前面這則就會在保護傘外面。
    """
    p = path(tmp_path)
    append_observation(OBS, path=p)
    _damage(p)

    def boom(payload):
        raise RuntimeError("webhook 500")

    outcome = run_reconciliation(
        today=20260818, notify=boom, fetch_official=lambda d: OFFICIAL,
        path=p, discord_enabled=True,
    )
    assert outcome.checked_day == 20260817


def test_a_half_written_record_does_not_raise(tmp_path):
    """寫到一半斷電留下的殘缺 JSON。"""
    p = path(tmp_path)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"trading_day": 20260817}' + chr(10))
    run_reconciliation(
        today=20260818, notify=RecordingNotifier(), fetch_official=lambda d: OFFICIAL,
        path=p, discord_enabled=True,
    )


def _damage(p: str) -> None:
    """在檔尾追加一行讀不懂的內容——寫到一半斷電就長這樣。"""
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{截斷的" + chr(10))


def _at(day: int) -> Observation:
    return Observation(**{**OBS.__dict__, "trading_day": day})


def _signal(signal: str) -> Observation:
    return Observation(**{**OBS.__dict__, "signal": signal})
