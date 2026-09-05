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
_SKIP = {".venv", ".build-venv", "build-dist", "build-work", "build-spec", "dist", "__pycache__", ".git"}


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


def test_only_one_module_asks_where_the_program_lives():
    """**`__file__` 只准出現在 `paths.py`。**

    2026-08-31 之前，`os.path.dirname(os.path.abspath(__file__))` 在
    `settings` / `state` / `observations` / `housekeeping` 四個檔案裡各有一份。
    那時候沒差，因為四份的答案一樣；打包成 exe 之後答案變了，而四份就是四個
    各自會漏掉的地方——漏掉的那個不會報錯，只會讀不到設定，或把部位寫進一個
    開機就消失的資料夾。

    ⚠️ 守的是**會被打包進交付包的程式**。`tools/` 與 `tests/` 不會進去。
    """
    offenders = []
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tools/", "tests/")) or rel == "paths.py":
            continue
        with open(path, encoding="utf-8") as fh:
            if "__file__" in fh.read():
                offenders.append(rel)
    assert offenders == [], (
        f"這些檔案自己去問「程式在哪」，打包後會指到錯的地方：{offenders}。"
        "改用 paths.app_root()。"
    )


# --- Windows 的兩個編碼陷阱（這個 repo 踩過三次，之前沒有測試守著）---


def test_every_cmd_file_is_ascii_and_crlf():
    """`.cmd` 必須純 ASCII ＋ CRLF。

    cmd 會在 `chcp` 生效**之前**用系統編碼讀整個檔案，所以 UTF-8 的中文註解
    會被弄壞，嚴重時連解析都會壞掉。2026-08-23 踩過一次——當時的症狀是
    排程「跑了但什麼都沒發生」，而那是最難查的一種。

    理由寫在檔案本身，說明則放在 `tools/setup_schedule.ps1` 與 `docs/DEPLOY.md`
    那些吃得下 UTF-8 的地方。
    """
    for path in ROOT.rglob("*.cmd"):
        if not _SKIP.isdisjoint(path.parts):
            continue
        raw = path.read_bytes()
        rel = path.relative_to(ROOT).as_posix()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AssertionError(f"{rel} 有非 ASCII 字元：{exc}") from None
        assert b"\r\n" in raw, f"{rel} 不是 CRLF"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 有 BOM，cmd 會把它當成命令"


def test_every_powershell_script_has_a_bom():
    """`.ps1` 必須有 BOM。

    PowerShell 5.1 沒有 BOM 就用系統編碼（這台是 cp950）讀檔，中文全毀。
    這個 repo 踩過三次。
    """
    for path in ROOT.rglob("*.ps1"):
        if not _SKIP.isdisjoint(path.parts):
            continue
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), (
            f"{path.relative_to(ROOT).as_posix()} 沒有 BOM，PowerShell 5.1 會用 cp950 讀它"
        )


def test_both_run_stage_variants_keep_the_log_redirect():
    """**兩份 `run_stage.cmd` 都必須保留那個 `>>` 重導向。**

    每日日誌完全來自它，程式裡沒有任何 FileHandler。少了它，排程照樣跑、
    照樣下單，但一個字都不會留下——而唯一的故障偵測是「早上沒收到 Discord」，
    那不會為「有跑但跑歪」觸發。

    兩份是刻意的（一份叫 python、一份叫 exe），cmd 的編碼陷阱太多，
    不在裡面做分支。代價是它們會漂，所以守住不能漂的那一行。

    ⚠️ 只看**可執行的行**。初版寫成「檔案裡有 `>>`」，而檔案的註解裡就寫著
       `">>"` 這三個字——那條斷言被自己的說明文字滿足了，拿掉真正的重導向
       之後仍然是綠的（2026-09-01 突變測試當場抓到）。
    """
    for rel in ("tools/run_stage.cmd", "deploy/templates/run_stage.cmd"):
        code = [ln for ln in (ROOT / rel).read_text(encoding="ascii").splitlines()
                if ln.strip() and not ln.strip().upper().startswith("REM")]
        redirects = [ln for ln in code if ">>" in ln and "logs" in ln]
        assert redirects, f"{rel} 的可執行行裡沒有把輸出導進 logs"
        assert all("2>&1" in ln for ln in redirects), (
            f"{rel} 沒有把錯誤一起收進日誌——traceback 會消失，"
            "而那正是出事時唯一看得到的東西"
        )
