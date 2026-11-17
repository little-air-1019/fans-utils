# FANS 活動申請 clicker：平台可行性調查（修訂版）

- 調查時間：2026-09-22 15:41–16:00 (Asia/Taipei)，唯讀、未登入、未安裝任何工具
- 目標活動：https://app.fans/community/nmixx/event/uoo7zw9oie（event id 1208）
- 申請時間：2026-09-22 19:00–19:10 KST = 18:00–18:10 Asia/Taipei
- 標記說明：**VERIFIED** = 本次直接觀察或官方來源；**INFERENCE** = 由觀察推論；**UNVERIFIED** = 未能觀察，需真機或登入

## 結論

1. **目前的 web build 不提供申請**：按鈕在 web 是元件層級 disabled，文字 key 為 `not-available-on-web`。申請要在 native app `com.jype.fans` 內進行。（VERIFIED，僅代表目前 build 的行為）
2. **平台建議是條件式的**：
   - 使用者有 iPhone → 優先嘗試 macOS 內建 iPhone Mirroring（本機已具備，零安裝），**但必須先通過合成點擊測試**（見 acceptance A）才算可用。
   - 使用者只有 Android 真機 → adb，需使用者開 USB debugging 並安裝 `android-platform-tools`；未在本任務測試。
   - Android emulator → 本機未安裝、本任務未測試，**可行性未知**，非不可行。
   - iOS Simulator → 一般只能執行 simulator build，App Store 版 FANS 預期無法直接安裝；本任務未實測，也未附官方來源，列為 UNVERIFIED。
3. **服務條款**：app.fans/terms 第 10 條第 3 項第 12 款禁止未經同意以 script、bot 等自動化手段存取服務。（VERIFIED，來源 https://app.fans/terms）此為使用者決策事項，本報告只陳述。

## 已驗證事實（VERIFIED）

| # | 事實 | 證據 |
|---|------|------|
| 1 | app.fans 網頁是 Expo / React Native Web 單一 bundle，開發者 Blue Garage | 頁面 `<meta name=author>`、script src `/_expo/static/js/web/index-*.js` |
| 2 | 網頁 Apply 按鈕：`aria-disabled=true`、`tabindex=-1`、class `bg-bg-disabled`；onClick 在 `disabled` 分支直接 return | 對 DOM 節點 `__reactProps` 檢查 |
| 3 | 按鈕文字 i18n key：`app.community-event-detail-screen.control.not-available-on-web` = "Apply on the app" | 解壓後 bundle 字串 |
| 4 | 活動資料：`submissionCategory: BUTTON`、`applyStatus: UPCOMING`、`applicationOpenAt 2026-09-22T19:00:00+09:00`、`applicationCloseAt 19:10:00+09:00`、`isPremiumOnly: true`、限制 AGE≥15、PENALTY≤2 | 頁面自己發的公開 GraphQL query `CommunityEventDetail` |
| 5 | Native app：iOS App ID 6474999628、Team `LX835Z7Q2R`、bundle `com.jype.fans`；Android package `com.jype.fans` | https://app.fans/.well-known/apple-app-site-association、https://app.fans/.well-known/assetlinks.json、https://apps.apple.com/us/app/id6474999628、https://play.google.com/store/apps/details?id=com.jype.fans |
| 6 | Web client 會向 `https://edge.app.fans/time` 取 server time（`epochMs`）；連續 3 次 curl 得到**同一個** epochMs 且 `x-cache: Hit from cloudfront` | network capture + curl |
| 7 | 頁面在 Asia/Taipei 顯示 18:00–18:10，內文 19:00 KST，一致 | snapshot |
| 8 | 本機：macOS 26.4、Apple M5、Bluetooth On、`/System/Applications/iPhone Mirroring.app` 存在 | `sw_vers`、`sysctl`、`system_profiler`、`ls` |
| 9 | 本機沒有 adb、scrcpy、Android SDK、Xcode.app（只有 CommandLineTools）、Appium、cliclick；有 node 22、python3、uv、java、Homebrew | `command -v`、`brew list` |
| 10 | iPhone Mirroring 官方需求：iPhone iOS 18+、Mac macOS 15+ 且 Apple Silicon 或 T2、同一 Apple Account（雙重驗證）、雙方開 Bluetooth 與 Wi-Fi、iPhone 鎖定且在附近 | https://support.apple.com/en-us/120421 |
| 11 | Orca 提供 `orca-emulator`（iOS Simulator）與 `orca-emulator-android`（adb）skill | `orca skills list` |

## 推論（INFERENCE）

- 事實 6 表示 `edge.app.fans/time` 不適合當精準對時來源；clicker 應以 NTP 校正的 Mac 系統時鐘為準。
- Web client 在載入活動頁時觀察到一個 `issueButtonEventV2Token` query。**這是 web build 的行為，不能推論 native app 的申請序列**；native 端流程列為 UNVERIFIED。

## 未驗證（UNVERIFIED）

- Native app 內按下「신청하기」後的 UI 流程（確認 dialog、loading、成功/額滿提示、跳轉）。
- iPhone Mirroring 視窗是否接受合成滑鼠事件，以及所需的 macOS 權限。
- adb / iPhone Mirroring 的實際點擊延遲（本任務未量測，不提供估計值）。
- Android emulator 是否能安裝並執行 FANS。
- iPhone Mirroring 的地區可用性（未找到官方來源，故不列）。

## Clicker 預設方案（正常 UI 操作）

1. **對時**：NTP 校正的 Mac 系統時鐘。
2. **預設行為**：在目標時刻對「有效目標」（已確認座標的按鈕）**單次點擊**。
3. **可選重複**：只有在真機驗證過按下後的 UI 流程（知道成功/失敗長什麼樣）之後，才啟用有上限的重複點擊，並以 UI 變化為停止條件。
4. **中斷保護**：mirroring 視窗消失或 adb device offline → 停止並提示使用者。
5. **預期管理**：clicker 只把點擊時間固定在目標時刻；名額 100 人，結果不保證。

## Acceptance scenarios（真機驗證）

| # | 情境 | 通過條件 |
|---|------|---------|
| A | 合成點擊測試：對 iPhone Mirroring（或 adb）畫面上一個**無害、不會送出任何東西的目標**（例如同一畫面的空白區或返回鍵）點擊 | 畫面出現對應反應，證明合成事件可送達；未通過則該平台不可用 |
| B | 座標取得：使用者已登入 FANS、停在活動頁、按鈕在畫面內 | 記錄到按鈕座標，並以截圖確認框選位置正確 |
| C | 排程單擊：在設定的目標時刻執行一次點擊 | 以 Mac 時鐘記錄的點擊時間與目標時刻的差值落在事前約定的容許範圍內；容許範圍由 A 測得的實際延遲決定 |
| D | 中斷保護：mirroring 斷線或 adb offline | 停止並發出可見警示，不再嘗試點擊 |
| E | 使用者中止：Ctrl-C 或 hotkey | 立即停止，不留殘餘點擊 |

註：對已結束活動的按鈕點擊只能證明點擊送達，**不能**證明實際申請流程，因此不列為 acceptance。

## 目前狀態（2026-09-22 16:18 更新，來自 coordinator）

- 裝置已定：iPhone + macOS iPhone Mirroring。adb 與 emulator 路線不再需要評估。
- Orca Computer Use 已取得 Accessibility 權限；FANS app 已在 iPhone 上執行並登入。
- **尚未驗證**：合成點擊的座標是否正確送達 iPhone Mirroring 視窗（acceptance A、B），以及 app 內按下後的 UI 流程。這兩項是實作前唯一剩下的前置條件。
