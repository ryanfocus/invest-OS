# OS 策略 — ticket 索引

規格見 [docs/SPEC.md](../../docs/SPEC.md)｜詞彙見 [CONTEXT.md](../../CONTEXT.md)｜決策見 [docs/adr/](../../docs/adr/)

每張 ticket 都是一條**切穿所有層的曳光彈**：完成後可獨立驗證，不是「先做完一整層」。

## 依賴關係

```
01 骨架 + 訊號 + 假 broker + Discord
 └→ 02 真實 broker：登入 + 取開盤價          ⭐ 全案閘門
     └→ 03 交易日與近月合約判定
         └→ 04 進場下單 + 狀態檔 + 開關
             ├→ 05 出場流程
             │   └→ 07 排程與部署
             └→ 06 隔日對帳
```

04 完成後，**05 與 06 可並行**。

## 清單

| # | Ticket | Blocked by |
|---|--------|-----------|
| 01 | [專案骨架 + 訊號計算 + 假 broker + Discord](issues/01-skeleton-signal-fake-broker-discord.md) | — |
| 02 | [真實 broker：登入與取得開盤價](issues/02-real-broker-login-and-open-price.md) | 01 |
| 03 | [交易日與近月合約判定](issues/03-trading-day-and-front-month.md) | 02 |
| 04 | [進場下單、狀態檔與開關](issues/04-entry-order-and-position-state.md) | 03 |
| 05 | [出場流程](issues/05-exit-flow.md) | 04 |
| 06 | [隔日對帳](issues/06-next-day-reconciliation.md) | 04 |
| 07 | [排程與部署](issues/07-scheduling-and-deployment.md) | 05 |

## 三個必須實機驗證的項目

規格標明在這些確認之前**不可開啟自動下單**：

| 項目 | 由哪張 ticket 解決 |
|------|-----------------|
| 微台的商品代號 | 02 |
| **開盤價是 AM 盤還是全盤** | 02 |
| 倉別參數填新倉還是自動 | 04 |

第二項是唯一一個「錯了會讓每天訊號都錯、卻從數字看不出來」的風險。
`tools/verify_login.py` 就是為了回答前兩項而寫的。
