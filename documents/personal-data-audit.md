---
name: personal-data-audit
description: "Audit a git repo for accidentally committed personal data — API keys / tokens, real Riot IDs / PUUIDs / summoner names, personal filesystem paths with usernames, real email addresses (including Git author metadata in commit history), and embedded raw records inside committed JSON / HTML data files. Use whenever the user asks to scan, check, or audit a repo for leaks of personal info, secrets, credentials, or PII — including a request to verify that a repo is safe to publish or share. Triggers on: check for personal data leak, audit repo for secrets, scan repo for leaks, repo PII audit, is this repo safe to publish, 個人資料洩漏, 個資洩漏, 個資外洩, 檢查個資, 有沒有洩漏個人資料, 檢查 repo 個資, repo 有沒有外洩, /personal-data-audit. Do NOT trigger when the user wants to add new secrets-management code, set up CI secret scanning (use gitleaks instead), or write security policy docs."
metadata:
  version: "1.0"
  last_updated: "2026-05-15"
  status: active
  scope: personal-global
---

# personal-data-audit — Repo 個資 / Secret 洩漏審計

把「掃 repo 看有沒有把個人資料推上去」壓縮成 7 個固定類別的審計。每次都跑完 7 項，不要中途看到幾項 clean 就下結論。

## 為什麼這是 skill

「幫我看 repo 有沒有洩漏個資」這句很模糊。沒 skill 時最常見的失敗：

1. **只 grep tracked files、漏掉 Git author metadata**。`git log` 和 `.git/logs/HEAD` 裡的真實 email 用一般 file grep 抓不到 — 這是 90% 漏掉的洩漏類型。
2. 用泛用 `grep secret`、漏掉專案特定的 ID 格式（例如 Riot ID `Name#TAG`、LCU PUUID UUID 格式）。
3. 把 placeholder（`Name#TAG`、`RGAPI-xxxx`、`example@example.com`）誤判成真實洩漏（cry wolf）。
4. 不檢查 committed 大型資料檔（`docs/*.html`、aggregated JSON）裡有沒有夾帶原始紀錄。

這個 skill 把 7 項必查固定下來、並寫清楚 placeholder vs 真實值的辨識規則。

## When to invoke

Auto-trigger 在審計類請求：
- 「有沒有洩漏個人資料 / 個資 / 個資外洩」
- 「audit / scan / check the repo for secrets / leaks / PII」
- 「commit 了什麼不該推上去的東西」、「這 repo 推上 GitHub 安全嗎」
- 顯式 `/personal-data-audit`

**不要 invoke**：使用者要寫 secret-management 程式（是實作不是審計）、要設 CI gitleaks（請走專門工具）、純詢問 PII 是什麼。

## 審計流程（7 項都要跑完）

### 0. 起手 — 確定範圍

```bash
git -C <repo> ls-files | wc -l       # tracked file 數
git -C <repo> log --oneline | wc -l  # commit 數
git -C <repo> remote -v               # 是否有遠端（push 風險）
```

只審 **tracked files** + **git metadata**。`data/`、`models/`、`__pycache__/` 等 gitignored 內容不在範圍。

### 1. API key / token / secret

```bash
git -C <repo> grep -nIE 'RGAPI-[A-Za-z0-9-]{20,}'              # Riot
git -C <repo> grep -nIE 'AKIA[0-9A-Z]{16}'                      # AWS access key
git -C <repo> grep -nIE 'ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}'  # GitHub token
git -C <repo> grep -nIE '(?i)(api[_-]?key|token|secret|password|bearer)\s*[=:]\s*["'"'"'][^"'"'"']{16,}'
```

Placeholder vs real：`RGAPI-xxxx`、`RGAPI-...`、`<your key>` → placeholder；長度 / charset 接近真實格式的 → 視為 real，要 user 確認。

### 2. 個人 ID / 帳號（依專案類型客製）

LoL / Riot：
- Riot ID `Name#TAG`（3-5 char tag）
- LCU PUUID（36-char `8-4-4-4-12` UUID）
- Riot PUUID（78-char base64-ish）
- summoner name、account ID、game ID

```bash
git -C <repo> grep -nIE '[A-Za-z0-9_一-鿿]{2,16}#[A-Za-z0-9]{3,5}\b'
git -C <repo> grep -nIE '\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'
```

辨識：`Name#TAG`、`YourID#0001`、`Example#TW1`、docstring 內示範 → placeholder；真實玩家風格的 ID → real。

其他專案類型對應替換：discord user ID（snowflake `\d{17,19}`）、Twitter handle、Steam ID 等。

### 3. 個人檔案系統路徑

```bash
git -C <repo> grep -nIE 'C:\\\\Users\\\\[^\\\\]+'
git -C <repo> grep -nIE '/home/[^/]+/'
git -C <repo> grep -nIE '/Users/[^/]+/'
```

加分：若使用者本名 / 機器名已知（從前文或 CLAUDE.md），加入 grep 字串。

### 4. Email

```bash
git -C <repo> grep -nIE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
```

Placeholder：`example@*.com`、`user@domain.*`、`noreply@*`、`*@anthropic.com` 屬於工具預設 → placeholder。

### 5. Git author metadata（**最常漏掉的一層**）

這層 grep tracked files 抓不到，必須單獨檢查：

