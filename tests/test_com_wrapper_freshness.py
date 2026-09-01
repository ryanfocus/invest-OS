"""打包版必須用**對方機器上那顆** SKCOM.dll 產生的介面定義。

程式第一次用群益元件時，會現場讀那顆 dll 產生一份「說明書」
（哪個函式吃幾個參數、哪個結構有多大），存起來重複用。
說明書上記著 dll 的檔案時間，下次啟動比對一下，不合就重新產生。

⚠️ **那道保險在打包版是關掉的。** `comtypes/_tlib_version_checker.py`：

    if not hasattr(sys, "frozen"):      # ← 打包後整段跳過
        ...
        raise ImportError("Typelib different than module")

後果是實測過的：SKCOM 2.13.42 的 `SKSTOCKLONG` 是 128 bytes、2.13.58 是
144 bytes，而取開盤價的 `SKQuoteLib_GetStockByNoLONG` 是把緩衝區交給元件寫入的。
用錯大小不會報錯——`nOpen` 在 offset 128 以下，**價格看起來完全正常**。

所以打包版每次啟動前要把舊的說明書丟掉，讓它照對方的 dll 重新產生。
"""

import os

import pytest

from broker.capital import discard_generated_com_wrapper


def _wrapper_dir(tmp_path):
    d = tmp_path / "comtypes_cache" / "osmain-312"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("", encoding="utf-8")
    (d / "SKCOMLib.py").write_text("# 舊版產生的說明書", encoding="utf-8")
    (d / "_75AAD71C_0_1_0.py").write_text("# 同上", encoding="utf-8")
    (d / "stdole.py").write_text("# 同上", encoding="utf-8")
    return d


def test_the_generated_wrapper_is_thrown_away(tmp_path):
    d = _wrapper_dir(tmp_path)
    discard_generated_com_wrapper(str(d))
    assert not (d / "SKCOMLib.py").exists()
    assert not (d / "_75AAD71C_0_1_0.py").exists()


def test_the_package_marker_survives(tmp_path):
    """`__init__.py` 要留著——那是 `comtypes.gen` 這個套件本身。

    連它一起刪掉的話，重新產生時 import 會找不到套件。
    """
    d = _wrapper_dir(tmp_path)
    discard_generated_com_wrapper(str(d))
    assert (d / "__init__.py").exists()


def test_a_missing_directory_is_not_an_error(tmp_path):
    """還沒產生過任何東西是正常的（第一次執行），不該當成失敗。"""
    discard_generated_com_wrapper(str(tmp_path / "從來沒有過"))


def test_a_wrapper_that_cannot_be_removed_stops_the_program(tmp_path, monkeypatch):
    """**丟不掉就不要交易。**

    這與 `purge_old_logs`「絕不拋例外」的原則刻意相反。清日誌失敗只是
    佔一點磁碟；丟不掉舊說明書卻繼續跑，意味著可能拿對方機器的元件
    去填一個照我們機器格式配置的緩衝區——而那條路上有每天早上的開盤價。

    大聲失敗會被上層接住並發 Discord，那天不交易。
    安靜地用錯的格式交易，才是這裡真正要避免的事。
    """
    d = _wrapper_dir(tmp_path)

    def _refuse(path):
        raise PermissionError("檔案被鎖住")

    monkeypatch.setattr(os, "remove", _refuse)
    with pytest.raises(Exception) as exc:
        discard_generated_com_wrapper(str(d))
    assert "SKCOMLib.py" in str(exc.value) or "說明書" in str(exc.value)
