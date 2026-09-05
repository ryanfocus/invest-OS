"""真實 broker 的可測部分。

COM 呼叫本身沒辦法在測試環境驗證——那正是 broker 接縫存在的理由。
但有一件事一定要守住而且測得了：**載入模組不可以碰 COM**。
群益官方範例在模組層級就建立 COM 物件，照抄的話未註冊 COM 的機器連
`pytest` 收集測試都會失敗，整個專案在拿到憑證之前一行都測不了。
"""

import os
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT


def test_capital_broker_imports_with_comtypes_unavailable(tmp_path):
    """在 `comtypes` 根本無法匯入的環境下，`broker.capital` 仍要 import 得起來。

    做法是在 PYTHONPATH 前面放一個一 import 就爆的假 comtypes，然後開子行程試。
    刻意**不用** `importorskip` 或 monkeypatch——那兩種寫法在沒裝 comtypes 的機器上
    會直接跳過，而那正是這條驗收條件唯一在乎的機器（SPEC user story 49、50）。
    """
    (tmp_path / "comtypes").mkdir()
    (tmp_path / "comtypes" / "__init__.py").write_text(
        "raise ImportError('模擬未安裝 comtypes')", encoding="utf-8"
    )
    (tmp_path / "comtypes" / "client.py").write_text(
        "raise ImportError('模擬未安裝 comtypes')", encoding="utf-8"
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), REPO_ROOT])
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import comtypes_probe_should_fail  # noqa: F401
        """).strip().replace("comtypes_probe_should_fail", "broker.capital")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "broker.capital 在沒有 comtypes 的環境下 import 失敗——"
        f"代表 COM 初始化不是延遲的。\n{result.stderr}"
    )


def test_capital_broker_exposes_the_expected_entry_point():
    """對外只暴露 CapitalBroker；商品代碼從 broker 套件取，不從這裡再匯出一份。"""
    import broker.capital as capital

    assert capital.__all__ == ["CapitalBroker"]
    assert hasattr(capital, "CapitalBroker")
