# macOS click driver（iPhone Mirroring）

`fans_utils/macos.py`。標準函式庫 ctypes 直接呼叫 CoreGraphics / CoreFoundation / AppKit（objc runtime），沒有 helper process、沒有 thread、沒有 queue、沒有第三方套件、每次點擊不啟動任何 shell 或編譯。

> `CGEventPost` 回傳 void。「已 post 一次點擊」只代表事件已交給 HID event tap。**不證明手機收到 tap，也不證明 FANS 伺服器接受申請。**

## 與 core 的整合（已由 core 完成）

`python3 -m fans_utils schedule <profile> --live --driver macos [--calibration PATH]`。core 以 `calibration_path` 建構 driver、在倒數前呼叫 `preflight()`、在 `run()` 後於 `finally` 呼叫 `close()`。本模組不再需要任何額外接線。

## API

```python
MacOSClickDriver(x=None, y=None, *,
                 expected_bundle_id=None,   # None：省略 x/y 時採用 calibration 內的 bundle_id；
                                            #       給了 x/y 時退回 "com.apple.ScreenContinuity"
                 calibration_path=None,     # 預設 <repo>/calibration.local.json；x/y 省略時讀取
                 native=None)               # 測試注入用
```

`expected_bundle_id` 明確給值且與 calibration 內的 bundle 不同時，建構即失敗（`DriverError`），避免用瀏覽器的校準去點 Mirroring。

| 成員 | 行為 |
|---|---|
| `name`, `performs_real_click` | `"macos"`, `True` |
| `preflight() -> list[str]` | 檢查 Accessibility 權限（`AXIsProcessTrusted`，不彈窗、不改設定）、座標有限且在某個 active display 內、目標 app 有 on-screen 視窗、座標落在該視窗內、視窗 bounds 與 calibration 一致；暖機 `CGEventSource`。前景 app 不是目標時**回傳警告**而非失敗，讓使用者在倒數期間把 iPhone Mirroring 帶到前景。失敗丟 `PermissionError` 或 `DriverError`，不點擊。 |
| `click()` | 每次點擊前重新檢查：Accessibility 仍授權、前景 bundle == 目標、目標視窗仍在 on-screen 清單中、**目標點在 layer-0 視窗中最上層的就是目標視窗**（任何 app、包含同 app 的另一個 layer-0 視窗遮住都拒絕；layer 高於 0 的選單、通知橫幅不在清單內，偵測不到，見已知限制）、bounds 未變（容忍 0.5 pt）、座標仍在視窗內。任一失敗丟 `DriverError` / `PermissionError`，**不會點到別的 app**。通過後同步送 `mouseMoved` → `leftMouseDown` → `leftMouseUp`（down/up 帶 `kCGMouseEventClickState=1`；up 在 `finally`，避免按鍵卡住）。同步、無 queue，所以 scheduler 的 Ctrl-C 之後不會有殘留點擊。 |
| `validate_target() -> Window` | `click()` 送事件前的全部 guard，抽出來成為唯讀函式。回傳當下的目標視窗；失敗丟 `DriverError` / `PermissionError`。**不 post 任何事件**，所以 benchmark 與 visual guard 可以任意次呼叫。 |
| `target_window` | preflight 鎖定的視窗（preflight 前為 `None`）。給 `fans_utils.visual` 用，不必碰私有欄位。 |
| `last_click_metrics` | 每次**成功送出**的 `click()` 後設為 `{guard_ms, down_posted_monotonic, up_posted_monotonic}`；guard 拒絕時設回 `None`，不會留下上一次的數字被誤讀。兩個 monotonic 值是各自 `CGEventPost` **回傳後**立刻取的 `time.monotonic()` 秒數。 |
| `close()` | 釋放 event source。可重複呼叫。 |
| `cursor_position()` | 目前游標的 global logical points。 |
| `diagnose()` | 唯讀快照：權限、顯示器、前景 app、目標視窗、游標下的視窗。 |
| `Calibration` / `load_calibration(path)` / `save_calibration(cal, path)` | 欄位：`x`, `y`, `bundle_id`, `window_owner`, `window_number`, `pid`, `window_bounds [x,y,w,h]`, `display_id`, `captured_at`, `version`。`window_number` 與 `pid` 是校準當下的身分，重新連線後可能改變，所以真正的 guard 是 `bundle_id` + `window_bounds`。preflight 不會靜默重抓幾何。 |

