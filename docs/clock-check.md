# Clock check：本機時鐘偏移量測與 `--adjust-ms` 建議

`python3 -m fans_utils.clockcheck` 用 macOS 內建的 `sntp` **唯讀**量測這台 Mac 的時鐘和公開 NTP 伺服器差多少，並換算成一個保守的 `fans-utils schedule --adjust-ms` 建議值。

> **範圍限制：量測的是「Mac 系統時鐘 vs 公開 NTP 來源」。** 不是 FANS 伺服器的時鐘，也不含 iPhone Mirroring 的輸入延遲或行動網路 RTT。那些都無法從這裡觀察。文字與 JSON 輸出都會印出這段範圍聲明。
>
> **用前重跑。** 讀數只在接下來幾分鐘內有意義，時鐘會漂移、網路狀況會變；幾小時前的結果不是可靠依據。

## 用法

```sh
python3 -m fans_utils.clockcheck                 # 預設 3 個 host，各 1 次 = 3 個請求
python3 -m fans_utils.clockcheck --samples 2     # 各 2 次 = 6 個請求（上限）
python3 -m fans_utils.clockcheck --host time.google.com --timeout 3 --json
```

| 參數 | 預設 | 範圍 |
|---|---|---|
| `--host`（可重複） | `time.apple.com`、`time.cloudflare.com`、`time.google.com` | host × samples ≤ 6 個請求 |
| `--samples` | 1 | 1..2（每個 host） |
| `--timeout` | 2 | 1..5 秒 |
| `--json` | 關 | 輸出完整 report（JSON） |

Exit code：`0` 有建議值、`1` 無建議（inconclusive / no_data）、`2` 參數錯誤。

### 安全保證

- 唯一會執行的指令形狀是 `sntp -t <timeout> -n 1 <host>`。**絕不**傳 `-s`、`-S` 或任何會 `adjtime` / `clock_settime` 的旗標，測試 `tests/test_clockcheck.py::ArgvSafetyTests` 檢查每一次呼叫的 argv。
- 以 `-` 開頭或含空白的 host 直接拒絕，避免被當成旗標。
- 不讀寫任何設定檔、不修改 event profile、不排程、不點擊。

## 輸出範例（2026-09-22 16:57 台北實測）

```
measures:         Mac system clock vs public NTP sources
does NOT measure: FANS server clock; iPhone Mirroring input latency; cellular/network RTT
captured (UTC):   2026-09-22T08:57:34+00:00
status:           consensus
samples ok/total: 3/3
  time.apple.com         +87.4 ms +/- 54.4  -> [+33.0, +141.9]  (17.253.114.35)
  time.cloudflare.com    +93.8 ms +/- 71.3  -> [+22.5, +165.0]  (162.159.200.1)
  time.google.com        +88.8 ms +/- 9.0  -> [+79.8, +97.8]  (216.239.35.12)
interval (ms):    [+79.8, +97.8]  (offset = remote - local; positive = this Mac is behind the measured NTP source time)
offset_ms:        +88.8
suggested:        --adjust-ms -74   (= ceil(-79.8 + 5); never fires early within the interval)
note: Scope: this measures the Mac system clock against public NTP sources only. ...
note: Rerun this check shortly before use ...
```

Coordinator 另一次獨立實測為 `[+77.850, +102.272] ms` → `--adjust-ms -72`，同一套算法。

## 算法

### 1. 符號

沿用 `sntp` 的定義：`offset = remote − local`。**正值 = 這台 Mac 的時鐘落後於量測到的 NTP 來源時間**（Mac 顯示 18:00:00.000 時，NTP 來源已是 18:00:00.088）。注意基準是那幾台 NTP 伺服器，不是 FANS 的時鐘。

### 2. 每個樣本 → 一個區間

`sntp` 每行輸出 `<offset> +/- <uncertainty> <host> <ip>`，轉成毫秒區間 `[offset − u, offset + u]`。

以下樣本視為失敗（計入 `failures`，附原因，不進入計算）：非零 exit、timeout、沒有 `+/-` 行、非數字、NaN / inf、負的 uncertainty、uncertainty ≥ 1 s（對毫秒級調校無意義）、**|offset| > 60 s**、`sntp` 不存在。

offset 上界有兩個理由：時鐘差到一分鐘以上是系統設定問題，不該用毫秒級的 `--adjust-ms` 遮掩；而且像 `1e308` 這種有限但荒謬的值乘以 1000 會溢位成 `inf`，讓建議值計算 `OverflowError`。加上界之後，所有被接受的樣本其毫秒運算必定有限，`intersect` 另外再擋一次非有限邊界。

