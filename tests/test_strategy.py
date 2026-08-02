"""OS 策略的訊號判定。

策略只做一件事：比較大台、小台、微台的開盤價。
規則是「要嘛純大於、要嘛純小於」——任何其他情況都不動作。
"""

from strategy import LONG, NO_TRADE, SHORT, compute


# --- 三種訊號方向 ---


def test_tx_above_both_gives_long():
    """大台開盤價同時大於小台與微台 → 做多。"""
    assert compute(tx=42331, mtx=42298, tmf=42265).signal == LONG


def test_tx_below_both_gives_short():
    """大台開盤價同時小於小台與微台 → 做空。"""
    assert compute(tx=45046, mtx=45184, tmf=45189).signal == SHORT


def test_tx_between_gives_no_trade():
    """大台夾在小台與微台之間 → 不動作。"""
    assert compute(tx=45353, mtx=45378, tmf=45288).signal == NO_TRADE


# --- 「純大於／純小於」的邊界：只要沾到相等就不成立 ---


def test_tx_equal_to_mtx_gives_no_trade():
    """大台等於小台、大於微台 → 不是「純大於」→ 不動作。"""
    assert compute(tx=42331, mtx=42331, tmf=42265).signal == NO_TRADE


def test_tx_equal_to_tmf_gives_no_trade():
    """大台大於小台、等於微台 → 不動作。"""
    assert compute(tx=42331, mtx=42298, tmf=42331).signal == NO_TRADE


def test_all_three_equal_gives_no_trade():
    """三者同價 —— 實測 138 個交易日中確實發生過，不是假想情境。"""
    assert compute(tx=42331, mtx=42331, tmf=42331).signal == NO_TRADE


def test_tx_equal_to_mtx_below_tmf_gives_no_trade():
    """大台等於小台、小於微台 → 不是「純小於」→ 不動作。"""
    assert compute(tx=42298, mtx=42298, tmf=42331).signal == NO_TRADE


def test_mtx_equal_tmf_and_tx_above_gives_long():
    """小台與微台同價、大台高於兩者 → 仍是純大於 → 做多。"""
    assert compute(tx=42400, mtx=42300, tmf=42300).signal == LONG


# --- 訊號結果帶著判斷依據，供 Discord 發報使用 ---


def test_result_carries_the_three_open_prices():
    result = compute(tx=42331, mtx=42298, tmf=42265)
    assert (result.tx, result.mtx, result.tmf) == (42331, 42298, 42265)


def test_compute_is_pure_and_accepts_floats():
    """開盤價還原小數後可能是浮點數，不可只吃整數。"""
    assert compute(tx=42331.5, mtx=42298.0, tmf=42265.25).signal == LONG
