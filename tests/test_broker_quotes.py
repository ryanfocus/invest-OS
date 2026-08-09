"""報價就緒判定 —— 真假 broker 共用的契約。

策略需要三個開盤價才能比大小。少一個、或任一個看起來不對，都不是「不動作」，
而是「**無法判斷**」——兩者必須分得開，混為一談會讓故障被當成正常結果。
"""

import pytest

from broker import (
    MTX_CODE,
    PRODUCT_CODES,
    TMF_CODE,
    TX_CODE,
    OpenPrices,
    Quote,
    QuoteNotReady,
    build_open_prices,
)


def _quote(code, open_price, limit_up=48990, limit_down=40084):
    return Quote(code=code, open=open_price, limit_up=limit_up, limit_down=limit_down)


def _all_good(tx=44177, mtx=44142, tmf=44258):
    """2026-08-06 的實際開盤價（期交所一般時段）。"""
    return {
        TX_CODE: _quote(TX_CODE, tx),
        MTX_CODE: _quote(MTX_CODE, mtx),
        TMF_CODE: _quote(TMF_CODE, tmf),
    }


# --- 正常路徑 ---


def test_three_valid_quotes_become_open_prices():
    result = build_open_prices(_all_good())
    assert result == OpenPrices(tx=44177, mtx=44142, tmf=44258)


# --- L1：資料未就緒 ---


def test_zero_open_is_not_ready():
    """開盤價 0 代表當日還沒成交，不是價格。"""
    quotes = _all_good()
    quotes[TX_CODE] = _quote(TX_CODE, 0)
    with pytest.raises(QuoteNotReady) as exc:
        build_open_prices(quotes)
    assert TX_CODE in str(exc.value)


def test_missing_product_is_not_ready_and_names_it():
    """三缺一要講清楚缺哪一個，否則排查時無從下手。"""
    quotes = _all_good()
    quotes[TMF_CODE] = None
    with pytest.raises(QuoteNotReady) as exc:
        build_open_prices(quotes)
    assert TMF_CODE in str(exc.value)


def test_all_problems_are_reported_together():
    """一次講完，不要修好一個才發現還有下一個。"""
    quotes = _all_good()
    quotes[TX_CODE] = None
    quotes[MTX_CODE] = _quote(MTX_CODE, 0)
    with pytest.raises(QuoteNotReady) as exc:
        build_open_prices(quotes)
    message = str(exc.value)
    assert TX_CODE in message and MTX_CODE in message


# --- L2：漲跌停不變量 ---


def test_open_above_limit_up_is_not_ready():
    quotes = _all_good()
    quotes[TX_CODE] = _quote(TX_CODE, 49000, limit_up=48990, limit_down=40084)
    with pytest.raises(QuoteNotReady):
        build_open_prices(quotes)


def test_open_below_limit_down_is_not_ready():
    quotes = _all_good()
    quotes[TX_CODE] = _quote(TX_CODE, 40000, limit_up=48990, limit_down=40084)
    with pytest.raises(QuoteNotReady):
        build_open_prices(quotes)


def test_open_exactly_on_the_limit_is_accepted():
    """漲停鎖死是合法的開盤價，不該被當成髒值。"""
    quotes = _all_good()
    quotes[TX_CODE] = _quote(TX_CODE, 48990, limit_up=48990, limit_down=40084)
    assert build_open_prices(quotes).tx == 48990


def test_each_product_is_checked_against_its_own_limits():
    """漲跌停隨盤別與商品而異，混用會誤判。

    實測：AM 盤 48990／40084，全盤 48683／39833。此處讓大台的開盤價落在
    自己的區間內、卻在小台的區間外——若實作拿錯漲跌停，這個案例就會失敗。
    """
    quotes = _all_good()
    quotes[TX_CODE] = _quote(TX_CODE, 48800, limit_up=48990, limit_down=40084)
    quotes[MTX_CODE] = _quote(MTX_CODE, 44142, limit_up=48000, limit_down=40000)
    assert build_open_prices(quotes).tx == 48800


# --- 守 ADR-0005：盤別代碼不可被誤改 ---


def test_all_product_codes_use_the_am_session():
    """商品代碼必須帶 AM 後綴，否則取到的是全盤（含夜盤）開盤價。

    期望值來自 2026-08-07 實測：TX00AM/MTX00AM/TM0000AM 的 nOpen 與期交所
    一般時段完全一致（44177/44142/44258），而不帶後綴的版本對不到任何已公布時段。
    詳見 docs/adr/0005-use-am-session-quote-codes.md——這不是筆誤，別「簡化」掉。
    """
    for code in PRODUCT_CODES:
        assert code.endswith("AM"), f"{code} 缺少 AM 後綴，會取到全盤開盤價"


def test_micro_taiex_code_uses_four_zeros():
    """微台是 TM0000AM，不是 TM00AM 也不是 TMF00——兩個推定都實測錯過。"""
    assert TMF_CODE == "TM0000AM"
