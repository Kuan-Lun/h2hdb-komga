# AGENTS.md

## 政策來源

- 本檔是此 repository 的唯一代理開發政策來源。
- 其他代理入口只能要求完整閱讀本檔，不得複製另一份政策。
- 可執行規則以 repository 內的 scripts 與設定檔為準。

## 溝通

- 最終回覆一律使用繁體中文。
- 程式碼、識別字、命令、檔名與 commit message 可使用英文。
- 不得為了承載回覆而新增 Markdown 文件。
- 移除 compatibility path、改變公開行為或採用例外時，必須在對話及
  最終回覆中明確說明。

## 設計與修改原則

- 不預設存在最小修改或向後相容要求。
- 在任務範圍內選擇架構、可讀性與可測試性最好的完整結果。
- 綜合考慮 SOLID、KISS、YAGNI、內聚性與低耦合。
- 必要的局部重構可直接納入任務。
- 若會實質擴大任務範圍、改變原要求未涵蓋的公開行為，或引入資料遷移，
  必須先取得使用者同意。
- 任務直接涉及的 legacy compatibility code 應移除，不保留 shim；不全面
  清理與任務無關的 legacy code。
- generated output 不得直接修改；必須修改 generator 或 source 後重新產生。

## 工作樹與 Git

- 唯讀分析不建立 branch。
- 凡會修改 tracked files 的任務，使用
  `scripts/detect-primary-branch.sh` 判定 primary，並建立專用 task branch。
- 不得 stash、reset、clean、覆寫或混入既有使用者修改。
- 工作樹不乾淨時，從 committed primary 建立獨立 worktree。
- task branch 可包含多個邏輯 Conventional Commits。避免巨大 commit；小而
  內聚的任務仍可只有一個 commit。
- 任務完成後執行 `scripts/git-flow-merge.sh`。該腳本負責完整 gate、
  `--no-ff` merge、安全移除 task worktree，以及以 `git branch -d`
  刪除已合併的本機 branch。
- primary 在任務期間可以推進；整合只要求 primary 與 task branch 有共同
  ancestor，不要求 task branch 仍直接基於目前 primary tip。
- merge conflict 或 gate failure 時必須 abort merge 並保留 task branch。
- merge 後收到的任何 follow-up 都建立新的 task branch。
- 本機 task branch、commit、`--no-ff` merge 與 `branch -d` 已獲預先
  授權。
- fetch、pull、push、remote branch、tag、release、publish、deploy 與任何
  force 操作仍須逐次明確授權。
- 不得使用 `--no-verify`。

## 提交格式

- 所有非 merge commit 必須符合 Conventional Commits。
- Breaking change 使用 `type!:` 或 `BREAKING CHANGE:` footer。
- project version 更新使用獨立 commit：
  `chore(release): bump version to X.Y.Z`。

## 版本政策

- `pyproject.toml` 的 `[project].version` 是唯一 project version source。
- project version 固定使用 `X.Y.Z`。
- 1.0 前，`Y` 是 compatibility lane，`Z` 是同一 lane 內的相容 release
  counter。相容修正或功能遞增 `Z`；breaking change 遞增 `Y` 並將
  `Z` 歸零。
- 1.0 後使用標準 Semantic Versioning。
- 整個 task branch 只在整合前更新一次 project version。
- shipped runtime 或 deployment surface 有變更時，至少需要相容升版。
- Breaking API、CLI、config、schema、protocol、資料格式或 Python/platform
  support 變更必須提高 compatibility lane 或 major。
- tests、一般文件、IDE、hooks、CI 與 dev-only tooling 單獨變更時不升版。
- 未分類路徑必須明確判定 impact，不得靜默當作 `none`。
- `Version-Impact: none` 必須附具體理由，並在最終回覆揭露。
- project version 變更必須觸發完整 direct dependency audit。
- `scripts/check-version.py` 以 staged merge candidate 判定 release surface，
  並強制 task-level `X.Y.Z` 只升版一次；pre-merge gate 不得略過。
- 升版後執行 `scripts/audit-dependencies.py --review-note "<相容性結論>"`，
  並將 `.release/dependency-audit.json` 納入 task branch。receipt 必須符合
  candidate version 與完整 dependency manifest。