## 座標空間

- 所有座標都是 CoreGraphics **global logical points**：原點在主顯示器左上角，y 向下，Retina 縮放已除掉。
- `CGWindowListCopyWindowInfo` 的視窗 bounds 與 `CGEventGetLocation` 的游標位置在同一空間，可以直接比較。
- 第二顯示器可能有負座標或大於主顯示器寬度的座標，原樣儲存。
- 從截圖換算：`global = window_origin + screenshot_px / scale`。例：視窗 (782, 139)、截圖 2x、Apply 矩形 px (44,1358)–(652,1450)，中心 px (348, 1404) → global (782 + 174, 139 + 702) = (956, 841)。
- 不要混用 `NSEvent.mouseLocation` / `NSScreen`（y 軸反向）。
- `diagnose` 的 `scale` 是 `CGDisplayPixelsWide / bounds.width`；在 scaled Retina 模式下可能回報 1.0。這不影響點擊，點擊永遠用 points。

## 使用者流程（正式：iPhone Mirroring）

同一個終端機 app（Terminal / iTerm / Orca…）從頭用到尾，Accessibility 權限是授予該 app，不是 python。

```sh
python3 -m fans_utils.macos diagnose                 # 確認 accessibility_trusted: true、target_windows 非空
python3 -m fans_utils.macos calibrate --in 10        # 10 秒內把游標移到 Mirroring 視窗裡的 Apply 按鈕上，只讀游標，不點
python3 -m fans_utils.macos calibrate --x 956 --y 841   # 或：直接給 global logical point，不倒數、不讀游標、不點；--x/--y 必須成對
python3 -m fans_utils.macos check                    # 只跑 preflight
python3 -m fans_utils.macos benchmark --samples 30   # 唯讀：量 guard 成本，不送任何事件
python3 -m fans_utils.visual watch --seconds 5       # 唯讀：看視覺 guard 在這台機器上的實際噪音
python3 -m fans_utils schedule nmixx-uoo7zw9oie --live --driver macos --calibration calibration.local.json
```

倒數期間把 iPhone Mirroring 視窗帶到前景並保持不動。視窗移動、縮放、手機旋轉、斷線都會讓 click 被拒絕。

## Coordinator 驗證流程（mock，不碰真實報名）

`cli.py` 拒絕 `--rehearse-in` 與 `--live` 併用，所以 mock 驗證走本模組自己的入口：

```sh
open tools/mock-click-target.html                    # 用任一瀏覽器開
python3 -m fans_utils.macos calibrate --in 10 --bundle <瀏覽器 bundle id>   # 游標放在黃色大按鈕上
python3 -m fans_utils.macos fire --in 5              # preflight、倒數 5 秒、恰好一次受保護的真實點擊
```

不知道瀏覽器的 bundle id 時，先不帶 `--bundle` 跑一次 calibrate，錯誤訊息會印出游標下視窗的 bundle id。`fire` 走 `scheduler.run`，與正式 `schedule --live` 同一條路徑。HTML 頁面會顯示 click 計數與毫秒時間戳。

## 2026-09-22 驗收證據

### Coordinator 對 mock HTML 的真實點擊（16:26 台北）

- `calibrate` 以游標模式存到 `/tmp/fans-utils-mock-calibration.json`，global (2022, 400)，bundle `com.stablyai.orca`（Orca 內嵌瀏覽器）。
- `fire --in 2`：`status completed`、`clicks posted 1`、dispatch lateness 0.810 ms。
- 頁面 DOM 顯示恰好一組 mousedown / mouseup / click，計數 1，時間 16:26:54.956。
- 這證明合成事件能送達本機瀏覽器，**不**證明手機或 FANS 伺服器端的任何事。

### Coordinator 對真實 iPhone 的無害 rehearsal（16:31–16:33 台北，AX 修正後）

