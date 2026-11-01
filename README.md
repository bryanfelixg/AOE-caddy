# AOE-caddy — read-only AoE2 telemetry prototype

This is a small Windows-only Python collector for the **AoE2Control Agent Bridge V3** protocol (AoE2Control 1.1.1+). It **never sends `act`** or other gameplay commands. It rejects non-`player_view` observation policies, and logs raw observations as timestamped JSONL. All Python package dependencies are standard library; pytest is optional for tests.

**Protocol reference:** https://aoe2control.github.io/agent-bridge/

**Repository:** https://github.com/bryanfelixg/AOE-caddy

The Python package remains `aoe2coach`, with the `aoe2-collect` command. It includes verified telemetry extractors ([docs/telemetry.md](docs/telemetry.md)), the coaching rules engine ([docs/coaching-rules.md](docs/coaching-rules.md)), a live local dashboard (`aoe2-collect coach`, Step 8) and post-game summaries (`aoe2-collect summary`, Step 9).

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

### Step 8: Live coaching dashboard

With a single-player match running (Steps 4–5), start the coach:

```powershell
.\.venv\Scripts\python.exe -m aoe2coach coach --player 1
```

**Confirmation:** the terminal prints `Dashboard: http://127.0.0.1:8765/` and `Recording to data\coach-<time>.jsonl`, and your browser opens the page. The badge turns green **LIVE** and game time follows the game. Click **Enable sound** (one test chime; adjust the volume slider). Idle-TC alerts appear about 1–2 s after production stops; idle villagers after 5 game-seconds. Pausing the game shows **PAUSED**; Ctrl+C stops the coach and the page shows **OFFLINE**.

The coach owns the player's bridge session, so do not run `record` for the same player at the same time. Useful options:

| Option | Effect |
|---|---|
| `--no-record` | Coach without saving a recording |
| `--no-browser` | Do not open the browser automatically |
| `--port 8766` | Use another port (the page is only served on 127.0.0.1) |
| `--replay data\coach-<time>.jsonl --speed 4` | Replay a recording through the same rules instead of the game (`--speed 0` = instant) |
| `--full-recording` | Save every sample in full (see Output format) |

Alert rules are documented in [docs/coaching-rules.md](docs/coaching-rules.md).

### Step 9: Post-game summary

When you stop `coach` with Ctrl+C it writes a summary next to its recording (`data\coach-<time>.summary.html`) and prints a short version. To summarize any recording yourself:

```powershell
.\.venv\Scripts\python.exe -m aoe2coach summary --input data\coach-<time>.jsonl
```

**Confirmation:** the terminal prints per-match idle-villager, Town Center idle, housed and stockpiling totals, age-up times, build-order timings and a villager count per minute, followed by `Report: ...summary.html`, which opens in your browser (`--no-browser` to skip; `--output` to choose the file). Summaries replay the recording through the same rules as the live dashboard, so the numbers match what it showed. Recordings that start mid-game, gaps in the data, alerts still active when recording stopped, and unreadable lines are called out; the match result is not claimed.

## 2. How it works

```text
AoE2:DE [single-player] → CONTROL Agent Bridge V3
                                    |
                       Windows named pipe (one request/response)
                                    |
                      aoe2coach.bridge.ReadOnlyBridge
                                    |
                       aoe2coach.collector.record ----> timestamped JSONL
                                    |
              aoe2coach.telemetry.extract -> aoe2coach.analysis.Coach
                                    |
              aoe2coach.dashboard (127.0.0.1, Server-Sent Events) -> browser
```

Request limit: 1 MiB. Response limit: 16 MiB. The pipe name is `\\.\pipe\AoE2ControlAgentV3.P1` for player 1. A session expires after 30 seconds without a request. The bridge normally serves observation requests on the next game frame (up to 2 seconds). The collector polls at 2 Hz by default, but effective latency may be higher. While the game is paused the bridge keeps answering with new `sequence` numbers but frozen `game_time_ms`.

## 3. Output format

Every line is standalone JSON:

