# Coaching rules

Implemented in [`aoe2coach/analysis.py`](../aoe2coach/analysis.py) on top of the verified signals in [telemetry.md](telemetry.md). One `Coach` instance consumes a stream of `PlayerState` snapshots and emits `raised` / `cleared` events; the same rules serve live alerts and post-game analysis.

## Episodes

- Each alert is an **episode**: it is raised once when its condition has held for the threshold, and cleared when the condition stops (activity resumes, the entity disappears, or a build step is completed).
- All durations are **game time**. A paused game does not advance timers.
- A gap of more than 5 game-seconds between samples restarts pending timers, so unobserved time is never counted as idle.
- A new match (`world` changes) or game time going backwards clears every alert and build-order progress.
- Unknown states (missing fields, unrecognised TC progress) never raise idle alerts.

## Alerts

Values below are the defaults; each threshold, on/off switch and sound can be changed in the dashboard settings (`data\settings.json`). Changes apply from the next observation; disabling an alert clears it.

| Alert | Condition | Default threshold | Default sound |
|---|---|---|---|
| Idle villager | Villager `idle == true` (one alert per villager) | 5 s | yes |
| Town Center idle | TC not training, researching, or housed. Message says to build a house when headroom is 0 | 1 s | yes |
| Housed | A TC has villagers queued but `housing_headroom` is 0 | 1 s | yes |
| House soon | Headroom is 1–2 (setting) and no house foundation exists | immediate | no |
| Stockpiling | A resource exceeds the next age-up cost by more than 400 (Dark/Feudal) or 800 (Castle/Imperial); the age-up allowance can be turned off | 20 s | no |
| Build order | Reminder step is due but not done (see below) | per step | no |

Next age-up costs used for the stockpile allowance: Feudal 500 food; Castle 800 food + 200 gold; Imperial 1000 food + 800 gold. Civilization discounts are not modelled.

## Build orders

The selected build order (see [build-orders.md](build-orders.md)) supplies the reminders and the training view. Changing it during a match takes effect when the next match starts; before any match is seen it applies immediately. Once a step is done it stays done for the match.

Age-up detection uses the technology list when available (Feudal/Castle/Imperial Age `state == 2`); without it any Town Center research counts, so Loom or Wheelbarrow could be mistaken for an age-up. Technology states are fetched every 4th poll and reused between fetches.

### Built-in: standard 22-pop Scouts

[`scouts-22-pop.toml`](../aoe2coach/builds/scouts-22-pop.toml) reproduces the original hard-coded rules (a test compares them on every realistic Dark/Feudal combination of villagers, buildings and TC research):

| Step | Due when | Done when | Grace |
|---|---|---|---|
| Lumber camp | 8+ villagers | Lumber camp exists (foundation counts) | 10 s |
| Mill | 13+ villagers | Mill exists | 10 s |
| Click Feudal | Dark Age, 21+ villagers (22 pop with the scout), not advancing | Feudal Age reached | 3 s |
| Barracks | Advancing with 21+ villagers, or Feudal reached | Barracks exists | 15 s |
| Stable | Feudal reached and barracks finished | Stable exists | 10 s |
| Scouts | Stable finished | 4+ Scout Cavalry (starting scout + 3) | 30 s |

Villager assignments are guidance only, because task-specific villager types are not verified.

## Live validation

A 2-minute run on a fresh match (CONTROL 1.1.5) stayed silent while the TC trained continuously, raised a TC-idle alert when production stopped after villager 7 (cleared after 77 s), and raised then cleared six idle-villager alerts when a group finished building.

## Known limitations

- Several villagers idling together raise one alert each; the dashboard groups them into one line and plays at most one chime per configured gap (default 2 s).
- At the 200 population cap, housed / TC-idle alerts are not suppressed yet.
- Multiple Town Centers each raise their own idle alert; later ages and other civilizations are not yet verified.