## 依賴與環境

- repository 必須能從單一乾淨 checkout 重建，不得依賴固定 sibling clone
  路徑。
- 明確跨 repository 任務可使用傳入的 wheel、Git URL/ref 或 repository
  path；sibling discovery 只能是選擇性的效能優化。
- Python registry dependencies 原則上使用 `>=` lower bound；合理 upper
  bound 與 `!=` 可以保留，但必須有相容性依據。
- 精確版本只允許經驗證且有文件理由的特殊契約。
- dependency audit 必須涵蓋 build、runtime、optional 與 development direct
  dependencies，並搜尋現有 upper bound 之外的候選版本。
- 有新版時必須檢查 release notes、驗證相容性並嘗試修正問題。
- `uv.lock` 不得成為環境重建或驗證的輸入；`scripts/rebuild-env.sh` 可
  使用 `uv venv` 與 `uv pip`，但不得使用會依賴 project lockfile 的
  同步流程。
- Node tooling 使用 `npm install --package-lock=false`，不得產生或提交
  `package-lock.json`。
- 不得依賴 system-wide lint、format、type-check 或 Markdown 工具。
- `requires-python` 使用 `>=3.14`；只有經驗證的壞版本可使用 `!=`。

## 品質工具

- `pyproject.toml` 是 Ruff 與 mypy 的唯一規則來源。
- 使用 Ruff lint 與 Ruff formatter，不使用 Black。
- Ruff 使用適合專案的嚴格規則集，不從 `ALL` 出發；每個停用規則必須
  記錄理由。
- mypy 使用標準 `strict = true`。不得保留 `mypy.ini`。
- module 例外使用精確 TOML overrides。
- `type: ignore` 必須指定 error code 並附理由。
- `noqa` 必須指定 rule code 並附理由。
- Markdown 使用 repository-local `markdownlint-cli2`。
- VS Code 使用相同設定與 repository-local environment；CLI gate 是最終
  權威，IDE diagnostics 為即時輔助。

## 檢查分層

- `scripts/format.sh`：明確執行會修改檔案的 formatter 或 fixer。
- `scripts/check-fast.sh`：離線、唯讀的 Ruff、format check、mypy 與
  markdownlint；每次非 merge commit 執行。
- `scripts/check-full.sh`：fast gate、完整測試、build、wheel smoke 及本
  repository 的特殊檢查；整合候選只跑一次。
- dependency audit 可連網，但 hooks 只驗證本機 receipt，不在 commit
  過程連網。
- GitHub Actions 只呼叫相同 scripts，並保留 trusted publishing、平台特有
  或本機無法可靠重現的檢查。
- 不使用 Claude、Codex 或其他 provider-specific Stop hooks 重複檢查。

## 測試與例外

- runtime 行為變更必須新增或更新測試；bug fix 必須有 regression test。
- 新功能涵蓋正常、邊界與錯誤路徑。
- 數值測試固定隨機種子；容許誤差需有依據。
- flaky test 視為失敗，不得以重跑掩蓋。
- 不設定跨 repository 的統一 coverage 百分比。
- live account、network、production 或 destructive probe 不得進入 hooks、
  一般 pytest 或自動 merge gate。
- `skip` 或 `xfail` 必須有理由；`xfail` 原則上使用 `strict=True`。
- 不得為通過檢查而全域放寬工具設定。

## 完成回報

最終回覆必須包含：

- 實作及公開行為變化。
- 移除的 compatibility path。
- project version 與 dependency audit 結果。
- commits 與完整檢查結果。
- primary branch 與 merge commit。
- branch/worktree 是否已清除。
- 是否仍未 push、publish 或 deploy。

## Repository-specific policy

`h2hdb-komga` 是將已 publication H2HDB catalog metadata 同步至 Komga 的
CLI，發佈名稱為 `h2hdb-komga`，公開 import package 為
`h2hdb_komga`。它是 read-only catalog consumer，不擁有或 migrate core
schema。

### Architecture and contracts

