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

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".venv", "__pycache__", "build", "dist", ".git"}


def _python_files():
    for path in sorted(ROOT.rglob("*.py")):
        if not _SKIP_DIRS.isdisjoint(path.parts):
            continue
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