```json
{"captured_at_utc":"2026-10-31T14:00:00+00:00","observation":{"world":1,"sequence":42,"game_time_ms":12345,"information_policy":"player_view","player":{},"entities":[]}}
```

The example is a schema illustration, not real telemetry. The collector omits `object_types` and `technologies` catalogs from each observe call to reduce response size. Catalog information returned from `open` isn't persisted.

By default `record` and `coach` save compact recordings: samples whose game time has not advanced since the previous one (a paused game) are not saved, and fog-of-war entities (`access: "position"`, mostly trees and mines out of sight) are dropped from saved rows, which then carry `"omitted_position_only_entities": <count>`. On a real 14-minute session this reduced the file from 436 MB to 113 MB without affecting coaching or replay. Use `--full-recording` to keep the complete payload of every sample.

Deduplication uses `(world, sequence)` within a single recording process. New worlds are automatically detected, so you can record across multiple consecutive matches in one process. The file is appended to, not overwritten; use a unique output path per recording run.

## 4. Safety and operating assumptions

- `ReadOnlyBridge.request` explicitly allows only `status`, `open`, `observe`, `map`, `close`; it cannot send `act` or `query` in this initial prototype.
- `open` and each `observe` check `information_policy == "player_view"` (the bridge also supports omniscience, which this client rejects).
- It does not use any external network or upload gameplay data. The coaching dashboard is served only on 127.0.0.1, rejects requests whose `Host` header is not local, and requires exclusive use of its port.
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
| `Port 8765 ... already in use` | Another coach is still running (check other terminals) or pass `--port`. |
| Dashboard stuck on STALE | The coach process is running but receives no observations; check its terminal for bridge errors. |
| Dashboard shows OFFLINE | The coach command stopped; restart it and reload the page. |
| No sound | Click **Enable sound** after the page loads (browsers block audio until a click) and check the volume slider / Mute. |

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

1. **Verify telemetry — done.** Villager, Town Center (training / researching / housed / idle), resource, and villager-queue signals were verified in live controlled tests; see [docs/telemetry.md](docs/telemetry.md) and `aoe2coach/telemetry.py`. Multiple TCs, later ages, and other civilizations are not yet verified.
2. **Shared analysis — done.** `aoe2coach/analysis.py` raises and clears alert episodes in game time: idle villagers (5 s), idle Town Center (1 s; training or researching counts as busy), housed (1 s), house-soon warnings, stockpiling (above the next age-up cost by 400 in Dark/Feudal, 800 later, for 20 s), and standard 22-pop Scouts build-order reminders. Validated in a live match; see [docs/coaching-rules.md](docs/coaching-rules.md).
3. **Local browser dashboard — done.** `aoe2-collect coach` runs one bridge session, the shared rules, an optional compact recording, and a dark large-text page at 127.0.0.1:8765 with grouped alerts, LIVE/PAUSED/STALE/OFFLINE status, resources with stockpile highlight, and the build-order checklist. A two-tone chime (volume slider, mute) sounds at most once every 2 s, plus a red screen-edge flash. Validated in a live match.
4. **Post-game summaries — done.** `aoe2-collect summary` (and `coach` on exit) replays a recording through the same `Coach` and writes an HTML report plus terminal text: idle-villager, Town Center idle, housed and stockpiling episodes, age-up times, build-order timings and villagers over time, with incomplete-data notes. Tests check parity with the live dashboard pipeline; a real 14-minute recording (436 MB) summarizes in about 5 s.

Possible next steps: verify multiple Town Centers and Castle/Imperial Age telemetry, suppress housed alerts at the 200 population cap, verify task-specific villager types (forager, hunter, miners) to check villager assignments, and add more build orders.

The configured 2 Hz is a polling target, not a delivery guarantee. Game speed and bridge latency affect when alerts arrive; measure a 1-game-second TC rule against game timestamps without promising 1-second wall-clock delivery. No automated gameplay, multiplayer use, cloud telemetry upload, or injected coaching overlay is planned.
