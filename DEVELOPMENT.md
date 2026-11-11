# Development & Deployment Guide (Windows)

This guide documents how to develop, test, refresh, and deploy **Triage** (install id `triage`, formerly `hermes-skill-judge`) on Windows for Hermes Agent & Hermes Desktop.

---

## 1. Architecture & Windows File Layout

Hermes on Windows stores its state, plugins, and logs in `%LOCALAPPDATA%\hermes` (`C:\Users\<username>\AppData\Local\hermes`), **not** `~/.hermes`.

| Component | Repository Source | Hermes Windows Path | Deployment Strategy |
| :--- | :--- | :--- | :--- |
| **Python Backend** | `<repo>` | `%LOCALAPPDATA%\hermes\plugins\triage` | NTFS Directory Junction |
| **Desktop UI** | `<repo>\desktop` | `%LOCALAPPDATA%\hermes\desktop-plugins\triage` | Linked package (`plugin.js`) |
| **SQLite Catalog** | *(Runtime SQLite store)* | `%LOCALAPPDATA%\hermes\plugin-data\triage\catalog.db` | Generated via `migrate` |
| **Hermes Config** | *(Host configuration)* | `%LOCALAPPDATA%\hermes\config.yaml` | `hermes plugins enable/disable` |
| **Hermes Logs** | *(Runtime logs)* | `%LOCALAPPDATA%\hermes\logs\` (`gateway.log`, `agent.log`) | Read for troubleshooting |

---

## 2. One-Time Setup & Deployment on Windows

### Step 1: Create Directory Junction for Backend Plugin
Use an NTFS directory junction (`mklink /J`) so code changes in your checkout (`<repo>`) reflect immediately in Hermes without copying files:
```powershell
cmd.exe /c mklink /J "%LOCALAPPDATA%\hermes\plugins\triage" "<repo>"
```

### Step 2: Enable in Hermes & Disable Conflicts
Disable legacy or competing retrieval plugins and enable `triage`:
```powershell
hermes plugins disable skill-retrieval
hermes plugins disable skill-router
hermes plugins enable triage
```
*Note: on its own, Hermes **copies** `desktop/plugin.js` to `%LOCALAPPDATA%\hermes\desktop-plugins\triage` and stamps `.hermes-package.json` beside it. That copy is a "package half": it starts **off**, it ignores the plugin's `defaultEnabled: true`, and its switch may not appear in Capabilities → Plugins. Step 2b replaces it.*

### Step 2b: Link the Desktop half yourself (this is what makes the UI appear)
Replace the managed copy with a junction to the repo's `desktop` folder. Hermes treats a **linked folder with no package marker** as your own plugin: the sync leaves it alone, and it loads on by default with no switch.

```powershell
$dst = Join-Path $env:LOCALAPPDATA 'hermes\desktop-plugins\triage'
Remove-Item -Recurse -Force $dst          # only a copied plugin.js + .hermes-package.json
cmd /c mklink /J "$dst" "<repo>\desktop"
```

Then press **Ctrl+R** in Hermes Desktop once (the renderer remembers the old "package, off" record until it reloads). Check: chips appear above the prompt as you type.

Do not commit `desktop/.hermes-package.json` if one ever shows up (it is in `.gitignore`). Its presence would turn the folder back into an opt-in package half.


### Step 3: Populate the SQLite Skill Catalog
Preferred: add folders under Settings → Plugins → triage → **Skill folders** and restart the gateway. The manual commands below do the same import.
Migrate your skills from your local Hermes skills directory into the plugin's SQLite database:
```powershell
# Migrate from default Hermes skills directory
python run.py migrate --from "$env:LOCALAPPDATA\hermes\skills"

