# fans-utils 專案規劃

> **狀態（2026-09-22）：核心 scheduler、profile、CLI 與 iPhone Mirroring 的 macOS driver 已實作並整合；wheel 打包、mock target 與真實 Mirroring 視窗的無害點擊 rehearsal、切走 app 時拒絕點擊的 guard 皆已通過。本次新增選用的 `--guarded-retry`（畫面未變才補點）、`rehearse` 子命令、down/up 延遲分項、`clockcheck` 與 keep-awake。`--guarded-retry` 已用真實截圖在 Safari + mock 頁完成驗收（見下方表格），鏡像視窗上的 guarded 測試因 Mac 鎖定未跑完。Apply 按鈕的 calibration 已存好並通過 `check`；Apply 未被點擊，正式 `schedule --live` 尚未啟動。**
>
> **上線前的操作提醒：** 使用者手機目前停在別的 app。正式執行前必須先把手機切回 FANS 的活動頁，並確認 Mirroring 視窗在前景、位置與大小跟校準時一致，否則 driver 會拒絕點擊。
>
> **範圍限制：本工具只做「在指定時刻點一次 Apply」。點擊之後的表單、確認頁或任何後續步驟尚未觀察、未自動化，由使用者在 App 上處理。**
> 本工具**不保證**能搶到前 100 名。伺服器端的申請順序由 FANS 平台決定，本地工具只能減少人為反應時間。

## 1. 目標

為個人使用的 FANS 平台活動建立一組可擴充的小工具。第一個工具是**定時點擊器（timed clicker）**：在使用者指定的時刻，於使用者自己正在操作的畫面上，點一次（或有上限的幾次）已校準好的座標。

設計原則：

- Mac-local，只操作使用者自己的 active UI。
- 不做 request flood、不做 API replay、不繞過登入或驗證。
- 一個活動 = 一個 profile，新增活動不需要 fork 實作。
- scheduler、click driver、event profile 三者分離，不預建框架。

## 2. 第一個參考活動：NMIXX 官方活動

| 項目 | 內容 |
|---|---|
| 活動頁 | https://app.fans/community/nmixx/event/uoo7zw9oie |
| 公告日 | 2026-09-22 |
| 報名時段 | 2026-09-22 19:00–19:10 Asia/Seoul（KST, UTC+9）= 18:00–18:10 Asia/Taipei（UTC+8） |
| 活動日 | 2026-10-06 |
| 名額 | 100 名正取 + 10 名候補，依申請順序給號 |
| 資格 | NSWER 4 期會員 |
| 報名管道 | 目前活動頁的瀏覽器 CTA 為「Apply on the app」，報名動作導向 App |

兩個時區都不採用日光節約時間，KST 減 1 小時就是台北時間。

使用者打算在今天的報名時段使用本工具，裝置已選定 iPhone Mirroring（第 3 節）。driver 與 CLI 整合已完成；**能否上線取決於使用者本機的 Accessibility 權限、calibration，以及在 mock target 或無害 UI 上完成一次成功的 `fire` rehearsal。** 這裡不做任何承諾，也不預測趕不趕得上。

## 3. 目標裝置：iPhone + Mac iPhone Mirroring（使用者已於 2026-09-22 16:09 台北確認）

目前活動頁把報名導向 App。使用者選定 iPhone Mirroring，點擊落在 Mac 上的 Mirroring 視窗。

| 項目 | 內容 |
|---|---|
| Driver 位置 | `fans_utils/macos.py`（已實作，詳見 [macos-driver.md](macos-driver.md)） |
| 機制 | ctypes 呼叫 CoreGraphics / AppKit：一次同步的 mouseMoved → leftMouseDown → leftMouseUp，每次點擊前重新檢查前景 bundle、視窗存在、bounds 未變、座標仍在視窗內；不探索隱藏 API |
| 校準 | `python3 -m fans_utils.macos calibrate --in N`：倒數後只讀游標位置（不點），連同視窗 bounds 存到 `<repo>/calibration.local.json`（已在 `.gitignore`） |
| Rehearsal | `python3 -m fans_utils.macos fire --in 5`：preflight、倒數、恰好一次受保護的真實點擊，對 `tools/mock-click-target.html` 或 Mirroring 的無害畫面元素 |
| 座標幾何假設 | global logical points；座標只在校準當時的視窗位置、大小、螢幕配置下有效。視窗移動、換螢幕、改解析度都必須重新校準，preflight 與 click 都會比對 bounds 並拒絕。 |
| 網頁路徑 | 未驗證，不採用 |

