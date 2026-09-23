# h2hdb-komga

`h2hdb-komga` 將 H2HDB 已發佈書庫的作品資料同步到 Komga，讓你在 Komga
中看到來源標題、簡介、日期與標籤。它是一個執行一次便結束的命令列工具，
適合在 ingest 更新書庫後執行，也可以加入你的排程。

本工具更新 Komga 的作品資料，不下載漫畫、不複製 CBZ，也不建立 H2HDB 資料庫。
開始前需要已有 [H2HDB](https://github.com/Kuan-Lun/h2hdb)、
[ingest](https://github.com/Kuan-Lun/h2hdb-ingest) 發佈的書庫，以及正在運作的 Komga。

## 同步哪些內容

| H2HDB 來源資料 | Komga 中的結果 |
| --- | --- |
| 原始作品標題 | 作品標題；來源標題空白時保留 Komga 現有標題 |
| 簡介 | 作品簡介 |
| 來源上傳日期 | 發行日期 |
| 非空白的來源標籤 | 作者清單：標籤值作為名稱、命名空間作為角色 |
| GID | 作者清單中角色為 `gid` 的項目 |

例如 `artist:alice` 會成為名稱 `alice`、角色 `artist` 的作者項目。
標籤不會寫入 Komga 的 `tags` 欄位，來源上傳帳號也不會作為作者匯入。
同步會以 H2HDB 資料更新上述欄位，手動編輯同一欄位後再次同步可能被覆寫。
閱讀進度由 Komga 管理。

## 準備書庫

需要 Python 3.14 以上版本，以及支援 POSIX 檔案鎖的環境，例如 Linux 或 macOS。
此版本使用 `h2hdb>=0.41.1,<0.42.0`，對應 epoch 3／schema version 8。
H2HDB 資料庫和 ingest 發佈的 CBZ 必須屬於同一個書庫。
舊 schema 7 必須先停止所有 consumers，使用 Core 的一次性離線工具升到 schema 8；
既有資料庫內容、CBZ 與 Komga 閱讀進度可保留。本工具不會執行資料庫轉換，
也不接受尚未完成轉換的 `BUILDING` 狀態。

1. 先由 H2HDB 與 ingest 完成資料庫初始化和書庫發佈。
2. 在 Komga 建立專用書庫，讓它只讀取 ingest 的 `current/acquisitions` 目錄，
   並將這些作品設定為 **One-Shot**。每本 CBZ 必須各自是一個單冊系列。
3. 關閉此 Komga 書庫的 **Scan on startup** 與 **Scan interval**。
   後續由本工具觸發掃描，不要另外從 Komga 介面或其他排程觸發掃描。
4. 準備可掃描書庫及修改作品資料的 Komga 帳號、目標書庫 ID，以及同步程式
   可讀取的 H2HDB 資料庫設定。
5. 依 ingest 安裝說明準備同一書庫的 `.h2hdb-coordination` 目錄，
   讓同步程式可讀取；ingest 完成初始化後，目錄內必須已有 `publication.lock`。

檔名必須是 ingest 產生的 `h2h-<gid>.cbz`，例如 `h2h-12345.cbz`；
Komga 顯示名稱可以省略 `.cbz`。不要手動改成純數字、帶標題或雜湊的檔名。
目標 Komga 書庫必須完整對應已發佈的 H2HDB 書庫，不能混入其他作品。

### 容器掛載範例

假設 ingest 的書庫位於主機 `/volume1/h2hdb/comics`，Komga 可使用：

```yaml
volumes:
  - /volume1/h2hdb/comics/current/acquisitions:/data/comics/_oneshots:ro
```

在 Komga 將書庫根目錄設為 `/data/comics`，One-Shots 目錄選項設為
`/_oneshots`，並關閉前述兩個自動掃描選項。
不要掛載整個 `current`：其中另有 OPDS 使用的 `artwork` 圖片，不應由 Komga 掃描。

同步程式所在容器另外掛載：

```yaml
volumes:
  - /volume1/h2hdb/comics/.h2hdb-coordination:/srv/h2hdb/coordination:ro
```

這些只是掛載片段；同步容器還需要安裝本套件、取得設定檔並能連到 Komga 和資料庫。
使用 SQLite 時也需提供資料庫路徑。不要把 ingest 私有的 `.h2hdb-state` 掛入
Komga 或同步容器。Coordination 路徑必須是絕對路徑，不能包含符號連結。

## 安裝

在準備執行同步的主機或容器內建立獨立 Python 環境：

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install h2hdb-komga
```

如果是從本專案原始碼安裝，在專案目錄將最後一行改為：

```bash
.venv/bin/python -m pip install .
```

## 設定

### Komga 連線

建立 `komga-config.json`，替換服務網址、書庫 ID 與 coordination 路徑：

```json
{
  "base_url": "https://komga.example.net",
  "api_username": "${KOMGA_API_USERNAME}",
  "api_password": "${KOMGA_API_PASSWORD}",
  "library_id": "your-library-id",
  "coordination_root": "/srv/h2hdb/coordination",
  "trigger_scan": true
}
```

`base_url` 是 Komga 的服務網址，不要加 `/api/v1` 或結尾斜線。
`library_id` 是目標 Komga 書庫的 ID，不是書庫顯示名稱。
`coordination_root` 使用同步程式能看到的路徑；容器內請使用容器路徑。

在執行命令的環境中提供帳號與密碼：

```bash
export KOMGA_API_USERNAME='your-account@example.net'
export KOMGA_API_PASSWORD='replace-with-your-password'
```

`${ENV_NAME}` 必須佔滿整個 JSON 字串，不能寫成 `prefix-${ENV_NAME}`。
變數未設定或帳號密碼空白時，命令會停止。也可以直接在 JSON 填入字串，
但使用環境變數可避免把密碼寫進設定檔。

`trigger_scan` 預設為 `true`，會先要求 Komga 掃描及分析書庫。
只有在 Komga 已完整反映目前書庫、只需要重新同步作品資料時，才設為 `false`。

### H2HDB 連線

建立 `h2hdb-config.json`。SQLite 範例：

```json
{
  "database": {
    "sql_type": "sqlite",
    "database": "/srv/h2hdb/catalog.sqlite3",
    "access_mode": "read-only"
  }
}
```

MariaDB 範例：

```json
{
  "database": {
    "sql_type": "mariadb",
    "host": "database.example.net",
    "port": 3306,
    "user": "h2hdb_reader",
    "password": "${H2HDB_DATABASE_PASSWORD}",
    "database": "h2h",
    "access_mode": "read-only"
  }
}
```

請填入既有書庫的連線資訊；MariaDB 範例另需設定 `H2HDB_DATABASE_PASSWORD`。
即使設定為可寫入，本工具仍會以唯讀模式開啟 H2HDB。它會修改 Komga，
不會修改 H2HDB 資料庫或漫畫檔案。

既有 schema 6 資料庫需先由管理者依
[H2HDB 的升級說明](https://github.com/Kuan-Lun/h2hdb#readme)
執行離線一次性 `upgrade-audit-schema.py` 轉換，保留 catalog 與 CBZ。
其他舊版本需由 H2HDB／ingest 準備新的相容資料庫；本工具不會自動升級。

## 執行同步

```bash
.venv/bin/python -m h2hdb_komga \
  --komgaconfig komga-config.json \
  --h2hdbconfig h2hdb-config.json
```

命令會觸發掃描與分析、等待 Komga 內容完整，再更新並核對作品資料。
書庫有缺漏、額外作品、重複檔名或非 One-Shot 作品時，會等待重新檢查，
不會只挑其中一部分開始同步。H2HDB 和 Komga 同時為空書庫也是有效狀態。

成功前會觀察內容與作品資料持續 30 秒沒有變動，因此即使沒有需要更新的作品，
命令也不會立即結束。預設整次執行最多一小時，可調整為例如兩小時：

```bash
.venv/bin/python -m h2hdb_komga \
  --komgaconfig komga-config.json \
  --h2hdbconfig h2hdb-config.json \
  --timeout-seconds 7200
```

命令正常結束代表本次同步完成；非零結束碼代表失敗，請檢查終端機記錄。
逾時或中斷前可能已有部分作品更新，排除原因後可重新執行完整命令。
若加入排程，請使用 Python 與設定檔的絕對路徑，並在排程環境提供所需變數。
同步期間會阻止 ingest 切換書庫，安排時間時請考慮整次同步所需時間。

## 疑難排解

| 現象 | 處理方式 |
| --- | --- |
| 帳號驗證或權限錯誤 | 檢查 Komga 網址、環境變數、帳號密碼及掃描／修改權限 |
| 找不到 `publication.lock` | 確認 ingest 已準備書庫、coordination 掛載正確且可讀，不要自行建立鎖定檔案 |
| 書庫鎖定中，或存在 `ACTIVATING` | 等待 ingest 完成發佈後重試；持續發生時由 ingest 處理復原，不要刪除鎖定檔或標記 |
| Komga 有作品但無法同步 | 檢查 `library_id`、One-Shot 設定，以及檔名是否為 `h2h-<gid>.cbz` |
| 持續等待或超過期限 | 檢查 Komga 掃描進度及記錄；排除缺少、額外、重複或非 One-Shot 作品後重試 |
| 掃描或分析請求逾時 | 確認 Komga 服務恢復後重新執行；逾時不能視為同步成功 |
| 資料庫版本不相容 | 依 H2HDB 說明先完成升級或重建，不能藉由更改設定略過檢查 |
| 標籤沒有出現在 Komga 的 Tags | 標籤寫入作者清單，命名空間顯示為角色；這是預期行為 |

啟動成功只代表資料庫可供此版本使用，不代表已完成全庫稽核。
完整資料檢查由 ingest 管理，或由管理者明確執行 H2HDB 的 `check`。

## 授權

本專案由 [Kuan-Lun Wang](https://www.klwang.tw/home/) 建立，採用
GNU General Public License v3.0，詳見 [LICENSE](LICENSE)。
