# Coaching rules

Implemented in [`aoe2coach/analysis.py`](../aoe2coach/analysis.py) on top of the verified signals in [telemetry.md](telemetry.md). One `Coach` instance consumes a stream of `PlayerState` snapshots and emits `raised` / `cleared` events; the same rules serve live alerts and post-game analysis.

## Episodes

- Each alert is an **episode**: it is raised once when its condition has held for the threshold, and cleared when the condition stops (activity resumes, the entity disappears, or a build step is completed).
- All durations are **game time**. A paused game does not advance timers.
- A gap of more than 5 game-seconds between samples restarts pending timers, so unobserved time is never counted as idle.
- A new match (`world` changes) or game time going backwards clears every alert and build-order progress.
- Unknown states (missing fields, unrecognised TC progress) never raise idle alerts.

## Alerts

| Alert | Condition | Threshold | Sound |
|---|---|---|---|
| Idle villager | Villager `idle == true` (one alert per villager) | 5 s | yes |
| Town Center idle | TC not training, researching, or housed. Message says to build a house when headroom is 0 | 1 s | yes |
| Housed | A TC has villagers queued but `housing_headroom` is 0 | 1 s | yes |
| House soon | Headroom is 1–2 and no house foundation exists | immediate | no |
| Stockpiling | A resource exceeds the next age-up cost by more than 400 (Dark/Feudal) or 800 (Castle/Imperial) | 20 s | no |
| Build order | Reminder step is due but not done (see below) | per step | no |

Next age-up costs used for the stockpile allowance: Feudal 500 food; Castle 800 food + 200 gold; Imperial 1000 food + 800 gold. Civilization discounts are not modelled.

## Build order: standard 22-pop Scouts

Reminders only use signals telemetry can confirm (villager count, age, TC research, buildings incl. foundations, unit counts). Once a step is done it stays done for the match.

| Step | Due when | Done when | Grace |
|---|---|---|---|
| Lumber camp | 8+ villagers | Lumber camp exists (foundation counts) | 10 s |
| Mill | 13+ villagers | Mill exists | 10 s |
| Click Feudal | Dark Age, 21+ villagers (22 pop with the scout), TC not researching | Feudal Age reached | 3 s |
| Barracks | Feudal research running with 21+ villagers, or Feudal reached | Barracks exists | 15 s |
| Stable | Feudal reached and barracks finished | Stable exists | 10 s |
| Scouts | Stable finished | 4+ Scout Cavalry (starting scout + 3) | 30 s |

Villager assignments (shown as guidance, not checked, because task-specific villager types such as forager/hunter are not yet verified): 1–6 sheep (first villagers build 2 houses), 7–10 wood + lumber camp, 11 lure boar, 12–15 berries + mill, 16–17 second boar/sheep, 18–20 wood, 21 farm, then click Feudal.

## Live validation

A 2-minute run on a fresh match (CONTROL 1.1.5) stayed silent while the TC trained continuously, raised a TC-idle alert when production stopped after villager 7 (cleared after 77 s), and raised then cleared six idle-villager alerts when a group finished building.

## Known limitations

- Several villagers idling together raise one alert each; the dashboard should play at most one sound per burst.
- At the 200 population cap, housed / TC-idle alerts are not suppressed yet.
- Multiple Town Centers each raise their own idle alert; later ages and other civilizations are not yet verified.