CLI 整合方式：`driver.py` 的 `REAL_DRIVER_IMPORTS = {"macos": ("fans_utils.macos", "MacOSClickDriver")}`，`--live` 時才 `import`，所以非 macOS 上 core 的 import 與測試不受影響。`schedule --live` 流程：驗證 profile/參數 → 過期直接拒絕（exit 3）→ 建 driver（`--calibration` 有給就轉給 `calibration_path`）→ `preflight()` 印警告 → 倒數 → `run()` → `finally: close()`。dry-run 永遠不建真實 driver。

## 4. 目前結構（2026-09-22，以 repo 為準）

Python 3.11+，僅用標準函式庫。

```
fans-utils/
├── pyproject.toml            # setuptools；profiles/ 以 package-data 打進 wheel（fans_utils.profiles）
├── fans_utils/
│   ├── profile.py            # EventProfile、load_profile、時區驗證
│   ├── driver.py             # ClickDriver protocol（preflight/click/close）、MockClickDriver、延遲載入的 REAL_DRIVER_IMPORTS
│   ├── scheduler.py          # 等待有效目標時間（嚴格不早於）、有上限的點擊、guarded retry、取消
│   ├── cli.py                # list / show / schedule / rehearse（dry-run 與 --live 生命週期）
│   ├── visual.py             # MacOSVisualGuard：--live --guarded-retry 才延遲載入的螢幕觀察器
│   ├── macos.py              # iPhone Mirroring 座標點擊 driver + diagnose/calibrate/check/fire 子命令
│   └── __main__.py
├── profiles/
│   └── nmixx-uoo7zw9oie.json # 單一來源；repo 執行讀這裡，wheel 安裝讀 fans_utils/profiles/
├── tools/mock-click-target.html   # 無害的本機點擊目標
├── tests/                    # fakes.py、test_profile.py、test_scheduler.py、test_cli.py、test_macos.py
└── docs/
```

Profile 目錄的查找順序：`fans_utils/profiles/`（wheel 安裝）→ 套件旁的 `<repo>/profiles/`（checkout，與 cwd 無關）→ `./profiles`；`--profiles-dir` 覆寫。wheel 打包已於 2026-09-22 驗證：coordinator 以 `python3 -m pip wheel --no-deps --wheel-dir /tmp/fans-utils-wheels .` 建 wheel、安裝到獨立目錄，從 `/tmp` 執行 `python3 -m fans_utils list` / `show` 都能讀到打包進去的同一份 event JSON。

三層分離的理由：scheduler 與裝置無關，可在沒有真實 driver 時完整測試；driver 是唯一碰平台的層；profile 是資料，新活動加一個 JSON 檔即可。

### Profile schema（`profiles/nmixx-uoo7zw9oie.json` 為權威）

```json
{
  "id": "nmixx-uoo7zw9oie",
  "name": "NMIXX FANS event uoo7zw9oie",
  "url": "https://app.fans/community/nmixx/event/uoo7zw9oie",
  "timezone": "Asia/Seoul",
  "signup_start": "2026-09-22T19:00:00+09:00",
  "signup_end": "2026-09-22T19:10:00+09:00",
  "capacity": 100,
  "alternates": 10,
  "channel": "app-only",
  "notes": "Taipei 18:00-18:10. Channel verified from the public browser CTA (app-only)."
}
```

Profile 只描述活動事實。執行參數（click 數、offset、interval）是 CLI 參數，不在 profile 中。

## 5. 目前可執行的指令

以下輸出皆為 2026-09-22 16:03 台北實際執行結果。

