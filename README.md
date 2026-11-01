# AOE-caddy — read-only AoE2 telemetry prototype

This is a small Windows-only Python collector for the **AoE2Control Agent Bridge V3** protocol (AoE2Control 1.1.1+). It **never sends `act`** or other gameplay commands. It rejects non-`player_view` observation policies, and logs raw observations as timestamped JSONL. All Python package dependencies are standard library; pytest is optional for tests.

**Protocol reference:** https://aoe2control.github.io/agent-bridge/

**Repository:** https://github.com/bryanfelixg/AOE-caddy

The Python package remains `aoe2coach`, with the `aoe2-collect` command. Verified telemetry extractors exist ([docs/telemetry.md](docs/telemetry.md)); coaching analysis and a dashboard are planned, not implemented.

## 1. Windows setup

Run each step from this repository root in PowerShell. Check the confirmation cue before proceeding; if it fails, retain the exact error for debugging.

### Step 1: Create and select a virtual environment

Install Python >=3.10. Inspect installed versions with `py --list`, then use a supported interpreter:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable); print(sys.version); print(sys.prefix != sys.base_prefix)"
```

If `.venv` already exists, inspect and reuse it rather than overwriting it blindly. In VS Code, run **Python: Select Interpreter** and choose `.venv\Scripts\python.exe`.

**Confirmation:** the executable is inside this project's `.venv`, Python is >=3.10, and the isolation check prints `True`. Python 3.14.4 was used for the verified local setup.

Optional activation: `.\.venv\Scripts\Activate.ps1`. If execution policy blocks it, keep using the explicit interpreter below; no security-policy change is needed.

### Step 2: Install the package and test dependencies

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip check
```

**Confirmation:** successful installation and `No broken requirements found`. If an editable installation and pytest >=8 already exist in this environment, verify them with `-m pip show aoe2-coach-collector pytest` and `-m pip check`; reinstallation is unnecessary.

### Step 3: Test without the game

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m aoe2coach --help
```

**Confirmation:** all tests pass; help lists `status`, `record`, and `snapshot`. These use fake transports and a recorded fixture, not a live game connection.

### Step 4: Configure the game bridge

1. Install **Age of Empires II: Definitive Edition** via Steam.
2. Review and install AoE2Control from its official project, https://aoe2control.github.io/ . This is an **unofficial injected DLL** with security and maintenance risks; validate provenance and test in a non-sensitive environment. Do not indiscriminately disable security software.
3. Launch a **single-player, cheats-disabled** AI game and load CONTROL using its documented launcher instructions. Use CONTROL 1.1.1+ with Agent Bridge V3 support.
4. In the running game's CONTROL → **Modules**, enable **Agent Bridge** globally and for the player slot you control in **Player 1–8** settings. Leave the opponent's native AI enabled. The bridge refuses multiplayer and cheats-enabled matches.
5. **Disable Modules See Everything** so `information_policy` is `player_view`. Close any other bridge client for your player.

**Confirmation:** the match is loaded, CONTROL's in-game menu is visible, and both bridge toggles stay enabled after reopening the menu. Running the launcher alone does not prove the player bridge is available.

### Step 5: Check live status

Substitute your actual player slot throughout the following commands:

```powershell
.\.venv\Scripts\python.exe -m aoe2coach status --player 1
```

**Confirmation:** successful exit and JSON showing `state: "ready"`, the correct `player_id`, and `information_policy: "player_view"`. Keep the game and CONTROL running.

### Step 6: Capture a snapshot

```powershell
New-Item -ItemType Directory -Path .\data -Force | Out-Null
$snapshot = Join-Path .\data ("observation-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff") + ".json")
.\.venv\Scripts\python.exe -m aoe2coach snapshot --player 1 > $snapshot
if ($LASTEXITCODE -ne 0) { throw "Snapshot failed; inspect the bridge error above." }
.\.venv\Scripts\python.exe -c "import json, sys; from pathlib import Path; o=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig')); assert o['information_policy']=='player_view'; print('world', o['world'], 'sequence', o['sequence'], 'game_time_ms', o['game_time_ms'], 'entities', len(o['entities']))" $snapshot
```

**Confirmation:** parseable `player_view` JSON with world, sequence, game time, and entities. PowerShell 7 writes UTF-8 redirection output; on Windows PowerShell 5.1, use `| Out-File -Encoding utf8 $snapshot` instead of `> $snapshot`. Files under `data` are excluded from Git.

### Step 7: Record a short sample

Keep the match unpaused. Use a fresh output path because recording appends:

```powershell
$recording = Join-Path .\data ("setup-check-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff") + ".jsonl")
.\.venv\Scripts\python.exe -m aoe2coach record --player 1 --hz 2 --count 20 --output $recording
if ($LASTEXITCODE -ne 0) { throw "Recording failed; inspect the error above." }
.\.venv\Scripts\python.exe -c "import json, sys; from datetime import datetime, timedelta; from pathlib import Path; r=[json.loads(s) for s in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()]; assert len(r)==20; assert all(datetime.fromisoformat(x['captured_at_utc']).utcoffset()==timedelta(0) and x['observation']['information_policy']=='player_view' for x in r); k=[(x['observation']['world'],x['observation']['sequence']) for x in r]; assert all(a!=b for a,b in zip(k,k[1:])); assert r[-1]['observation']['game_time_ms']>r[0]['observation']['game_time_ms']; print('PASS: 20 UTC player_view samples, no adjacent duplicates, game time advances')" $recording
```

**Confirmation:** `Recorded 20 observations` and the validation prints `PASS`. If recording repeatedly reports an unavailable bridge or never reaches the count, stop with Ctrl+C and check the match/bridge rather than waiting indefinitely.

Start `record` without `--count` for continuous recording; stop with Ctrl+C. Each CLI invocation opens and closes its own session when needed; only one client can own a player slot at a time. You can also use `.\.venv\Scripts\aoe2-collect.exe ...`.

## 2. How it works

```text
AoE2:DE [single-player] → CONTROL Agent Bridge V3
                                    |
                       Windows named pipe (one request/response)
                                    |
                      aoe2coach.bridge.ReadOnlyBridge
                                    |
                       aoe2coach.collector.record
                                    |
                            timestamped JSONL