- `fire --in 4`，校準點 global (815, 242)（手機的「返回」）：啟動時前景是 `com.openai.codex`，倒數中 `open -b com.apple.ScreenContinuity`，click 完成 1 次、dispatch lateness 0.288 ms。截圖顯示手機從活動詳情頁回到 NMIXX EVENTS 列表。**合成事件送達真實 iPhone 已確認。**
- 點擊活動列 global (956, 430) 還原：完成 1 次、lateness 1.016 ms，截圖確認回到正確的活動詳情頁，Apply 在底部，視窗 bounds 仍為 (782, 139, 348, 766)。
- 反向守衛：preflight 前景 Codex → 結束時前景 Spotlight → 正確拒絕，0 次點擊。
- `calibrate --x 956 --y 841` 已寫入 repo 的 `calibration.local.json`（`git check-ignore` 確認被忽略），`check --bundle com.apple.ScreenContinuity` 通過。**Apply 未被點擊，尚未 arm 任何正式報名。**

### Driver worker 的唯讀實測（開發期間，未執行任何 fire）

以下是 driver worker 在實作時的唯讀檢查，與上方 coordinator 的 mock 與真實手機點擊測試是不同的兩組證據：

- `diagnose`：`accessibility_trusted: true`，兩個顯示器（1512×982 與 1920×1080，第二個從 x=1512 起），前景 `com.apple.ScreenContinuity`，目標視窗 number 7929 bounds (782, 139, 348, 766)。
- `calibrate --in 0` 在游標位於另一個 app 上時，正確拒絕並印出 `--bundle com.openai.codex` 提示；未寫檔。
- `check` 對手寫的 scratch calibration（點 (956, 522) 在上述視窗內）通過；bounds 改成 700 時被拒絕（moved or resized）；`--bundle com.apple.Safari` 時被拒絕（calibration 不符）。
- driver worker 本身未執行任何 `fire` 或真實點擊；真實點擊證據全部來自上方 coordinator 的測試。

## 測試

```sh
python3 -m unittest tests.test_macos tests.test_visual -v
```

`tests/test_macos.py` 以 `FakeNative` 注入，涵蓋：非有限座標、螢幕外、負座標的第二顯示器、權限被拒、目標未執行、視窗最小化、點在視窗外、calibration 與實際 bounds 不符、前景錯誤（preflight 警告 / click 拒絕）、視窗消失 / 隱藏 / 移動 / 縮放、mouse-down 後被中斷仍會 release 且不重複點、close 後拒絕點擊、calibrate 寫檔與 round-trip。新增：`last_click_metrics` 的順序與有限性、被拒絕時清成 `None`、backend 提供的時間戳優先、`validate_target()` 不送事件且套用全部 guard、`target_window` 只在 preflight 後出現、benchmark 不送事件並統計拒絕次數、前景讀取失敗在 preflight 是警告但在 click 是拒絕。

`tests/test_visual.py` 以注入的 capture 涵蓋：padded row 處理（用 `width*4` 取代 `bytes_per_row` 會讓兩張相同的畫面被判定成不同）、低於 `PIXEL_DELTA` 的抖動不算變化、ROI 排除頂端時鐘區、**一格大小的 spinner 會觸發但同樣變化低於整體 ROI 門檻**、拍不到 / 空白 / 過期 / 抓錯視窗 / 尺寸改變 / 還沒 arm / 還沒 prepare 一律丟例外、anchor 漂移時 `arm()` 拒絕、`changed()` 每次都重新擷取、close 後拒絕使用、guard 全程不送事件。

macOS 限定的 smoke test 只呼叫唯讀綁定。AX focused-app 讀取在環境層級失敗（`-25212`）時 **skip 而不是 fail**，因為那是環境狀態不是邏輯缺陷，對應的邏輯已由 fake 覆蓋。**沒有任何測試斷言真實 UI 收到點擊或真實畫面變化。**

## 前景 app 的判斷（2026-09-22 修正）