```sh
python3 -m fans_utils --help
```
```
usage: python3 -m fans_utils [-h] [--version] [--profiles-dir PROFILES_DIR]
                             {list,show,schedule} ...
```

```sh
python3 -m fans_utils show nmixx-uoo7zw9oie
```
```
signup_start: 2026-09-22T19:00:00.000+09:00  |  Taipei 2026-09-22T18:00:00.000+08:00  |  UTC 2026-09-22T10:00:00.000+00:00
signup_end:   2026-09-22T19:10:00.000+09:00  |  Taipei 2026-09-22T18:10:00.000+08:00  |  UTC 2026-09-22T10:10:00.000+00:00
capacity:     100 (+10 alternates)
channel:      app-only
now:          2026-09-22T16:03:13.956+08:00 Taipei  ->  starts in 7006.0s
```

Rehearsal（dry-run，忽略 profile 起始時間，以現在 + 0.2 秒為目標，mock driver）：

```sh
python3 -m fans_utils schedule nmixx-uoo7zw9oie --rehearse-in 0.2
```
```
mode:             DRY-RUN (mock driver, no real click)  ** REHEARSAL: profile start ignored, target = now + 0.2s **
adjust:           +0 ms
effective target: 2026-09-22T17:03:21.424+09:00  |  Taipei 2026-09-22T16:03:21.424+08:00  |  UTC ...
plan:             max 1 click(s), interval 250 ms, duration cap 5000 ms, driver mock
waiting:          0.200s  (Ctrl-C to cancel)
status:           completed
clicks attempted: 1  (dry-run via mock)
lateness ms:      [2.101]
note: counts are local click attempts only; FANS application acceptance and server ranking are not observable from this tool.
```

`schedule` 參數與預設值：

| 參數 | 預設 | 說明 |
|---|---|---|
| `--adjust-ms` | 0 | 目標時間位移，負值提早 |
| `--max-clicks` | 1 | 點擊上限 |
| `--interval-ms` | 250 | 點擊間隔 |
| `--max-duration-ms` | 5000 | 有效目標後多久停止 |
| `--guarded-retry` | 關 | 選用。畫面沒變才補點，上限 5 次 / 2000 ms / 間隔 ≥100 ms，預設 3 / 150 / 800 |
| `--rehearse-in SECONDS` | 無 | 僅 dry-run，不能與 `--live` 併用；真實點擊的 rehearsal 用 `rehearse` 子命令 |
| `--live --driver NAME` | driver 預設 `macos` | 使用真實 driver：preflight → 倒數 → 點擊 → close |
| `--calibration PATH` | driver 預設 `<repo>/calibration.local.json` | 僅 `--live`，轉給 driver 的 `calibration_path` |

Exit code：0 完成 · 1 profile/參數錯誤或點擊時 driver 拒絕（status error）· 2 用法錯誤 · 3 目標已過期 · 4 driver 不存在或無法載入 · 5 driver 建立（calibration）、preflight 或螢幕觀察器建立失敗 · 6 `--guarded-retry` 因畫面改變而停止 · 130 Ctrl-C。4、5 都發生在任何等待或點擊之前。

### `--guarded-retry`（本次新增，選用）

動機：單次點擊有一個觀察不到的失敗模式——按鈕還沒 enable 就點下去，事件送出但什麼也沒發生。補點只在「畫面沒變」時做：

- 有效目標前約 `ARM_LEAD_S`（100 ms）拍 baseline，拍照成本由等待吸收。單元測試以 30 ms 與 80 ms 的拍照成本驗證 dispatch lateness 為 0；本機 5 次取樣也沒有觀察到第一次點擊被延後（單次 0.79–5.01 ms、guarded 0.75–5.04 ms，分布重疊）。這是本機量測結果，不是「一定準時」的保證。
- 每次點擊後等 interval，再拍一張跟 baseline 比；**比對就在下一次點擊之前**，所以 interval 期間才出現的變化也會擋掉下一次點擊。
- 畫面改變 → status `screen_changed`、exit 6、立刻停止。這**不是**成功訊號。
- `arm()` / `changed()` 拋錯（截圖失敗、權限、幾何不符）→ status `error`、不再補點。絕不把失敗當成「沒變化」。
- 上限寫在 `ClickPlan.__post_init__`，`schedule` 與 `rehearse` 共用，超過就 exit 1。

