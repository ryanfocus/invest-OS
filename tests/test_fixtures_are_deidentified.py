"""進版控的樣本裡不可以有真實的帳號。

`tests/fixtures/` 收的是**券商回來的真實字串**——那正是它們的價值，
期望值來自交易所而不是來自誰的推導。代價是它們一不小心就會把真實的
帳戶識別資訊一起帶進公開的版控。

## 這個流程失敗過三次，所以要有測試守著

2026-09-04 的稽核發現：

* `onnewdata-spread-2026-08-19.txt` 的檔頭寫著「帳號等識別資訊已置換」，
  **那句話是假的**——第 20、21 行的帳號欄是真值，而且已經推上公開的 GitHub。
* 同一份的欄位 `[27]`（帳號級識別碼）也是真值。專案**第一次遮罩時就把那一欄
  歸為該遮的**（2026-08-17 那份遮成 `0000`），後面三份卻連續漏掉。
* 兩個測試檔的假帳號沿用了真實的 4 碼分公司代碼。

三次都不是「不知道該遮」，是**沒有東西在檢查**。

## 為什麼只遮識別資訊，不遮成交流水

風險一直是「可歸戶」，而歸戶靠的是帳號。帳號拿掉之後，
「某人在某天用某價成交 1 口微台」就只是公開的市場資料。

而委託序號、委託書號、成交編號、成交價、時間戳**必須留著**——
跨格式的串接關係（書號把推播與查詢串起來）是 ticket 09 整個設計的根據，
遮掉的話這些樣本就失去存在的理由了。它們對外人也沒有用：
群益的查詢綁登入帳號，沒有帳密既查不到也送不出委託。

⚠️ 這條測試需要 `.env` 才能知道「真值長什麼樣」。沒有 `.env` 的環境會跳過——
   那是刻意的：它守的是**產生樣本的那台機器**，而樣本就是在那裡產生的。
"""

import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

# ⚠️ **這裡刻意不寫出那個帳號級識別碼的真值。**
#
# 它出現在 OnNewData [27]、GetOrderReport [39]、GetFulfillReport [32]，
# 而專案第一次遮罩時就把它歸為該遮的。但把它寫在這裡，等於為了守一個機密
# 而把那個機密寫進版控——那正是這條測試要防的事。
#
# 所以改由「一致性」來守：同一種格式的同一格，不能有的遮了有的沒遮。
# 那不需要知道遮的是什麼值。


def _secrets() -> dict:
    """從 `.env` 讀出「不可以出現在版控裡」的字串。"""
    env = ROOT / ".env"
    if not env.exists():
        pytest.skip(".env 不存在，無從得知真值長什麼樣（CI 環境的預期行為）")
    values = {}
    for line in io.open(env, encoding="utf-8"):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, raw = line.partition("=")
        raw = raw.strip()
        if not raw:
            continue
        key = key.strip()
        if key == "CAPITAL_FUTURES_ACCOUNT":
            values["期貨帳號"] = raw
            values["分公司代碼"] = raw[:4]
            values["帳號後段"] = raw[4:]
        elif key in ("CAPITAL_USER_ID", "CAPITAL_PASSWORD", "DISCORD_WEBHOOK_URL"):
            values[key] = raw
    return {k: v for k, v in values.items() if len(v) >= 4}


def _tracked_text_files():
    """版控裡的文字檔。用 `git ls-files` 而不是走檔案系統——

    「要守哪些檔案」與「哪些檔案會被推出去」是同一件事，
    不必再維護第二份清單（而那份清單漏掉的那天，這條測試就開始漏報）。
    """
    import subprocess

    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    if out.returncode != 0:
        pytest.skip("這裡不是 git 工作區")
    for name in out.stdout.splitlines():
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            yield name, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue          # 二進位檔：機密不會以明文躺在那裡


def test_no_tracked_file_contains_a_real_account():
    """**版控裡的任何檔案都不可以含真實的帳戶識別資訊。**

    不只 fixture——2026-09-04 的稽核在兩個**測試原始碼**裡也找到真實的
    分公司代碼（假帳號沿用了真的前 4 碼）。所以掃全部追蹤檔，不挑目錄。
    """
    secrets = _secrets()
    offenders = []
    for name, text in _tracked_text_files():
        for label, value in secrets.items():
            if value in text:
                line = text[: text.index(value)].count("\n") + 1
                offenders.append(f"{name}:{line} 含【{label}】")
    assert offenders == [], (
        "這些進版控的檔案含真實識別資訊：\n  " + "\n  ".join(offenders)
        + "\n用 F999/0000 之類的假值置換。⚠️ 只遮識別資訊，"
        "成交價、口數、委託序號、委託書號要留著——那是這些樣本存在的理由。"
    )


def test_the_masking_convention_is_applied_consistently():
    """**同一個欄位在每一份樣本裡都要遮。**

    漏掉的方式不是「不知道要遮」，是「這一份忘了」——`[27]` 在
    2026-08-17 那份遮成 `0000`，後面三份連續漏了三次。

    所以這裡比對的是**一致性**：同一種格式的同一格，不能有的遮了有的沒遮。
    """
    onnewdata = sorted(FIXTURES.glob("onnewdata-*.txt"))
    assert len(onnewdata) >= 2, "至少要兩份才比得出一致性"

    seen = {}
    for path in onnewdata:
        for line in io.open(path, encoding="utf-8"):
            if line.startswith("#") or not line.strip():
                continue
            fields = line.rstrip("\n").split(",")
            for i in (4, 5, 27):
                if i < len(fields) and fields[i].strip():
                    seen.setdefault(i, set()).add(
                        "遮過" if set(fields[i]) <= {"0", "F"} else f"真值@{path.name}"
                    )
            break

    for i, states in sorted(seen.items()):
        assert states == {"遮過"}, f"OnNewData 的欄位 [{i}] 遮得不一致：{sorted(states)}"


def test_the_headers_do_not_claim_something_that_is_not_true():
    """檔頭若宣稱「已置換」，那就必須是真的。

    `onnewdata-spread-2026-08-19.txt` 的檔頭寫著「帳號等識別資訊已置換」而
    實際上沒有——**一句假的保證比沒有保證更糟**，因為它會讓下一個人不去檢查。
    """
    secrets = _secrets()
    for path in sorted(FIXTURES.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        if "置換" not in text and "遮" not in text:
            continue
        for label, value in secrets.items():
            assert value not in text, (
                f"{path.name} 的檔頭宣稱識別資訊已置換，但檔案裡仍有【{label}】"
            )
