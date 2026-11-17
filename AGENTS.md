# AGENTS.md

給所有在此 repo 工作的 AI agent 的常設規則。

## 分工

| 角色 | 職責 |
|---|---|
| 使用者 | 決定需求與目標裝置，自行到 App 確認申請結果 |
| Claude agents | 所有程式碼與測試的撰寫 |
| Codex | 規劃、審查、執行測試、驗證；不撰寫程式碼或測試 |

## 規則

1. **正式申請不是測試。** 驗證用 mock target。真實裝置上不會送出申請的 rehearsal（點無害的畫面元素）可以。
2. **不從點擊推斷申請被接受。** 工具只能記錄本地點擊嘗試與延遲，伺服器端排名不可觀察。
3. **只操作使用者自己的 active UI。** 不做 request flood、API replay、auth bypass。
4. **不宣稱尚未存在的裝置支援。** `REAL_DRIVERS` 為空就說為空。
5. **文件用繁體中文**（台灣用語），技術名詞保留英文。

## 文件

- `docs/plan.md`：結構、指令、驗收規格、本次 session 的臨時分工
- `docs/research-fans-platform.md`：FANS 平台研究
