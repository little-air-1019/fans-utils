# fans-utils

個人使用的 FANS 平台活動小工具集。第一個工具是定時點擊器（timed clicker）。

> **狀態（2026-09-22）：** core 與 iPhone Mirroring 的 macOS driver 已整合並通過驗收（mock target 與真實 Mirroring 視窗的無害點擊 rehearsal、切走 app 時拒絕點擊的 guard、wheel 安裝）。本次新增選用的 [`--guarded-retry`](#--guarded-retry有條件的補點選用)、`rehearse` 子命令與延遲分項數據，233 個 unittest 全部通過。Apply 按鈕的 calibration 已存好，**尚未點擊 Apply，也尚未啟動正式 `schedule --live`**。驗收細節見 [docs/macos-driver.md](docs/macos-driver.md)。
>
> **範圍限制：** 本工具預設在指定時刻點一次 Apply；另有選用的 [`--guarded-retry`](#--guarded-retry有條件的補點選用)，在畫面沒有變化時補點，次數與時間都有上限。點擊之後的表單、確認頁、任何後續步驟都尚未觀察過、也沒有自動化，由使用者自己在 App 上處理。本工具不保證能搶到名額；伺服器端的申請順序由平台決定，點擊送出不代表申請成功，結果請到 App 確認。
>
> **本工具量得到什麼、量不到什麼：** 可以量的是本機事件送出的時刻（scheduler dispatch、mouse-down/up 交給 OS 的時間），以及 [`clockcheck`](#檢查本機時鐘clockcheck) 量到的**本機時鐘對公開 NTP 來源**的偏移。量不到的是 FANS 伺服器時鐘、iPhone 收到 tap 的時間、Mirroring 的輸入延遲，以及手機的行動網路往返時間。Mac 的網路狀況不能拿來推論手機的網路狀況。

## 第一個參考活動

NMIXX 官方活動：https://app.fans/community/nmixx/event/uoo7zw9oie

- 報名時段：2026-09-22 19:00–19:10 Asia/Seoul（= 18:00–18:10 Asia/Taipei）
- 活動日：2026-10-06
- 100 名正取 + 10 名候補，依申請順序給號
- 目前活動頁的瀏覽器 CTA 導向 App，因此點擊落在 Mac 上的 iPhone Mirroring 視窗

## 需求

- macOS（driver 用 ctypes 呼叫 CoreGraphics / AppKit）、Python 3.11+、無第三方依賴
- iPhone Mirroring 已連線並顯示活動頁
- 終端機 app（Terminal / iTerm / Orca…）已獲 **系統設定 › 隱私權與安全性 › 輔助使用** 權限。權限是給終端機 app，不是給 python；從頭到尾用同一個終端機 app。

## 端到端流程（在 repo 根目錄執行）

```sh
# 0. 看活動時間（活動時區 / 台北 / UTC）與倒數
python3 -m fans_utils show nmixx-uoo7zw9oie

# 0b. 唯讀檢查本機時鐘對公開 NTP 的偏移（要用之前幾分鐘內再跑一次，見下節）
python3 -m fans_utils.clockcheck

# 1. 唯讀診斷：accessibility_trusted 要是 true，target_windows 要非空
python3 -m fans_utils.macos diagnose

# 2. 校準：10 秒內切到 iPhone Mirroring，把游標放在「申請」按鈕上不要動。只讀游標位置，不點擊。
#    結果寫入 <repo>/calibration.local.json（不進 git）。已知座標可用 --x/--y 直接寫入，跳過倒數。
python3 -m fans_utils.macos calibrate --in 10
python3 -m fans_utils.macos check          # 只跑 preflight，確認座標仍在 Mirroring 視窗且 bounds 未變

# 3. dry-run rehearsal：mock driver，不碰任何裝置，確認倒數、adjust、輸出格式
python3 -m fans_utils schedule nmixx-uoo7zw9oie --rehearse-in 2

# 4. 無害目標的真實點擊 rehearsal：用瀏覽器開 tools/mock-click-target.html，對它校準後 fire 一次
#    （或在 Mirroring 視窗的「非報名」畫面上校準一個無害元素）。fire 走與正式相同的 scheduler 路徑。
open tools/mock-click-target.html
python3 -m fans_utils.macos calibrate --in 10 --bundle <瀏覽器 bundle id> --path /tmp/mock-cal.json
python3 -m fans_utils.macos fire --in 5 --bundle <瀏覽器 bundle id> --path /tmp/mock-cal.json

# 5. 正式：重新對 Mirroring 的申請按鈕校準（步驟 2），preflight 通過後等到 18:00:00 點一次
python3 -m fans_utils schedule nmixx-uoo7zw9oie --live --driver macos --calibration calibration.local.json
```

`schedule --live` 的順序是：驗證 profile 與參數 → 目標已過期就直接拒絕（exit 3）→ 建立 driver（讀 calibration）→ preflight（權限、顯示器、視窗位置與 bounds）→ 倒數 → 點擊 → 無論成功、失敗或 Ctrl-C 都 `close()`。

失敗要分兩種看：

- **設定階段失敗**（exit 2、3、4、5：參數、過期、driver 載入、calibration、preflight）：發生在倒數之前，**保證沒有送出任何滑鼠事件**。
- **點擊階段失敗**（status `error`，exit 1；或倒數後 Ctrl-C，exit 130）：driver 在每次 `click()` 前重新檢查前景與視窗，檢查不過就拒絕、不送事件。但若 `mouseDown` 已送出後才出錯，driver 仍會送 `mouseUp` 以免按鍵卡住，這時**可能已經產生一次點擊效果**。看 `clicks attempted` 與 status 訊息判斷，並到 App 確認實際狀態。

### 倒數期間要做的事

- 把 iPhone Mirroring 視窗帶到**前景**並保持在校準時的位置與大小。preflight 時前景不對只會警告，讓你有時間切回去。點擊當下 driver 會重新檢查前景 bundle、視窗是否還在 on-screen 清單、bounds 有沒有變、座標是否仍在視窗內，任一項不符就**拒絕自己**而不是點到別的 app（已實測：切走前景與鎖定畫面各一次，都是 0 個事件）。
  但這些檢查**不等於能偵測所有異常**：手機斷線是否被擋，取決於視窗在 window server 清單中的狀態；layer 高於 0 的選單與通知橫幅也偵測不到（見 [docs/macos-driver.md](docs/macos-driver.md) 的已知限制）。畫面看起來對，不代表手機端狀態一定對。
- 不要移動滑鼠、不要切換 Space。`--live` 期間工具會自己開一個 `caffeinate -di -w <pid>` 擋掉 idle sleep，
  程序結束就跟著結束（輸出會有 `keep-awake: on`）；這不擋闔蓋，也不改任何系統設定。
- Ctrl-C 隨時停止；停止後不會有殘留點擊。
- 校準座標只在當時的視窗位置、大小、螢幕配置下有效。動過就重跑 `calibrate`。

### 輸出怎麼讀

- `lateness ms` 是 scheduler 把 click 交給 driver 的時刻減去 effective target，本機實測 dry-run 約 3–5 ms、mock target 真實 `fire` 為 0.810 ms。這不是手機或伺服器收到的時間，裝置與網路延遲不在其中。
- `guard ms` / `down posted ms` / `up posted ms` 只有在 driver 有回報 `last_click_metrics` 時才出現，是三個不同的東西：
  `guard ms` 是 driver 點擊前檢查（前景、視窗、遮蔽）花的時間；`down posted ms` / `up posted ms` 是 mouse-down /
  mouse-up **交給作業系統**的時刻減去 effective target，已經包含 guard 時間。三者都**不含手機收到 tap 與網路送達**的時間，
  那段本工具觀察不到。沒有回報時印 `n/a`。
- `baseline age ms` 只有 `--guarded-retry` 會出現：第一次點擊送出時，螢幕 baseline 已經多舊。
- `screen checks` 是拿去跟 baseline 比對的新截圖張數。
- `clicks attempted` 只是本地已送出（posted）的合成滑鼠事件數，送達與否無法驗證。手機是否當成 tap、FANS 是否受理、排名多少，工具都觀察不到。
- `schedule --live --driver macos` 固定要求 calibration 的目標 app 是 iPhone Mirroring（`com.apple.ScreenContinuity`）；對瀏覽器 mock target 做的校準會被拒絕，不會被誤當成正式目標。mock rehearsal 請走 `python3 -m fans_utils.macos fire --bundle ...`。
- `--adjust-ms` 負值提早、正值延後，是使用者對本機延遲與時鐘差的手動補償。

## 指令與參數

```sh
python3 -m fans_utils list
python3 -m fans_utils show <profile-id | path.json>
python3 -m fans_utils schedule <profile> [--live --driver macos --calibration PATH]
                                         [--adjust-ms N] [--max-clicks N] [--interval-ms N] [--max-duration-ms N]
                                         [--guarded-retry]
                                         [--rehearse-in SECONDS]        # 僅 dry-run
python3 -m fans_utils rehearse (--in SECONDS | --at ISO) --calibration PATH [--bundle ID] [--guarded-retry]
python3 -m fans_utils.macos diagnose | calibrate --in N | check | fire --in N
```

| `schedule` 參數 | 預設 | 說明 |
|---|---|---|
| `--live` | 關 | 使用真實 driver；預設是 mock dry-run |
| `--driver` | `macos` | `--live` 用的 driver，延遲載入，非 macOS 上 import 也不會壞 |
| `--calibration PATH` | driver 預設 `<repo>/calibration.local.json` | 僅 `--live` |
| `--adjust-ms` | 0 | 目標時間位移，負值提早 |
| `--max-clicks` | 1（`--guarded-retry` 時 3） | 點擊上限 |
| `--interval-ms` | 250（`--guarded-retry` 時 150） | 點擊間隔（每次點擊後的最小間隔，不補打） |
| `--max-duration-ms` | 5000（`--guarded-retry` 時 800） | 有效目標後多久停止 |
| `--guarded-retry` | 關 | 見下節。預設完全不啟用，行為與先前版本一模一樣 |
| `--rehearse-in SECONDS` | 無 | 僅 dry-run，忽略 profile 起始時間；不能與 `--live` 併用 |

Exit code：0 完成 · 1 profile/參數錯誤或點擊時 driver 拒絕 · 2 用法錯誤 · 3 目標已過期 · 4 driver 不存在或無法載入 · 5 driver 建立（calibration）、preflight 或螢幕觀察器建立失敗 · 6 `--guarded-retry` 因畫面改變而停止（**不代表申請成功**）· 130 Ctrl-C。

## `--guarded-retry`：有條件的補點（選用）

單次點擊有一個觀察不到的失敗模式：按鈕還沒 enable 時點下去，事件送出了但什麼也沒發生。`--guarded-retry`
針對這種情況補點，而且只在「畫面沒變」時才補：

1. 在有效目標前約 100 ms 對目標視窗拍一張 baseline。拍照成本由等待時間吸收，所以**在本機實測中沒有觀察到第一次點擊被延後**
   （5 次取樣：單次 0.79–5.01 ms、guarded 0.75–5.04 ms，兩者分布重疊）。這是本機量測，不是保證。
2. 點一次，等 interval，再拍一張跟 baseline 比。
3. 畫面沒變 → 在上限內再點一次；畫面變了 → **立刻停止**，status `screen_changed`、exit 6。

上限（寫死在 `ClickPlan`，不能超過）：最多 5 次、總窗 2000 ms、間隔至少 100 ms。預設值是 3 次 / 150 ms / 800 ms。

**必須知道的限制與誠實邊界：**

- `screen_changed` **不是成功**。畫面改變可能是 loading、modal、錯誤訊息，或跟點擊無關的重繪。一律到 App 確認。
- baseline 是在第一次點擊**之前**拍的。如果 Apply 按鈕正好在目標時刻才 enable，那個變化會被算在第一次點擊頭上，
  執行結果就是點一次後停止。這是刻意保守的設計，不是故障。
- 畫面沒變也**不代表**點擊失敗或成功，只代表看不到變化。
- 截圖失敗、權限不足、視窗幾何不符一律中止，不再補點（status `error`），絕不把失敗當成「沒變化」繼續點。
- 這不是 request flood：只點使用者自己的 active UI，次數有上限，沒有任何 API 呼叫。

## 檢查本機時鐘（clockcheck）

```sh
python3 -m fans_utils.clockcheck                       # 預設幾個公開 NTP 來源
python3 -m fans_utils.clockcheck --host time.google.com --json
```

唯讀，透過 macOS 內建的 `sntp` 比對，不會改系統時間、不會改任何設定。輸出會給一個偏移區間與建議的
`--adjust-ms`（取區間邊界再留安全邊際，所以不會讓點擊早於有效目標）。

**這個數字的邊界請務必看清楚：**

- 量的是**本機 Mac 時鐘 vs 公開 NTP 來源**，就這樣而已。
- **不是** FANS 伺服器時鐘。伺服器跟公開 NTP 之間的差異本工具看不到，也無從推論。
- **不含** iPhone 收到 tap 的時間、Mirroring 輸入延遲、手機行動網路的往返時間。Mac 接公司 Wi-Fi、手機走行動網路，
  兩者的網路狀況不一樣，**不能**拿 Mac 的量測推論手機。
- 偏移會漂移，網路狀況也會變。**要用之前幾分鐘內重跑**，不要沿用幾小時前的數字。
- 它是本機時鐘的修正參考，不是端到端的時間保證。

## `rehearse`：無害的真實點擊演練

`schedule --rehearse-in` 只能 dry-run。要對**自己校準的無害目標**做真實點擊演練，用 `rehearse`：

```sh
open tools/mock-click-target.html
python3 -m fans_utils.macos calibrate --in 10 --bundle <瀏覽器 bundle id> --path /tmp/mock-cal.json
python3 -m fans_utils rehearse --in 5 --calibration /tmp/mock-cal.json --bundle <瀏覽器 bundle id>
python3 -m fans_utils rehearse --at 2026-09-22T18:00:00+08:00 --calibration /tmp/mock-cal.json --guarded-retry
```

- `--calibration` **必填、沒有預設值**，所以不可能不小心用到正式的 Apply 校準。
- `rehearse` 不讀任何 profile，也**不套用**正式模式的 iPhone Mirroring 目標鎖定，`--bundle` 由使用者自己指定。
- `--at` 收一個帶 UTC offset 的 ISO 時刻，方便跟 mock 頁的 `readyAt` 對齊；`--in` 與 `--at` 互斥。
- 走的是與 `schedule --live` 完全相同的 scheduler 路徑，所以延遲數據可以互相參照。

### mock 頁的 query parameter

`tools/mock-click-target.html` 不發任何網路請求，可以模擬三種情況（可組合）：

| 參數 | 行為 |
|---|---|
| `?ignore=N` | 前 N 次點擊完全沒反應（不計數、不變色），模擬按鈕還沒 enable |
| `?readyAt=T` | T 之前的點擊同樣完全沒反應。T 可以是 epoch 毫秒或 ISO 時刻，可與 `rehearse --at` 對齊 |
| `?change=0` | 點擊計數但按鈕永遠不變色，用來驗證「畫面沒變 → 在上限內重複點」 |

例：`?ignore=1` 驗證「第一次被忽略、第二次才被接受、接受後立刻停止」。

## 新增活動：加一個 profile，不改程式

一個活動 = `profiles/<id>.json` 一個檔案。`list` / `show` / `schedule <id>` 直接以檔名（去掉 `.json`）當 id，也可以給任意路徑。欄位全部必填（`notes` 除外），多餘欄位會被拒絕：

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

驗證規則：`url` 必須是 https；`timezone` 必須是合法的 IANA 名稱；`signup_start` / `signup_end` 必須帶 UTC offset 且與 `timezone` 一致；`signup_end` 必須晚於 `signup_start`；`capacity` / `alternates` 是非負整數；不接受 `NaN` / `Infinity`。profile 只描述活動事實，執行參數（點擊數、offset、間隔）是 CLI 參數。換一個目標按鈕就重新 `calibrate`，換活動不需要改任何程式。

## 安裝方式與 profile 來源

- **從 repo 執行**（主要路徑）：`python3 -m fans_utils ...`，預設讀 `<repo>/profiles/`，與 cwd 無關。
- **wheel 安裝**：`python3 -m pip wheel --no-deps --wheel-dir /tmp/fans-utils-wheels .`，安裝後 `python3 -m fans_utils list` / `show` 一樣能找到 profile，因為 `profiles/` 以 package-data 打進 `fans_utils.profiles`（單一來源）。已於 2026-09-22 由 coordinator 實際建 wheel、安裝到獨立目錄並從 `/tmp` 執行驗證。
- `--profiles-dir DIR` 可覆寫上述兩者。

## 測試

```sh
python3 -m unittest discover -t . -s tests -v   # 全部（core + macos driver 與螢幕觀察器以 fake 注入），不會點擊任何 UI
```

`-t .` 不能省略：測試之間有 package 內的相對 import，少了它 `tests.test_visual` 會以 ImportError 收場。

## 原則

- Mac-local，只操作使用者自己的畫面；不做 request flood、API replay、auth bypass
- 正式申請不是測試，驗證一律用 mock target 或無害元素
- 不從點擊推斷申請被接受；工具不記錄任何個人資料

## 文件

- [docs/plan.md](docs/plan.md)：結構、指令輸出、驗收規格、開放問題
- [docs/macos-driver.md](docs/macos-driver.md)：driver 的 API、座標空間、守衛條件
- [AGENTS.md](AGENTS.md)：分工與規則
- `docs/research-fans-platform.md`：平台研究
