"""查詢期貨帳號 —— 交付出去的 exe 要能自己回答「我的帳號是什麼」。

`CAPITAL_FUTURES_ACCOUNT` 是開啟自動下單前的必填值。2026-09-02 之前，
取得它的**唯一**指引是 `tools/verify_login.py --show-account`——
而那支程式不在交付包裡（包裡根本沒有 `tools/`，對方也沒有 Python）。
連 exe 自己在缺帳號時印出的錯誤訊息也是這樣講的，等於把人導進死路。
"""

from broker import LoginFailed
from broker.capital_wire import FuturesAccount
from broker.fake import FakeBroker
from main import run_account

TF = FuturesAccount(market="TF", account="F9990012345", name="王小明")
TS = FuturesAccount(market="TS", account="F9999876543", name="王小明")


def _lines(accounts=(), broker=None):
    out = []
    code = run_account(broker or FakeBroker(accounts=list(accounts)), out=out.append)
    return code, "\n".join(str(x) for x in out)


def test_the_account_to_fill_in_is_printed_in_full():
    """完整帳號要印出來——遮罩的話使用者沒辦法填。

    這與 `tools/verify_login.py` 預設遮罩的取捨不同：那支工具的預設用途是
    「貼給別人求助」，這一支的唯一用途就是「抄進 .env」。
    """
    code, text = _lines([TF])
    assert "F9990012345" in text
    assert code == 0


def test_the_market_column_tells_you_which_one_to_use():
    """一個人可能有證券與期貨多個帳號，要填的是 **TF** 那筆。

    挑錯的話下單會失敗，而錯誤訊息看不出原因。
    """
    _, text = _lines([TS, TF])
    assert "TF" in text and "TS" in text
    assert "TF" in text.split("F9990012345")[0].split("\n")[-1], "TF 要和它的帳號在同一行"


def test_the_output_warns_that_it_should_not_be_shared():
    """印的是完整帳號，畫面會被截圖貼給別人。"""
    _, text = _lines([TF])
    assert "不要" in text and ("貼" in text or "傳" in text)


def test_no_accounts_points_at_the_most_likely_cause():
    """查不到時要給下一步，不是只說「查不到」。

    最常見的原因是**期貨 API 下單聲明書沒簽**——沒簽的話群益不會回傳期貨帳號。
    那件事光看「沒有帳號」是想不到的。
    """
    code, text = _lines([])
    assert code != 0
    assert "聲明書" in text


def test_a_login_failure_is_explained_not_raised():
    """登入失敗要印得出來，不可以用 traceback 收場。

    這支是交付出去給人跑的第一支程式，而使用者多半是在帳密還沒填對的
    狀態下第一次執行它。
    """
    code, text = _lines(broker=FakeBroker(login_error=LoginFailed("帳號或密碼錯誤")))
    assert code != 0
    assert "帳號或密碼錯誤" in text
    assert "Traceback" not in text