**問題**：第一版用 `NSWorkspace.sharedWorkspace.frontmostApplication`。它靠 main run loop 收到的 workspace 通知更新，而 `python3 -m fans_utils` 沒有 run loop，所以整個程序只會得到第一次呼叫時的值。實測：preflight 看到 Orca，倒數期間 `open -b com.apple.ScreenContinuity` 切到 Mirroring，click 仍讀到 Orca；另一次 preflight 看到 Chrome，使用者 15 秒內切回 Mirroring，click 仍以 Chrome 拒絕。

**修正**：`frontmost_bundle_id()` 改為每次呼叫都透過 Accessibility server 即時讀取：`AXUIElementCreateSystemWide()` → `AXUIElementCopyAttributeValue(kAXFocusedApplication)` → `AXUIElementGetPid()` → `NSRunningApplication.runningApplicationWithProcessIdentifier:` 取 bundle id。AX 權限本來就是必要條件，不增加任何新要求。讀取失敗（例如 AXError -25211 未授權）丟 `DriverError`，click 拒絕，不會退回舊值。

實測發現新程序的**第一次** AX 讀取會回 AXError -25204（CannotComplete），下一次就成功。因此讀取有上限重試（5 次、間隔 50 ms），且 `warm_up()`（preflight 內）會先讀一次，讓 click 時的讀取已暖機。

**驗收（coordinator，同一程序內，修正後）**：preflight 讀到 `com.openai.codex` → 倒數期間 `open -b com.apple.ScreenContinuity` → click 讀到 Mirroring 並完成，手機畫面正常回退；重複的事件列還原測試也通過。反向案例：preflight 讀到 Codex → 結束時前景是 Spotlight → 正確拒絕、0 次點擊。同一程序內讀值隨切換而變，證明不再是快取值。

不做的事：不自動把目標 app 帶到前景、不繞過 guard。NSWorkspace 的讀法已完全移除，程式中不再有任何前景快取。

## Guard 成本量測（2026-09-22，唯讀）

```sh
python3 -m fans_utils.macos benchmark --path calibration.local.json --samples 30   # 不送任何事件
```

`benchmark` 只跑 `validate_target()` 並統計 p50 / p95 / max，輸出明確印出 `events posted: 0`。guard 被拒絕的樣本仍然計入時間，因為前景檢查排在最後，被拒絕之前已經付出完整的讀取成本。

| 項目 | p50 | p95 | max |
|---|---|---|---|
| 完整 guard，前景讀得到（coordinator 實測 30 次） | 0.442 ms | 1.994 ms | 2.865 ms |
| 完整 guard，前景讀不到（driver worker 實測 40 次，含 8 次 AX 失敗） | 0.833 ms | 207.5 ms | 208.4 ms |

**那 207 ms 不是 guard 的成本，是 AX 重試預算。** `frontmost_bundle_id()` 讀取失敗時會重試 `AX_READ_ATTEMPTS = 5` 次、間隔 `AX_READ_RETRY_S = 0.05` s，也就是失敗路徑固定付 200 ms 的 sleep（實測失敗讀取中位數 205.88 ms，成功讀取 0.05 ms）。含意：**正式執行時如果前景讀取失敗，那一次點擊會被延後約 200 ms 然後仍然被拒絕。** 這是刻意的取捨（寧可重試也不要因為一次暫時性 `-25204` 就放棄），但排程時要知道最壞情況的 guard 時間是 200 ms 級而不是 1 ms 級。要縮短就得調小上面兩個常數，那會降低暫時性失敗的容忍度，屬於 core 的排程決策。

分段實測（driver worker，30–100 次）：`accessibility_trusted` 約 0.00 ms、`frontmost_bundle_id` 0.05 ms、`on_screen_windows` 0.54 ms、`CGEventCreateMouseEvent` 0.00 ms（100 次 max 0.01 ms）。

**結論：沒有套用任何最佳化。** guard 主要成本是 `on_screen_windows` 的約 0.5 ms 視窗伺服器查詢，那是正確性必需的遮蔽偵測，不能省。事件預先配置（preallocated events）在量到的數字下沒有意義：建立 event 的成本已經低於量測雜訊。先量再說，量完發現沒有可省的工作，就不加程式碼。

## AX 讀取的順序相依（2026-09-22 實測）