- `config_loader.py` 擁有 frozen `KomgaConfig` 與 JSON loading。
  `coordination_root` 是 required absolute path，指向 ingest
  `.h2hdb-state/coordination` 的獨立 read-only mount。
  `coordination.py` 擁有 reader fencing，必須以 descriptor-relative、逐層
  `O_NOFOLLOW` 且 `O_NONBLOCK` 的方式只讀開啟 regular
  `publication.lock`，不得建立或修改 coordination state。
  `metadata.py` 是 neutral `CatalogPublication` 到 Komga metadata 的唯一
  translation layer。`komga.py` 是具有 hard timeout 的薄 REST client；
  orchestration/retry policy 不得放入 client。
- `sync.py` 使用 injected `CatalogReader` 與 Komga gateway。它只接受
  `h2h-<gid>.cbz` canonical basename（可容許 Komga 省略副檔名），將名稱
  normalize 後以 public artifact-name lookup 查詢；不得重新加入 pure GID、
  content-addressed、friendly `[gid]` 或 catalog pagination fallback。每個
  lookup pass 都 pin 同一 revision。
- core head 在 pinned lookup 中推進時，必須在建立任何 PATCH 前丟棄整個 pass，
  清除 local observation，並從 fresh current head 重試；不得混用兩個 head。
- 每次 poll 都重新 reconcile 所有 current books，讓 transient GET failure
  與被 Komga analysis 改回的 metadata 能重試。完成條件是 unchanged、
  write-free observation window；整體 polling 必須有 hard timeout。
- per-book fetch、verification 與 bulk PATCH chunk 使用 bounded
  `ThreadPoolExecutor`（`KOMGA_MAX_WORKERS`）。verification 在同一 attempt
  的 chunks 全部完成後才執行，不能 nested pools。不得重新引入已移除且不適合
  I/O work 的 `h2hdb.threading_tools.ThreadsList`。
- bulk PATCH 204 不代表每本書成功；每次 attempt 後都重新 fetch 驗證，只重試
  failed books，超過 `PATCH_RETRY_ATTEMPTS` 必須 fail。
- 每次完整 sync 必須 nonblocking 取得 `publication.lock` shared flock，並在
  lock 下確認不存在任何 `ACTIVATING` entry。shared lock 從觸發 Komga
  scan/analyze 前持有到 settling、metadata reconciliation 與 final stability
  check 完成；lock busy、missing/nonregular、symlink/FIFO 或 marker present
  一律 fail closed。worker 被正常停止或 hard kill 時依賴 descriptor close
  釋放 kernel lock，不得寫入 reader marker。
- Komga library 的 **Scan on startup** 與 **Scan interval** 都必須 disabled；
  只有本 coordinated CLI job 可以觸發 scan/analyze，不得由 Komga UI、其他
  API client 或 scheduler 執行未持鎖的 scan。
- CLI 將 `CoreConfig.database.access_mode` 強制改為 read-only，再呼叫 top-level
  `open_database()` 執行 epoch-3 `READY` audit。不得 import core internals
  或呼叫 `migrate()`。outer process supervisor 必須維持 wall-clock hard
  deadline，即使 socket、database gate 或 thread 不合作也能終止 worker。

### Verification

- tests 使用 fake `CatalogReader` 與 Komga gateway，不得要求 live Komga、
  production database 或外部網路。
- live Komga validation 必須逐次取得使用者授權，且永遠不得進入 commit 或
  merge gate。
- neutral mapping、missing artifact、revision advance、settling loop、PATCH
  verification、bounded concurrency、hard timeout 與 read-only bootstrap
  變更都必須有 regression tests。
- coordination tests 必須涵蓋 full-sync shared lock lifetime、exclusive
  contention、marker、symlink path、FIFO/nonregular lock 與 descriptor cleanup；
  canonical basename tests 必須拒絕 Unicode digit。
- `scripts/check-full.sh` 執行完整離線 pytest、sdist/wheel build，以及從
  installed wheel 執行 package/CLI smoke。
- comments 預設不寫；只有程式碼無法表達的 hidden constraint 或 workaround
  才加 timeless explanation，不得留下描述本次修改過程的註解。