已知限制（刻意保守，不是故障）：baseline 在第一次點擊之前拍，所以如果 Apply 按鈕正好在目標時刻 enable，那個變化會被歸給第一次點擊，執行結果是點一次後停止。輸出的 `baseline age ms` 就是這段的長度。

### `rehearse` 子命令（本次新增）

`schedule --rehearse-in` 維持 dry-run only 的限制不變。真實點擊的無害演練改由 `rehearse` 負責：`--calibration` 必填且無預設值（正式 Apply 的校準不可能被誤用）、不讀 profile、**不套用** `REAL_DRIVER_LIVE_KWARGS` 的 iPhone Mirroring 鎖定、`--bundle` 由使用者指定、`--in` 與 `--at`（帶 offset 的 ISO 時刻）互斥。走與 `schedule --live` 相同的 scheduler 路徑。

### 點擊延遲的三個數字

`lateness ms` 是 scheduler 交給 driver 的時刻；driver 若回報 `last_click_metrics`，另外印 `guard ms`（driver 點擊前檢查耗時）與 `down posted ms` / `up posted ms`（mouse-down / mouse-up 交給 OS 的時刻減去 effective target）。三者都**不含手機收到 tap 與網路送達**，輸出文字明說 `EXCLUDES phone and network arrival`。沒回報時印 `n/a`。

### keep-awake

`--live` 期間以 `caffeinate -di -w <pid>` 擋 idle sleep，隨程序結束自動退出，不改任何系統設定；`caffeinate` 不存在時只是不啟用，不影響執行。

### 端到端正式流程（2026-09-22 實際指令）

```sh
python3 -m fans_utils.macos diagnose                       # accessibility_trusted: true、target_windows 非空
python3 -m fans_utils.macos calibrate --in 10              # 切到 Mirroring，游標放在申請按鈕上；只讀不點
python3 -m fans_utils schedule nmixx-uoo7zw9oie --rehearse-in 2          # dry-run，mock driver
open tools/mock-click-target.html                          # 無害目標
python3 -m fans_utils.macos calibrate --in 10 --bundle <瀏覽器 bundle id> --path /tmp/mock-cal.json
python3 -m fans_utils.macos fire --in 5 --bundle <瀏覽器 bundle id> --path /tmp/mock-cal.json   # 恰好一次真實點擊
python3 -m fans_utils.macos calibrate --in 10              # 回到 Mirroring 重新校準申請按鈕
python3 -m fans_utils schedule nmixx-uoo7zw9oie --live --driver macos --calibration calibration.local.json
```

沒有 calibration 時 `--live` 的實際輸出（2026-09-22 16:25 台北）：

```
error: driver 'macos' setup failed: no calibration at /Users/.../fans-utils/calibration.local.json. Run: python3 -m fans_utils.macos calibrate --in 10
```
exit 5，未等待、未點擊。

## 6. 驗收規格

每一條對應至少一個測試或可觀察的 CLI 輸出。標 ✅ 者已在上方輸出中觀察到。

### 時間

- [x] **明確時區換算**：`show` 同時顯示活動時區、台北、UTC。19:00 Asia/Seoul 顯示為 18:00 Asia/Taipei。✅
- [x] **倒數與 preflight**：`show` 顯示 `starts in`；`schedule` 顯示 `waiting` 與完整計畫後才等待。✅
- [x] **有效目標時間**：`effective target = nominal start + adjust`，三者都在 `schedule` 輸出中。✅
- [x] **有效目標前不點擊**：scheduler 最後一段 `while left > 0: sleep(max(left, 1e-6))`，只在 monotonic ≥ deadline 後才點；`tests/test_scheduler.py` 的 undersleep / asymptotic undersleep / wall-clock step 測試覆蓋。✅
- [x] **過期日期拒絕**：effective target 已過去時 `schedule` 在建 driver 之前就 exit 3 並提示 `--rehearse-in`（僅 dry-run）；`test_cli.py` 覆蓋 dry-run 與 live 兩種。✅
- [x] **可調整 offset 並清楚顯示**：`--adjust-ms`，`adjust:` 行永遠顯示。✅