```bash
git -C <repo> log --all --format='%ae' | sort -u   # 出現過的所有 author email
git -C <repo> log --all --format='%an' | sort -u   # 出現過的所有 author name
git -C <repo> log --all --format='%ce' | sort -u   # committer email（rebase 後可能不同）
```

Verdict：
- 任何非 `*@users.noreply.github.com` 的真實 email → 視為 **LEAKED**
- 真實本名（vs GitHub username）→ 視為 SUSPICIOUS，flag 出來讓 user 決定

也順便看 `.git/logs/HEAD` reflog（同樣含 email），但這個檔不會跟 push 一起送上 GitHub — 對遠端風險較低。

### 6. Committed 資料檔案內嵌的原始紀錄

大型 committed data file（aggregated JSON、HTML dashboard、CSV report）可能誤把原始紀錄嵌進去：

```bash
git -C <repo> ls-files | xargs -I{} wc -c "{}" 2>/dev/null | sort -nr | head -10
```

對每個 > 50 KB 的 tracked data file，掃原始紀錄 marker：

```bash
grep -cE 'puuid|matchId|summonerName|accountId|"gameId"' <suspicious-file>
```

> 0 即代表夾帶原始紀錄 — 但要看上下文（schema docstring 出現 `"puuid"` 字面 ≠ 真實 PUUID）。

### 7. `.gitignore` 充分性（未來風險）

```bash
grep -E '^\.env|\.pem|\.key|credentials|secret' <repo>/.gitignore
```

若缺泛用 secret rule（`.env`、`*.pem`、`*.key`、`*.p12`、`credentials.json`、`*.secret`），flag 為「未來 `git add` 意外帶入的風險」，但這不是現有洩漏。

## Output 格式

```
## Verdicts

| Category | Verdict |
|---|---|
| 1. API keys / tokens     | CLEAN / SUSPICIOUS / LEAKED |
| 2. 個人 ID / 帳號         | ... |
| 3. 個人路徑               | ... |
| 4. Email（tracked files） | ... |
| 5. Git author metadata   | ... |
| 6. 大檔內嵌原始紀錄       | ... |
| 7. .gitignore 充分性     | OK / 不足 |

## Findings
[每項 LEAKED / SUSPICIOUS：file:line + 看到的內容（**redact 真實值**，例如 `RGAPI-xxxx...`、`<real-email>@gmail.com`）]

## Remediation
[針對每個 finding 給修補建議；只給建議不要自己動手洗歷史]

## 一行結論
LEAKED / SUSPICIOUS / CLEAN
```

Verdict 定義：
- `CLEAN`：沒找到任何疑似洩漏。
- `SUSPICIOUS`：找到看起來像、無法確定是真實的（請 user 確認）。
- `LEAKED`：確認真實洩漏，要立即處理（rotate / scrub）。

## Remediation 對照表

| Finding | 修補 |
|---|---|
| API key in source | **先 rotate key**；`git filter-repo --replace-text` 或 `filter-branch` 洗歷史；改用環境變數 / `.env`（並加 gitignore） |
| 真實 email in git metadata | 未來：`git config user.email <id>+<user>@users.noreply.github.com`；過去：`git filter-branch -f --env-filter`（見 Windows 注意事項）→ `git push --force-with-lease` |
| 真實本名 in git metadata | `git config user.name <github-username>`；歷史一併洗 |
| 個人路徑 hardcoded | 改用 `Path(__file__).resolve().parent` 相對路徑 |
| `.gitignore` 不足 | 加 `.env`、`*.pem`、`*.key`、`*.p12`、`credentials.json`、`*.secret`、`.envrc` |
| 大檔嵌入原始紀錄 | 重建檔案只保留 aggregated 欄位；舊 commit 用 filter-repo 洗 |

## Windows 平台注意

- `git filter-repo`（pip 安裝或直接下載 single-file script）在 Windows 上可能被 Defender 即時掃描卡住 — symptom 是 `python.exe` 啟動後 CPU 0% 但不退出（這是真實案例：同一台機器上的 polars import 也有相同現象）。
- Fallback：用 git 內建 `git filter-branch -f --env-filter '...'` — deprecated 但走 git 原生路徑，不會被 AV 卡。
- 洗完後務必清乾淨：
  ```bash
  git for-each-ref --format='%(refname)' refs/original/ | xargs -I{} git update-ref -d "{}"
  git reflog expire --expire=now --all
  git gc --prune=now --aggressive
  ```
- 驗證舊 SHA 已消失：`git cat-file -e <old-sha>` 回 `fatal: ...` 才算乾淨。

## NEVER

- ❌ 因為 tracked files 都 clean 就結論整體 clean — **Git author metadata 抓不到**，這是 skill 存在的主因。
- ❌ 把 placeholder（`Name#TAG`、`RGAPI-xxxx`、`example@*.com`）報成 LEAKED — cry wolf 讓 user 失去信心。
- ❌ 主動執行 `git filter-branch` / `filter-repo` 洗歷史 — 這是 destructive，必須 user 明確同意才能做；本 skill 只負責審計與寫修補建議。
- ❌ 在 output 裡貼出真實的 key / email / PUUID 全文 — 永遠 redact（`RGAPI-xxxx...` / `<real-email>` / `xxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`）。
- ❌ 把 `.git/` 整個視為「在 tracked files 範圍之外」就完全忽略 — `.git/logs/HEAD` 和 `git log` 都要單獨查。