```

Request limit: 1 MiB. Response limit: 16 MiB. The pipe name is `\\.\pipe\AoE2ControlAgentV3.P1` for player 1. A session expires after 30 seconds without a request. The bridge normally serves observation requests on the next game frame (up to 2 seconds). The collector polls at 2 Hz by default, but effective latency may be higher.

## 3. Output format

Every line is standalone JSON:

```json
{"captured_at_utc":"2026-10-31T14:00:00+00:00","observation":{"world":1,"sequence":42,"game_time_ms":12345,"information_policy":"player_view","player":{},"entities":[]}}
```

The example is a schema illustration, not real telemetry. The collector preserves the complete observation payload; it omits `object_types` and `technologies` catalogs from each observe call to reduce response size. Catalog information returned from `open` isn't persisted yet; retain it in a later version if type normalization needs it.

Deduplication uses `(world, sequence)` within a single recording process. New worlds are automatically detected, so you can record across multiple consecutive matches in one process. The file is appended to, not overwritten; use a unique output path per recording run.

## 4. Safety and operating assumptions

- `ReadOnlyBridge.request` explicitly allows only `status`, `open`, `observe`, `map`, `close`; it cannot send `act` or `query` in this initial prototype.
- `open` and each `observe` check `information_policy == "player_view"` (the bridge also supports omniscience, which this client rejects).
- It does not use any external network or upload gameplay data.
- It still relies on a third-party injector in the game. Never assume it is permitted for multiplayer/competitive use.
- Named-pipe errors 2 and 231 are retried for a bounded interval. The collector reconnects after recoverable session errors. Other remote protocol errors are surfaced rather than silently ignored.
- Ctrl+C and exceptions trigger a best-effort `close`. If CONTROL crashes or is killed, the bridge will expire the session.

## 5. Troubleshooting

| Symptom | Check |
|---|---|
| `winerror=2` opening pipe | CONTROL running? Global and player-specific Agent Bridge toggles enabled? Correct player ID? |
| CONTROL running, but player pipe missing | Recheck both toggles inside the current game. `AoE2ControlStatus` or `AoE2ControlRmsIdeV1` pipes do not substitute for `AoE2ControlAgentV3.P1`. |
| `winerror=231` | Another client or request may temporarily occupy the pipe; collector retries. |
| `waiting_for_match` | Start a valid single-player game and wait for it to load. |
| `multiplayer_refused` | Use a single-player, cheats-disabled match; bridge disallows multiplayer and cheats-enabled matches. |
| `player_in_use` | Another client owns the same player session; stop it or wait for expiration. |
| Refusing omniscient mode | Turn off `Modules See Everything`. |
| `session_expired`/`invalid_session` | Reconnect occurs automatically in record mode. |
| `observation_unavailable` | The match may not be running or the player is not in the current match. |
| Unexpected large response | This prototype disables catalogs per sample. Add an `entity_fields` allowlist if needed. |

## 6. Tests and limits

Run `python -m pytest -q` in your environment on Windows, Linux or macOS; the unit tests use an injected fake transport, so the game is not required. **The actual Windows named-pipe path requires on-machine integration testing.**

Local setup validation passed with Python 3.14.4 and CONTROL 1.1.5: unit tests, live player-1 `ready` / `player_view` status, a snapshot, and 20 unique JSONL observations with advancing game time. This validates collection in that environment, not all game versions or future coaching features.

## 7. Publishing source to GitHub

The existing destination is **AOE-caddy**, not AOE2-caddy. Keep `.venv`, `data`, caches, generated metadata, and credentials out of commits. Configure your author identity locally; use a GitHub noreply email if you do not want your personal address in commit history.

For a new local repository with an empty remote:

```powershell
# Skip initialization if this folder already has a Git repository.
git init -b main
git config --local user.name "YOUR_NAME"
git config --local user.email "YOUR_COMMIT_EMAIL"
git remote add origin https://github.com/bryanfelixg/AOE-caddy.git
git remote -v
git ls-remote --heads origin
git add -- .gitignore README.md pyproject.toml aoe2coach tests
git diff --cached --stat
git diff --cached --name-only
git commit -m "Initialize read-only AoE2 collector" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git push -u origin main
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