### 點擊

- [x] **座標校準與重播**：`macos calibrate` 存座標與視窗 bounds，`macos check` / `schedule --live` 的 preflight 比對後才點；`test_macos.py` 覆蓋 round-trip 與 bounds 不符拒絕。✅
- [x] **dry-run**：預設模式，mock driver，輸出格式與 live 相同；`test_dry_run_never_constructs_the_real_driver` 保證 dry-run 不建真實 driver。✅
- [x] **有上限的點擊**：`--max-clicks` 預設 1，另有 `--max-duration-ms` 時間上限；間隔不會睡過上限、driver 慢也不補打。✅
- [x] **立即取消**：Ctrl-C → status cancelled、exit 130、`close()` 必呼叫；driver 點擊為同步，無殘留。`test_ctrl_c_*` 三個測試覆蓋等待中、點擊間、live。✅
- [x] **App focus 與座標幾何假設**：README、macos-driver.md 與 `schedule --live` 的 `live:` 提示都明說前景、不動視窗、重新校準的條件。✅
- [x] **mock target 真實點擊 rehearsal**：2026-09-22 16:26:54 台北，`python3 -m fans_utils.macos fire` 對 `tools/mock-click-target.html` 送出一次 mouseDown/mouseUp，頁面 click 計數 0→1，scheduler dispatch 延遲 0.810 ms（不含裝置/網路延遲）。✅
- [x] **Mirroring 視窗真實 rehearsal**：2026-09-22 台北，對 Mirroring 視窗內無害的活動列點一次（global (956, 430)，completed 1，lateness 1.016 ms），手機畫面確實切換並還原，視窗 bounds (782, 139, 348, 766) 未變。Apply 按鈕的 calibration 已以 `calibrate --x 956 --y 841` 存入 `calibration.local.json`，`check --bundle com.apple.ScreenContinuity` 通過，`git check-ignore` 確認不進 git。**Apply 未被點擊，正式 `schedule --live` 尚未啟動。** ✅

### `--guarded-retry` 的真實截圖驗收（coordinator，2026-09-22，Safari + mock 頁）

以下全部是 **Mac / Safari / mock 頁**的證據，**不是** FANS 受理的證據，也量不到手機或網路速度。

| 情境 | 結果 | DOM 實際狀態 | dispatch 延遲 | mouse-down 交給 OS |
|---|---|---|---|---|
| `?ignore=1` | 2 次嘗試後 `screen_changed` | `APPLIED`、計數 1；buffer 記錄先 `IGNORED` 17:32:51.374 再 `ACCEPTED` 17:32:51.541 | +1.038 ms | +23.355 ms |
| `?ignore=0` | 1 次嘗試後 `screen_changed` | `APPLIED`、計數 1 | +1.023 ms | +17.918 ms |
| `?ignore=999` | 3 次嘗試、`completed`（畫面未變） | 計數 0，3 次點擊全部被忽略 | [1.039, 201.556, 392.361] ms | [20.015, 207.800, 397.209] ms |

- 三種情境都在 800 ms 窗內結束，**沒有出現第 4 次點擊**，上限有效。
- `ignore=1` 證明了要驗的那件事：第一次點擊被忽略（畫面沒變）→ 補點 → 第二次被接受 → 畫面變了 → **立刻停止**。
- `ignore=999` 證明畫面一直沒變時，補點次數仍被上限擋住，不會無限點。
- 反向守衛另外實測兩次：把前景切走、以及鎖定到 login window，兩次都**拒絕點擊、0 個事件**。
- `screen_changed` 在這裡只代表「mock 頁的畫面變了」。放到正式情境，它一樣不代表申請被受理。

