# OS 策略 — ticket 索引

規格見 [docs/SPEC.md](../../docs/SPEC.md)｜詞彙見 [CONTEXT.md](../../CONTEXT.md)｜決策見 [docs/adr/](../../docs/adr/)

每張 ticket 都是一條**切穿所有層的曳光彈**：完成後可獨立驗證，不是「先做完一整層」。

## 依賴關係

```
01 骨架 + 訊號 + 假 broker + Discord          ✅ done
 └→ 02 真實 broker：登入 + 取開盤價
     └→ 03 交易日與近月合約判定
         └→ 04 進場下單 + 狀態檔 + 開關
             ├→ 05 部位一致性防護  ⭐ 唯一會直接虧錢的失效模式
             │   └→ 06 出場流程
             │       └→ 08 排程與部署
             ├→ 07 隔日對帳
             └→ 09 成交查詢後備（強化，非必要）
```

04 完成後，**05→06→08 這條線與 07 可並行**。

## 清單

| # | Ticket | Blocked by | 狀態 |
|---|--------|-----------|------|
| 01 | [專案骨架 + 訊號計算 + 假 broker + Discord](issues/01-skeleton-signal-fake-broker-discord.md) | — | ✅ done |
| 02 | [真實 broker：登入與取得開盤價](issues/02-real-broker-login-and-open-price.md) | 01 | ✅ done |
| 03 | [交易日與近月合約判定](issues/03-trading-day-and-front-month.md) | 02 | 🟡 待 08/19 驗證 |
| 04 | [進場下單、狀態檔與開關](issues/04-entry-order-and-position-state.md) | 03 | 🟡 待倉別／回報欄位實打 |
| 05 | [部位一致性防護](issues/05-position-consistency-guard.md) | 04 | 🟡 進場側完成，出場側在 06 |
| 06 | [出場流程](issues/06-exit-flow.md) | 05 | |
| 07 | [隔日對帳](issues/07-next-day-reconciliation.md) | 04 | |
| 08 | [排程與部署](issues/08-scheduling-and-deployment.md) | 06 | |
| 09 | [成交查詢後備](issues/09-fill-query-fallback.md) | 05、06 | 強化，優先度低於 06／07 |

## 實機驗證項目

規格標明在這些確認之前**不可開啟自動下單**：

| 項目 | 由哪張解決 | 狀態 |
|------|-----------|------|
| 微台的商品代號 | 02 | ✅ `TM0000AM`（2026-08-07 實測） |
| **開盤價是 AM 盤還是全盤** | 02 | ✅ 必須用 `AM` 後綴（[ADR-0005](../../docs/adr/0005-use-am-session-quote-codes.md)） |
| `nOpen` 在 08:50 是否等於期交所 | 02 | ⏳ 交易日 08:50 跑 `tools/compare_open.py` |
| 結算日當天用的是即將到期的合約 | 03 | ⏳ 2026-08-19 當天實跑 |
| 倉別參數（新倉／自動） | 04 | ⏳ **正式環境 1 口微台實打**——四項中**唯一要花錢**的 |
| `OnNewData` 的欄位位置 | 04 | ⏳ `tools/verify_order_path.py`（**零風險**，待補） |
| 下單前置路徑（初始化／連回報／查帳號） | 04 | ⏳ 同一支工具（**零風險**） |
| `GetFulfillReport` 回傳格式 | 04→09 | ⏳ 同一支工具（**零風險**） |

前兩項曾是「錯了會讓每天訊號都失準、且從數字完全看不出來」的風險。
實測證實：若用 `TX00` 會取到 44129，正確的 `TX00AM` 是 44177——兩者都是合理的台指價位。
這也是 07（隔日對帳）必須保留的理由。