Inspect each result before proceeding. Reuse an existing correct origin instead of adding it again. If the remote already has history, preserve it and resolve integration explicitly before pushing; never force-push to bypass a rejection.

**Confirmation:** only intended source/docs are staged, push succeeds, local HEAD equals the remote main hash, and files appear on GitHub. Use Git's trusted credential flow if authentication is required; do not put tokens in chat, URLs, or project files.

## 8. Coaching roadmap

```text
AoE2:DE single-player -> read-only collector -> local telemetry/catalogs
    -> shared analysis engine -> live browser dashboard + post-game summary
```

Implement these milestones separately, with confirmation and tests at each stage:

1. **Verify telemetry — done.** Villager, Town Center (training / researching / housed / idle), resource, and villager-queue signals were verified in live controlled tests; see [docs/telemetry.md](docs/telemetry.md) and `aoe2coach/telemetry.py`. Unit counts (`object_types`) are fetched live periodically, not stored in recordings. Multiple TCs, later ages, and other civilizations are not yet verified.
2. **Shared analysis.** Target villager idle episodes lasting **5 game-seconds**, Town Center idle episodes lasting **1 game-second** (training or researching counts as busy), a separate **housed** alert at 1 game-second, plus resource stockpiling and build-order reminders. Confirm stockpile thresholds and a specific build order/civilization before implementing those rules. Test exact thresholds, activity resumption, match resets, missing/stale observations, and pauses.
3. **Local browser dashboard beside the game.** Show live advice and connection freshness, with visual and sound alerts once per idle episode. Include sound enable/mute controls and visual fallback for browser autoplay restrictions. Bind to loopback and share a single collection session; do not open another player session for the display.
4. **Post-game summaries.** Reuse the live event rules for idle durations, production gaps, resource alerts, and build-order adherence. Mark incomplete data and verify live/offline parity.

The configured 2 Hz is a polling target, not a delivery guarantee. Game speed and bridge latency affect when alerts arrive; measure a 1-game-second TC rule against game timestamps without promising 1-second wall-clock delivery. No automated gameplay, multiplayer use, cloud telemetry upload, or injected coaching overlay is planned.