未完成：鏡像（iPhone Mirroring）視窗上的 guarded 測試因為 Mac 被鎖定而沒跑完。先前 v1 的無害原生測試（返回鍵、開啟活動頁）仍然有效，**Apply 仍未被點擊過**。

### 誠實邊界

- [x] **延遲觀察 vs 真實排名**：工具記錄本地 `lateness ms`，並在輸出中明說無法觀察伺服器端申請順序。✅
- [ ] **不從點擊推斷申請成功**：完成點擊後由使用者到 App 確認。工具可以說「點擊已送出」，不可以說「申請已被接受」。
- [x] **畫面改變不等於申請成功**：`--guarded-retry` 的 `screen_changed` 狀態、exit 6 與輸出訊息都明說可能是 loading、modal、錯誤或無關重繪，一律到 App 確認；`test_screen_change_exits_6_and_never_claims_success` 覆蓋。✅
- [x] **補點有上限且有條件**：最多 5 次、總窗 2000 ms、間隔 ≥100 ms，寫在 `ClickPlan` 驗證裡；只在畫面未變時補點，觀察器出錯一律中止。無 API 呼叫、無 request flood。✅
- [x] **延遲數字分開報告**：dispatch / guard / down posted / up posted 四個數字分開印，並明說不含手機與網路送達。✅
- [ ] **正式申請不是測試**：驗證一律用 mock target。真實裝置上、不會送出申請的 rehearsal（例如點一個無害的畫面元素）是允許的。

### 可擴充性

- [ ] 新增活動 = 新增一個 JSON profile，不修改 `fans_utils/` 程式碼。待第二個 profile 驗證。

## 7. 本次 session 的臨時分工

常設規則見 [AGENTS.md](../AGENTS.md)。本 session 內：

| 路徑 | 撰寫者 |
|---|---|
| `README.md`、`AGENTS.md`、`docs/plan.md`、`.gitignore` | Claude worker（規劃文件） |
| `docs/research-fans-platform.md` | Claude worker（平台研究） |
| `fans_utils/`（`macos.py` 除外）、`profiles/`、`pyproject.toml` | Claude worker（核心實作與 CLI） |
| `fans_utils/macos.py`、`tests/test_macos.py`、`docs/macos-driver.md` | Claude worker（iPhone Mirroring driver，已完成） |
| `fans_utils/visual.py`、`tests/test_visual.py` | Claude native worker（螢幕觀察器 `MacOSVisualGuard`） |
| `fans_utils/scheduler.py`、`cli.py`、`driver.py`、`tests/test_scheduler.py`、`test_cli.py`、`fakes.py`、`tools/mock-click-target.html` | Claude core worker（本次 guarded retry、rehearse、延遲數據） |
| `tests/` | Claude worker（測試） |

## 8. 開放問題

1. **Apply 之後的流程未知**：本工具只點一次 Apply。後續是否還有表單、確認頁、驗證，尚未觀察也未自動化，由使用者當場處理。
2. 本機時鐘與 FANS 伺服器的時間差沒有觀測手段，`--adjust-ms` 是使用者手動補償用；本機實測 dry-run 約 3–5 ms、真實 fire 約 0.8–1.0 ms。
3. 前景判斷已改用 Accessibility（AX）即時讀取；另一個 app 的浮動面板蓋在點擊位置上時仍無法偵測。
4. 點擊階段的失敗不等於「沒點到」：driver 在 `mouseDown` 之後出錯仍會送 `mouseUp` 以免按鍵卡住，可能已產生一次點擊效果。設定階段（exit 2/3/4/5）才保證零事件。使用者要以 App 內狀態為準。
5. （已解決）headless CLI 下 `NSWorkspace.frontmostApplication` 會回傳快取值，driver 已改用 AX 即時讀取前景。最終 guard 驗收：preflight 時前景為 `com.openai.codex`，倒數結束時前景為 `com.apple.Spotlight`，click 正確拒絕、0 次點擊。細節見 [macos-driver.md](macos-driver.md)。
