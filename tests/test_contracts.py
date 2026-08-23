"""近月合約與結算日 —— 都來自商品清單查詢，不是由日期推算。

日期算式（「每月第三個週三」）在假日順延時會失準，而結算日算錯的後果很直接：
出場時間該提前到 13:30 卻用了 13:40，那時合約已經停止交易，單送不出去。

測試資料取自 2026-08-07 實際抓到的商品清單（獨立事實來源）。
"""

from datetime import date

import pytest

from broker import MTX_CODE, TMF_CODE, TX_CODE, ContractInfo, ProductListUnavailable
from broker.capital_wire import parse_product_list

# 實際回傳格式：「%類別碼%類別名%」開頭，接著以「;」分隔的
# 「商品代碼,名稱,最後交易日,交易所代碼」
REAL_SAMPLE = (
    "%201%期指數%"
    "TX00AM,台指近,20260819,TXFH600;"
    "TX08AM,台指08,20260819,TXFH6;"
    "TX09AM,台指09,20260916,TXFI6;"
    "MTX00AM,小台近,20260819,MXFH600;"
    "MTX08AM,小台08,20260819,MXFH6;"
    "TM0000AM,微台近,20260819,TMFH600;"
    "TM2608AM,微台2608,20260819,TMFH6;"
    "TX00,台指近,20260819,TXFH600;"
)


# --- 解析 ---


def test_parses_the_three_products_we_trade():
    contracts = parse_product_list(REAL_SAMPLE)
    assert contracts[TX_CODE].last_trading_day == 20260819
    assert contracts[MTX_CODE].last_trading_day == 20260819
    assert contracts[TMF_CODE].last_trading_day == 20260819


def test_category_header_is_stripped_from_the_first_entry():
    """清單開頭有「%201%期指數%」前綴，不剝掉的話第一筆代碼會變成一串垃圾。"""
    contracts = parse_product_list(REAL_SAMPLE)
    assert TX_CODE in contracts, "第一筆就是大台，前綴沒剝乾淨就會找不到"


def test_contract_month_comes_from_the_last_trading_day():
    """最後交易日必定落在契約月份內，所以年月直接取前六碼——仍然是 API 給的資料。"""
    contracts = parse_product_list(REAL_SAMPLE)
    assert contracts[TX_CODE].contract_month == "202608"


def test_missing_product_raises_rather_than_returning_partial():
    """三缺一就不能算訊號，早點爆比帶著半套資料往下走好。"""
    without_micro = REAL_SAMPLE.replace("TM0000AM,微台近,20260819,TMFH600;", "")
    with pytest.raises(ProductListUnavailable) as exc:
        parse_product_list(without_micro)
    assert TMF_CODE in str(exc.value)


def test_empty_list_raises():
    with pytest.raises(ProductListUnavailable):
        parse_product_list("")


def test_malformed_entries_are_skipped_not_fatal():
    """清單裡混了殘缺列很正常，只要三個目標商品都在就該通過。"""
    noisy = REAL_SAMPLE + "壞掉的一列;,,,;X;"
    assert parse_product_list(noisy)[TX_CODE].last_trading_day == 20260819


# --- 結算日 ---


def test_settlement_day_is_the_contracts_last_trading_day():
    contract = ContractInfo(code=TX_CODE, last_trading_day=20260819)
    assert contract.is_settlement_day(date(2026, 8, 19)) is True


def test_day_before_settlement_is_not_settlement_day():
    contract = ContractInfo(code=TX_CODE, last_trading_day=20260819)
    assert contract.is_settlement_day(date(2026, 8, 18)) is False


def test_day_after_settlement_is_not_settlement_day():
    contract = ContractInfo(code=TX_CODE, last_trading_day=20260819)
    assert contract.is_settlement_day(date(2026, 8, 20)) is False


def test_settlement_detection_does_not_use_the_third_wednesday_rule():
    """假日順延時，最後交易日會不是第三個週三——此時日期算式會判錯而 API 不會。

    構造：最後交易日順延到 08/20（週四）。第三個週三是 08/19，
    若實作偷用日期算式，08/19 會被誤判為結算日、08/20 會被漏掉。
    """
    delayed = ContractInfo(code=TX_CODE, last_trading_day=20260820)
    assert delayed.is_settlement_day(date(2026, 8, 19)) is False, "第三個週三但不是最後交易日"
    assert delayed.is_settlement_day(date(2026, 8, 20)) is True, "順延後的最後交易日"


# --- 下單用的商品代碼 ---
#
# 報價用近月連續代碼（TX00AM），下單卻要指名月份（TX08）——群益官方文件在
# SendFutureOrderCLR 的備註寫明「bstrStockNo帶入TX03」。兩者不是同一個東西。


def test_order_code_is_the_month_specific_one_not_the_continuous_one():
    """下單代碼必須指名月份。

    近月連續代碼是報價用的，不是委託用的。這裡的期望值來自 2026-08-07
    實際抓到的商品清單：大台的本月合約列是 `TX08AM,台指08,20260819`。
    """
    contracts = parse_product_list(REAL_SAMPLE)
    assert contracts[TX_CODE].order_code == "TX08"


def test_each_product_has_its_own_code_format():
    """三個商品的月份代碼格式**不一樣**，不可以用同一條規則拼出來。

    實際清單：大台 TX08（月）、小台 MTX08（月）、微台 TM2608（年月）。
    微台多了年份兩碼——自己組字串的話這裡一定會錯，所以只能從清單讀。
    """
    contracts = parse_product_list(REAL_SAMPLE)
    assert contracts[MTX_CODE].order_code == "MTX08"
    assert contracts[TMF_CODE].order_code == "TM2608"


def test_a_later_month_is_not_mistaken_for_the_front_month():
    """清單裡同時有 TX08 與 TX09，挑錯就會交易到下個月的合約。

    區分依據是最後交易日：近月連續代碼的最後交易日是 20260819，
    TX09 是 20260916。
    """
    contracts = parse_product_list(REAL_SAMPLE)
    assert contracts[TX_CODE].order_code != "TX09"


def test_missing_month_specific_entry_raises_rather_than_guessing():
    """找不到對應的月份代碼時，絕不可以退而求其次用連續代碼。

    連續代碼能不能下單沒有文件保證，猜錯的後果是委託被拒或下到別的東西上。
    """
    without_month = REAL_SAMPLE.replace("TX08AM,台指08,20260819,TXFH6;", "")
    with pytest.raises(ProductListUnavailable) as exc:
        parse_product_list(without_month)
    assert "TX" in str(exc.value)


def test_settlement_day_uses_the_expiring_contract_not_next_month():
    """結算日當天進場用的仍是即將到期的那個合約——它當天 13:30 才停止交易。

    商品清單的近月連續代碼在結算日當天仍指向本月合約，所以照常取用即可。
    """
    contracts = parse_product_list(REAL_SAMPLE)
    assert contracts[TX_CODE].contract_month == "202608"
    assert contracts[TX_CODE].is_settlement_day(date(2026, 8, 19)) is True
