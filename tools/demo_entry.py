"""用假資料實跑一次進場流程，把訊息真的發到 Discord。

這支存在的目的是驗收：證明「取價 → 算訊號 → 發報」這條鏈在真實環境接得起來，
而不只是在測試裡通過。broker 是假的，Discord 是真的。

用法：
    python tools/demo_entry.py                    # 預設做多情境
    python tools/demo_entry.py 45046 45184 45189  # 自訂三個開盤價

真實的 broker 在 ticket 02 接上，屆時 main 會有自己的正式進入點。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings  # noqa: E402
from broker.fake import FakeBroker  # noqa: E402
from main import run_entry  # noqa: E402
from notifiers.discord import send  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    if len(args) == 3:
        tx, mtx, tmf = (float(a) for a in args)
    elif not args:
        tx, mtx, tmf = 42331.0, 42298.0, 42265.0  # 2026/07/31 實際開盤價，訊號為做多
    else:
        print("用法：demo_entry.py [大台開盤 小台開盤 微台開盤]")
        return 2

    webhook = settings.read_env("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("找不到 DISCORD_WEBHOOK_URL（.env）——只印出訊息，不實際發送")

    sent_payloads = []

    def notify(payload):
        sent_payloads.append(payload)
        if not webhook:
            return False
        return send(payload, webhook)

    # ⚠️ 狀態檔與觀測記錄寫到暫存目錄。這支是示範，不可以動到正式的
    #    `state/`——那裡面是重建不回來的稽核歷史，而它跑的是假開盤價。
    with tempfile.TemporaryDirectory() as scratch:
        outcome = run_entry(
            settings.load(),
            today=date.today(),
            broker=FakeBroker(tx=tx, mtx=mtx, tmf=tmf),
            notify=notify,
            # 示範永遠算「準時」：它要展示的是發報那條鏈，不是時間關卡。
            now=datetime.now().time(),
            state_path=os.path.join(scratch, "position.json"),
            observations_path=os.path.join(scratch, "observations.jsonl"),
            logs_path=scratch,
            fetch_official=lambda day: None,
        )

    print(f"訊號：{outcome.signal}")
    print("-" * 40)
    for payload in sent_payloads:
        print(payload["content"])
    print("-" * 40)
    print(f"已發送到 Discord：{outcome.notified}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
