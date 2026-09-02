"""期貨帳號的解析 —— 讓交付出去的 exe 自己查得到帳號。

`CAPITAL_FUTURES_ACCOUNT` 是開啟自動下單前的必填值，而在 2026-09-02 之前，
取得它的**唯一**指引是 `tools/verify_login.py --show-account`——那支程式不在
交付包裡。連 exe 自己的錯誤訊息也是這樣講的，等於把收到程式的人導進死路。

`OnAccount` 的 `bstrAccountData` 以逗號分隔，欄位依序是
「市場, 分公司代碼, 分公司, 帳號, 身分證字號, 姓名」，
而**完整帳號 = 分公司代碼 + 帳號**（那個拼接錯了就會下到別人的帳戶）。
"""

import pytest

from broker.capital_wire import parse_account_row

# 格式取自 tools/verify_login.py 的 `_OrderEvents` 說明（2026-08 實機確認過的欄位順序）
TF_ROW = "TF,F999,台北分公司,0012345,A123456789,王小明"


def test_the_full_account_is_the_branch_code_plus_the_number():
    """**完整帳號 = 分公司代碼 ＋ 帳號**，兩段要接起來。

    只填後面那段的話，委託會被退或送到別的帳戶——而那是要花錢才會發現的錯。
    """
    acct = parse_account_row(TF_ROW)
    assert acct.account == "F9990012345"


def test_the_market_is_kept_so_the_user_can_tell_which_one_to_use():
    """一個人可能有證券（TS）與期貨（TF）多個帳號。

    這個策略交易台灣期貨，要填的是 **TF** 那一筆。挑錯的話下單會失敗，
    而錯誤訊息看不出原因。
    """
    assert parse_account_row(TF_ROW).market == "TF"
    assert parse_account_row(TF_ROW).name == "王小明"


def test_a_row_we_cannot_read_is_skipped_not_guessed():
    """欄位數不對就回 None。

    這條線上有多種市場、而群益的回傳格式我們只實機驗證過期貨那一種。
    看不懂的時候承認看不懂，比拼出一個看起來像帳號的字串安全——
    後者會被人填進 .env，然後在下單那一刻才發現。
    """
    for bad in ("", "TF", "TF,F999,台北", "只有一欄"):
        assert parse_account_row(bad) is None


def test_the_id_number_is_not_carried_out_of_the_parser():
    """⚠️ 原始列的第 5 欄是**身分證字號**，解析結果不帶它。

    這個值會被印在畫面上、可能被截圖貼給別人。帳號本身已經夠敏感了，
    沒有理由讓身分證字號跟著旅行——而它在這裡沒有任何用途。
    """
    acct = parse_account_row(TF_ROW)
    assert "A123456789" not in repr(acct)