### 3. 共識區間 = 所有成功樣本區間的交集

```
lo = max(所有區間下界)
hi = min(所有區間上界)
```

- `lo ≤ hi` 且 ≥ 2 個不同 host 成功 → `status = consensus`
- `lo ≤ hi` 但只有 1 個 host 成功 → `status = single-source`（照樣給建議，但明確標示不是共識）
- `lo > hi`（來源互相矛盾）→ `status = inconclusive`，**不給建議**
- 全部失敗 → `status = no_data`，不給建議

`offset_ms` 是交集的中點，只供參考；建議值只看下界。

### 4. 建議值：永不提早

scheduler 在**本機**時間 `nominal + adjust` 觸發，對應的**真實**時間是 `nominal + adjust + offset`。要對區間內每個可能的 offset 都不早於 nominal：

```
adjust + offset ≥ 0  for all offset ∈ [lo, hi]
⟹ adjust ≥ −lo
suggested_adjust_ms = ceil(−lo + 5)      # 加 5 ms 邊際，往「較晚」的整數取
```

範例：`lo = +75.38 ms` → `ceil(−75.38 + 5) = ceil(−70.38) = −70`。上面實測 `lo = +79.8` → `−74`。

如果區間包含 0 或下界為負（例如 `[−20, +10]`），公式自然得到正值（`ceil(20 + 5) = 25`），也就是**比名目時間晚**觸發，寧可慢也不搶跑。這是刻意的：提早觸發等於在報名開放前點擊，一定無效。

### 5. 這個數字代表什麼、不代表什麼

| 有補償 | 沒有補償 |
|---|---|
| Mac 系統時鐘相對 NTP 的偏移 | FANS 伺服器自己的時鐘 |
| | iPhone Mirroring 的輸入延遲 |
| | App → 伺服器的網路 RTT |
| | 報名按鈕實際出現的時間 |

所以 `--adjust-ms -74` 的意思只有：「讓 Mac 在**量測到的 NTP 來源時間** ≥ 18:00:00.000 時才觸發」。它不是「在 FANS 認定的 18:00 觸發」，更不是「保證第一個送達」。

## 程式 API（供之後 core 整合）

```python
from fans_utils.clockcheck import measure, Report

r: Report = measure(hosts=DEFAULT_HOSTS, samples=1, timeout_s=2, runner=subprocess.run)
```

| 欄位 | 型別 | 說明 |
|---|---|---|
| `captured_at_utc` | str | ISO 8601 UTC 量測時間 |
| `hosts` | list[str] | 去重後的 host 順序 |
| `samples` | list[Sample] | 每次請求：`host, ok, offset_s, uncertainty_s, server, raw, error` |
| `failures` | int | 失敗樣本數 |
| `status` | str | `consensus` / `single-source` / `inconclusive` / `no_data` |
| `interval_ms` | (lo, hi) 或 None | 共識區間（ms） |
| `offset_ms` | float 或 None | 區間中點 |
| `suggested_adjust_ms` | int 或 None | `ceil(−lo + margin_ms)` |
| `margin_ms` | float | 5.0 |
| `sign_convention` | str | 符號說明（基準是量測到的 NTP 來源時間） |
| `measures` | str | `Mac system clock vs public NTP sources` |
| `does_not_measure` | str | `FANS server clock; iPhone Mirroring input latency; cellular/network RTT` |
| `scope` | str | 完整範圍聲明，JSON 與文字輸出都有 |
| `note` | str | 「用前重跑」提醒 |
| `reasons` | list[str] | 失敗原因與狀態說明 |

`measure` 對網路問題不會 raise（變成失敗樣本）；參數錯誤 raise `ClockCheckError`。`runner` 可注入以便測試，`Report.to_dict()` 給 JSON。

## 測試

```sh
python3 -m unittest tests.test_clockcheck -v
```

25 個 case，涵蓋：實際 macOS 輸出解析、正負號、壞輸入（NaN / inf / 負 uncertainty / 無上界 / 亂碼 / `1e308` 這類有限但荒謬的 offset / 剛好超過 60 s）、被接受的樣本必定產生有限的毫秒區間與 `int` 建議值、`intersect` 擋非有限邊界、交集與矛盾、timeout 與非零 exit、`sntp` 不存在、請求上限、argv 永不含 `-s` / `-S`、建議值對區間內所有 offset 都不提早、文字與 JSON 都含範圍聲明且不出現 "true time"、CLI 的 JSON 輸出與 exit code。全部用 fake runner，不打網路。