新程序的 system-wide AX 讀取（`AXFocusedApplication`）在**還沒做過任何 window server 呼叫之前**會每次都回 `-25204`（CannotComplete），重試 5 次也一樣。先呼叫一次 `CGWindowListCopyWindowInfo` 之後就正常。`preflight()` 的順序（先列視窗、後 `warm_up()`）剛好滿足這個條件，請勿調換。

另外實測到 `-25212`（NoValue）：某些 app（Electron 系，如 Orca）在前景時，AX 讀不到 focused application。處理方式：

- `preflight()` 把讀取失敗當成**警告**（倒數期間使用者還能切換前景視窗補救），不是致命錯誤，所以 `check` / `benchmark` / `visual watch` 在 Orca 終端機裡仍然可跑。
- `click()` 維持嚴格：讀不到前景就拒絕點擊。**沒有**退回「最上層視窗擁有者」之類的推測來源，寧可不點。

## 視覺變化 guard（`fans_utils/visual.py`）

用途：讓重試迴圈在畫面已經變了之後**停手**。`changed()` 回傳 `True` 代表畫面動了，要交回使用者自己在 App 上確認。**它不代表申請成功、不代表伺服器接受、也不代表手機真的收到 tap。**

### API（core 已確認）

```python
from fans_utils.visual import MacOSVisualGuard

guard = MacOSVisualGuard(driver, capture=None)   # driver 是 preflight 過的 MacOSClickDriver
guard.prepare()   # preflight 階段：檢查 Screen Recording 權限 + 拍下 anchor（唯讀）
guard.arm()       # 第一次點擊前（core 可在 T-100ms 呼叫）：拍 baseline
guard.changed()   # 每次點擊後：重新拍一張跟 baseline 比
guard.close()     # finally
```

`prepare()` 拍下的 **anchor** 會保留。`arm()` 會先跟 anchor 比對，**畫面若已經漂移就丟 `VisualGuardError` 而不是默默把新畫面當成 baseline**——鏡像暫停、手機斷線的覆蓋層、使用者自己切走頁面，都必須在第一次點擊前中止，否則等於盲點。

任何「拍不到 / 空白 / 過期 / 尺寸不符 / 抓錯視窗 / 還沒 arm」都**丟例外，永遠不會回傳「沒變」**。呼叫端要把例外一律當成「停手」。

### 擷取方式

`CGWindowListCreateImage`（stdlib ctypes，不開 subprocess、不編譯、不需要 Swift helper）。2026-09-22 在 macOS 26.4 實測可用：iPhone Mirroring 視窗回傳真實 BGRA 696×1532，每張 12–38 ms，像素複製 3–8 ms，比較 0.85–1.4 ms。這個 API 已被 Apple 標記 deprecated，所以 `prepare()` 在拿到 NULL 時會明確失敗而不是安靜跳過。

需要**螢幕錄製**權限（授予終端機 app，不是 python）。本模組只用 `CGPreflightScreenCaptureAccess` 檢查，**不會跳出授權視窗、不會改設定**。

### ROI 與門檻（都是實測值）

