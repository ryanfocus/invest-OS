"""這個程式住在哪裡。

設定檔、狀態檔、日誌的位置全部從這裡推導。**只有這一個地方回答這個問題**——
2026-08-31 之前，`os.path.dirname(os.path.abspath(__file__))` 這一行在
`settings` / `state` / `observations` / `housekeeping` 四個檔案裡各有一份。

那時候沒有差別，因為四份的答案都一樣。打包成 exe 之後就不一樣了，
而四份就是四個各自會漏掉的地方。

## 打包之後為什麼答案會變

PyInstaller 把原始碼收進 `_internal\\`（onedir）或一個每次執行都不同、
跑完就刪掉的暫存目錄（onefile）。所以「原始碼在哪」不再等於「使用者的檔案在哪」——

    使用者拿到的樣子              程式照 __file__ 會找到的地方
    invest-os\\osmain.exe          invest-os\\_internal\\
    invest-os\\config\\settings.yaml   ← 使用者填這個，但程式往上面那層找

⚠️ **實測過的後果**（2026-08-31 用真的 PyInstaller build 出來跑）：
   `FileNotFoundError: ...\\_internal\\config\\settings.yaml`。
   onefile 更糟：部位檔寫進那個會被刪掉的暫存目錄，而且下午那班拿到的是
   另一個暫存目錄——**早上記下的部位下午讀不到**，那正是這套系統最貴的失效模式。
"""

from __future__ import annotations

import os
import sys


def app_root() -> str:
    """程式的根目錄。設定、狀態、日誌都掛在它底下。

    ⚠️ **判斷依據是 `sys.frozen`，不是檔名。** PyInstaller 會在凍結的程式裡
       設下這個屬性，一般的 Python 沒有。改成看「檔名是不是 .exe」的話，
       用 `python.exe` 跑原始碼也會被當成打包版，根目錄會指到
       Python 的安裝資料夾。
    """
    if getattr(sys, "frozen", False):
        # 打包版：使用者的檔案放在 exe 旁邊，不在 exe 裡面
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))
