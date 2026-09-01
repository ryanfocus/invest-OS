"""靜態檢查 —— 守住**測試執行不到的那段程式**。

`broker/capital.py` 有 400 行只在真的連上群益 COM 元件時才會跑。
在這台機器以外的地方它連 import 都不會成功，所以 500 多條測試裡
沒有任何一條會執行到它——它的錯誤只有在 08:50 那一刻才看得見，
而那時使用者在上班。

2026-08-23 就是這樣：把線路格式搬進 `capital_wire` 時漏掉了
`PRODUCT_CODES` 的 import，`get_open_prices` 一跑就 `NameError`。
全部測試綠燈，`/code-review` 才抓到。這條測試是那次的補救。

⚠️ 只查**用到不存在的名字**這一類。不查沒用到的 import、行長之類的東西——
   那些是風格，紅在這裡只會讓人學會忽略它。
"""

import io
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _python_files():
    """**版控裡的 .py，就這些。**

    原本是走檔案系統再排除幾個目錄名，而那份清單漏掉了 `deploy/` 底下的
    建置產物——裡面有一整套 Python 直譯器，掃它只會掃出別人的問題。

    改用 `git ls-files` 之後，「要守哪些檔案」與「哪些檔案進版控」變成同一件事，
    不必再維護第二份排除清單（而那份清單漏掉的那天，這條測試就開始亂報）。
    """
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        pytest.skip("這裡不是 git 工作區，無法決定哪些檔案該守")
    for name in out.stdout.splitlines():
        path = ROOT / name
        if path.is_file():
            yield path


def test_no_code_refers_to_a_name_that_does_not_exist():
    """用到不存在的名字 = 那一行一跑就 `NameError`。"""
    api = pytest.importorskip(
        "pyflakes.api",
        reason="pyflakes 沒裝，這道關卡形同不存在——見 requirements.txt 的「開發」段",
    )
    from pyflakes import messages
    from pyflakes.reporter import Reporter

    crashers = (messages.UndefinedName,
                messages.UndefinedLocal,
                messages.UndefinedExport)
    found: list[str] = []

    class Collect(Reporter):
        def __init__(self):
            super().__init__(io.StringIO(), io.StringIO())

        def flake(self, message):
            if isinstance(message, crashers):
                found.append(str(message))

        def syntaxError(self, filename, msg, lineno, offset, text):
            found.append(f"{filename}:{lineno}: 語法錯誤 {msg}")

    reporter = Collect()
    for path in _python_files():
        api.checkPath(str(path), reporter)

    assert found == [], (
        "以下的名字不存在，執行到那一行就會 NameError：\n" + "\n".join(found)
    )