- ROI = 視窗頂端往下 `ROI_TOP_PT = 90` logical points 以下到底部。跳過 Mac 標題列與手機狀態列／時鐘（時鐘每分鐘會變，不排除掉會誤判），Apply 在底部所以包含在內。
- 視窗擷取**不含滑鼠游標**，也**不反映遮蔽**（遮蔽是 click driver 的 guard，不是這裡的）。
- 比較取綠色通道、抽樣約 20000 點，只有 `|Δ| > PIXEL_DELTA = 24` 才算一個變化點。鏡像是即時視訊串流，位元層級的抖動本來就存在，**單純做量化分桶不夠**（±1 跨過桶邊界一樣會翻轉），所以用的是振幅門檻。
- 判定是**逐 tile**，不是整個 ROI：ROI 切成 `TILE_ROWS × TILE_COLS = 12 × 4`，**任何一個 tile** 的變化比例達到 `TILE_CHANGE_FRACTION = 0.03` 就算變了。整體門檻會漏掉小型 spinner 或 Apply→Cancel 這種字樣變化，因為它們佔整個視窗不到 1%；但在自己那一格裡佔比夠大。
- 門檻怎麼定的（實際幾何換算）：擷取 696×1532、抽樣間隔 6 px，一個 tile 約 522 個樣本。0.03 需要約 16 個樣本改變，相當於 **24×24 px（12×12 pt）的色塊**。刻意訂在 20 pt iOS 轉圈指示器（40 px ≈ 44 樣本 ≈ 一個 tile 的 8.5%）**之下**：若用 0.08，那種 spinner 剛好卡在門檻上，而且跨到兩個 tile 邊界時每半邊各自計算就會直接漏掉。
- **實測噪音底線：0.00000。** 在確認靜止的鏡像頁面上，連續 11 對擷取（約 30 萬次樣本比較）沒有任何一個樣本超過 `PIXEL_DELTA`；改用 0.03 門檻後再測 12 次，最差 tile 比例仍然全部是 0.0000、`changed` 全部是 `False`。另外兩個靜態非視訊視窗（CotEditor、Dia）各 8 次取樣也全是 0.00000。門檻離噪音底線很遠，收緊不會付出誤報代價。
- 量測過程中一度看到連續 30–80% 的變化，追查後確認是**當時有人在操作手機**，不是頁面本身的動畫；停止操作後重測即為 0.00000。早期一份「66% 位元組相同、最大差 40」的直方圖就是在那段受外部操作影響的時間取的，**不是靜止值**，不作為門檻依據。

### 新鮮度

每張 frame 在擷取呼叫回傳後立刻蓋上 `time.monotonic()`，`changed()` 拒絕超過 `STALE_MS = 250` ms 的 frame。這只證明 **Mac 端的擷取是新的**；**不證明手機串流是活的**——鏡像凍結時，對一張靜止畫面所做的擷取一樣是「新鮮」的。這是設計上的限制，不是可以靠時間戳解決的問題。

### 唯讀觀察工具

```sh
python3 -m fans_utils.visual watch --path calibration.local.json --seconds 5 --interval 0.25
```

印出每次取樣的最差 tile 與變化比例、擷取與比較耗時。**不送任何事件**，可以用來確認門檻在目前這台機器上的實際噪音，或確認某個畫面變化真的會被偵測到。

### 這個 guard 做不到的事

- 不做 OCR、不判斷語意。「畫面變了」不等於「申請成功」。
- 頁面若本身就有持續動畫，`arm()` 的 anchor 漂移檢查會在第一次點擊前中止。這是刻意的取捨（寧可中止也不要盲點），但要知道有這個行為。
- **`prepare()` 的呼叫時機是 core 要決定的事，而且有陷阱**：`prepare()` 拍下 anchor 之後，任何手機端的畫面切換都會讓 `arm()` 判定漂移而中止。若 `prepare()` 在 `schedule` 一啟動就跑，而使用者接著才把 Mirroring 帶到前景、才導覽到 Apply 畫面，那 `arm()` 一定會中止。`prepare()` 必須在**手機已經停在 Apply 畫面之後**才呼叫，或是在那個時間點重跑一次。
- 幾何層級的 guard 偵測不到「視窗還在、但顯示的是連線中斷覆蓋層」這種情況；不要宣稱偵測得到。
- 不會自動確認任何 modal，不會點任何東西。

## 已知限制

- 遮蔽偵測依據 window server 的 layer-0 on-screen 視窗清單。layer 高於 0 的 overlay（選單、系統通知橫幅）不在清單內，無法偵測。
- 本機時鐘與伺服器的差異不在本模組範圍。
- `calibrate` / `fire` 的 `--in` 上限 24 小時；正式活動用 `schedule`。
- 手機端把 Mac 合成點擊當成觸控：**目前這台 iPhone + 這台 Mac 的 Mirroring 設定已於 2026-09-22 在非報名畫面上確認通過**（見驗收證據）。換手機、換 Mac、換螢幕配置或 macOS/iOS 升級後，要重新跑一次無害 rehearsal 再上正式。
- 本工具只負責在指定時刻點一次 Apply；Apply 之後的表單、確認頁或其他步驟尚未觀察、未自動化，由使用者在 App 上處理。
