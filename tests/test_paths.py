"""程式住在哪裡 —— 打包成 exe 之後這個問題有兩個答案。

沒打包時，「程式住的地方」就是原始碼所在的資料夾。
打包成 exe 之後，原始碼被塞進 `_internal\\`（onedir）或一個每次執行都不同、
跑完就刪掉的暫存目錄（onefile）——**而使用者填的設定檔不在那裡面**。

實測過的後果（2026-08-31 用真的 PyInstaller build 出來跑）：

    FileNotFoundError: ...\\_internal\\config\\settings.yaml

而 onefile 更糟：部位檔會寫進那個會被刪掉的暫存目錄，
**早上記下的部位，下午那班讀不到**——那正是這套系統最貴的失效模式。
"""

import os
import sys

import paths


def test_without_freezing_the_root_is_the_project_folder():
    """開發時的行為不變：根目錄就是原始碼所在的地方。"""
    root = paths.app_root()
    assert os.path.isfile(os.path.join(root, "config", "settings.yaml")), (
        f"根目錄 {root} 底下找不到 config/settings.yaml"
    )


def test_when_frozen_the_root_is_where_the_exe_sits(monkeypatch, tmp_path):
    """**打包之後要看 exe 在哪，不是看原始碼在哪。**

    使用者拿到的是「exe 旁邊放著 config 資料夾」。程式若照原始碼的位置去找，
    會找進 `_internal\\` 那個他打不開、也不會去放檔案的地方。
    """
    fake_exe = tmp_path / "invest-os" / "osmain.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    assert paths.app_root() == str(fake_exe.parent)


def test_freezing_is_detected_by_the_flag_not_by_the_filename(monkeypatch, tmp_path):
    """判斷依據是 `sys.frozen`，不是檔名長什麼樣。

    寫成「檔名結尾是 .exe 就算打包」的話，用 `python.exe` 直接跑原始碼
    也會被誤判——那時候 `sys.executable` 是直譯器的位置，
    根目錄會指到 Python 的安裝資料夾。
    """
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert paths.app_root() != str(tmp_path), "沒有 sys.frozen 就不該看 sys.executable"