# Or any other folder of SKILL.md skills:
python run.py migrate --from "<your skills folder>"
```

Verify that the catalog is populated:
```powershell
python run.py status
python run.py list
```

---

## 3. How to Develop & Work on It

### Python Backend (`*.py`)
- **`plugin.py`**: Hermes hook registrations (`pre_llm_call`, `pre_tool_call`, `transform_tool_result`, `post_compaction`) and middleware (`llm_request`, `tool_execution`).
- **`service.py`**: Runtime coordinator managing session state, allowlists, bridge injections, and dashboard REST endpoints (`/api/plugins/triage/*`).
- **`judge.py`**: Scoring engine. Runs FTS5 BM25 queries, applies recency weights, calculates softmax probabilities over the shortlist, and includes the explicit no-skill bucket.
- **`store.py`**: SQLite database schema, FTS5 virtual tables, and skill record persistence (`catalog.db`).
- **`strip.py`**: Outbound system prompt cleaner. Strips default `<available_skills>` / `## Skills` blocks from LLM requests.
- **`guard.py`**: Gating logic ensuring `skill_view` can only be invoked for approved or bridged skills.

### Desktop UI (`desktop/plugin.js`)
- The UI is packaged as a single standalone ES module (`desktop/plugin.js`) importing only `@hermes/plugin-sdk` and `react`.
- **Left row**: a trash / lock button, the held pile, a magnifier that opens the search list, and suggestion chips (`NN% <skill-name>`).
- **Right fan**: Active loaded skills (in-context deck / stacked cards) with hover peek details.
- Clicking a suggestion chip sends `POST /api/plugins/triage/approve` and immediately pushes the skill into the right fan.

---

## 4. How to Refresh & Reload

### Refreshing Desktop UI Changes
Because the desktop plugin is linked to your checkout:
1. Save changes to `desktop/plugin.js`.
2. In the Hermes Desktop application:
   - Navigate to **Capabilities → Plugins**.
   - Click **Rescan** (or press `Ctrl+R` / `F5` in the Desktop window).
3. The updated UI bundle hot-reloads without restarting Hermes.

### Refreshing Python Backend Changes
When you edit Python files (`plugin.py`, `service.py`, `judge.py`, etc.):
1. **Hooks & CLI commands**: Restart or reload the gateway:
   ```powershell
   # Toggle plugin to reload into the active gateway:
   hermes plugins disable triage
   hermes plugins enable triage

   # Or restart the gateway process directly:
   hermes gateway restart
   ```
2. **Catalog / Schema changes**: If you alter SQLite schema in `store.py`, re-run `python run.py migrate`.

---

## 5. Testing

### Run the Test Suite
The repository includes a comprehensive `unittest` test suite covering the store, judge, bridge, strip middleware, and approval flow:
```powershell
python -m unittest discover tests
```
*Expected output: `Ran 141 tests ... OK`.*

### Test Ranking & Peek from CLI
Simulate how the runtime judge scores user prompts against your active catalog:
```powershell
python run.py peek "review the commit message"
python run.py peek "deploy container to kubernetes cluster"
```
*Prints the shortlist with probability scores, no-skill probability, and explanation of BM25 matches.*

### Measure the Ranking
`eval` reports how often the right skill is the first chip, how often it is in the top 5, and how often auto-attach fired and was right, at eight thresholds. Run it before and after any change to `judge.py` or to a threshold.
```powershell
python run.py eval --db <demo catalog> --cases examples/eval-demo.jsonl --misses
python run.py eval-collect
python run.py eval --misses
```
- A case is one JSON line: `{"draft": "...", "expect": ["skill-a"], "kind": "described"}`. `expect` lists every skill that counts as right; `[]` means no skill is wanted; `null` means not labelled yet (skipped).
- `eval-collect` appends your real messages from Hermes' `state.db` to `<plugin data dir>/eval/drafts.jsonl` as unlabelled cases, with the ranker's top chips as a `hint`. That file holds private chat text and stays outside the repository. Existing lines and labels are kept.
- Both commands work on a temporary copy of the catalog. The live catalog and `state.db` are only read.
- Only the desktop chip path is measured (each skill scored against "no skill"), not the CLI / Telegram turn decision.

### Test Contract Validation
Validate that the plugin adheres to Hermes's manifest and security contracts:
```powershell
hermes plugins validate <repo>
```

---

## 6. Windows Troubleshooting & Gotchas

1. **Path Discrepancy (`~/.hermes` vs `%LOCALAPPDATA%\hermes`)**:
   Always verify paths using `$env:LOCALAPPDATA\hermes`. Hermes on Windows places configuration, desktop plugins, and data directories under `AppData\Local\hermes`.

2. **File Locking (`[WinError 32]`)**:
   On Windows, SQLite databases hold file locks when a process is running. If `python run.py migrate` or test runners complain about file locks, ensure background gateway processes or previous test processes are closed.

3. **Chips / fan / Auto-accept missing entirely**: the Desktop half is not loading. Check that `desktop-plugins\triage` is a junction (Step 2b) with no `.hermes-package.json` inside, then Ctrl+R. A plugin that loads and then fails shows a red `composer` warning chip in the composer, or a `[plugins] … failed` line in `logs\desktop.log`. "Nothing at all" means it never loaded.

   To trace the renderer without DevTools, turn on the `debug` plugin setting (`plugins.entries.triage.settings.debug: true`). The desktop half then writes `[triage-diag]` lines (registered, mounted, rank with top chips and source, turn-finished) to `logs\desktop.log`, which records only renderer `console.error`. Leave it off in shipped configs.

7. **Renamed from `hermes-skill-judge` (2026-11-08)**: install id, CLI (`hermes triage`), REST (`/api/plugins/triage/*`), project file (`.hermes/triage.yaml`) and plugin data (`plugin-data\triage`). Hermes' `plugin_data_dir()` creates the new folder on first use, so `paths.py` falls back to the old folder whenever the new catalog has no skills. Leftover old-name installs: `python scripts/sanitize_desktop_plugin.py`.

4. **Two session ids**: `host.state.focusedSessionId` is the runtime id (composer verbs, `slash.exec`). The Python hooks key on the stored id `local-<sha>` (`host.state.focusedStoredSessionId`). REST calls (`/approve`, `/loaded`) must send the stored id.

5. **Python edits need a gateway restart**: the `/rank` route and the hooks hold `judge.py` in memory. Desktop `plugin.js` edits hot-reload; Python edits do not.

6. **Checking Runtime Logs**:
   To inspect live hook execution, errors, or prompts:
   ```powershell
   # View recent agent logs
   Get-Content -Tail 50 -Wait "$env:LOCALAPPDATA\hermes\logs\agent.log"

   # View gateway logs
   Get-Content -Tail 50 -Wait "$env:LOCALAPPDATA\hermes\logs\gateway.log"
   ```
