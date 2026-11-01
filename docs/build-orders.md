# Writing build orders

The coach trains you on one build order at a time. Pick it in the dashboard (**⚙ Settings → Build order**); a new choice starts with the next match.

- **Built-in:** `22-pop Scouts (generic)`, defined in [`aoe2coach/builds/scouts-22-pop.toml`](../aoe2coach/builds/scouts-22-pop.toml).
- **Yours:** any `.toml` file in `data\build_orders\`. That folder is Git-ignored, so builds you transcribe from paid guides stay on your PC. Do not move them into the repository.

Start from [`aoe2coach/builds/TEMPLATE.toml`](../aoe2coach/builds/TEMPLATE.toml):

```powershell
Copy-Item aoe2coach\builds\TEMPLATE.toml data\build_orders\my-build.toml
.\.venv\Scripts\python.exe -m aoe2coach builds
```

**Confirmation:** `builds` lists your build with its step and check counts. Errors name the file, step number and problem; the coach skips that file and keeps running. Reopen the dashboard settings to see new or edited files.

## File format (version 1)

| Key | Required | Meaning |
|---|---|---|
| `format` | yes | Always `1`. |
| `id` | yes | Unique id: lowercase letters, digits, `-`, `_`. Saved in settings and recordings. |
| `name` | yes | Shown in the dashboard and summaries. |
| `civilizations`, `description`, `source` | no | Shown in the build preview. |
| `[[steps]]` | yes | One table per step, in the order you play them. |

### Step keys

| Key | Meaning |
|---|---|
| `villagers` | Villager count at which the step applies (0-200). Use the total villager count, not the number sent. |
| `age` | `dark`, `feudal`, `castle`, `imperial`. Defaults to the previous step's age. |
| `task`, `note` | Text shown on the dashboard. |
| `id` | Optional name for reminders and summaries (default `step-N`). |
| `grace_s` | Seconds (game time) a check may be late before a reminder appears. Default 10. |
| `while_advancing` | `true`: due only once the age-up to the next age is in progress. |
| `requires` | Building name(s) that must be **finished** before the step is due, e.g. `["Barracks"]`. |

### Checks (optional, all must pass)

| Check | Done when |
|---|---|
| `build = "Mill"` or `{ name, count, finished }` | You own at least `count` of that building. Foundations count unless `finished = true`. Counts are totals, so a second lumber camp is `count = 2`. |
| `train = "Spearman"` or `{ name, count }` | You have at least `count` of that unit alive (including garrisoned units and the starting scout). |
| `research = "Loom"` or a numeric id | The technology is researched. |
| `age_up = "feudal"` | That age is reached. No reminder while the age-up is in progress. Put it in its own step. |
| `reach_age = "castle"` | That age is reached. |

Steps without checks are guidance: they are shown as **Now / Next** but never remind you.

## When steps are current and due

- **Current step:** the last step whose age is below yours, or in your age with `villagers` at or below your count. Reaching a new age makes its first step current even if you have fewer villagers than planned.
- **Due check:** in its age with enough villagers, or you are already in a later age. If not done within `grace_s`, a silent reminder appears (sound is configurable). Done checks stay done for the match.

## Names the coach recognizes

Building and unit names match the in-game English names, ignoring case and extra spaces. Common shorthands work: `TC`, `range`, `scouts`, `maa`, `skirms`, `spear`. Unrecognized names load with a warning, because they only match the exact in-game name.

Technologies are reported by the game as numeric ids. These names have ids verified against live game costs:

Feudal Age, Castle Age, Imperial Age, Loom, Town Watch, Town Patrol, Wheelbarrow, Hand Cart, Double-Bit Axe, Bow Saw, Two-Man Saw, Horse Collar, Heavy Plow, Crop Rotation, Gold Mining, Stone Mining, Bloodlines, Husbandry, Fletching, Bodkin Arrow, Bracer, Padded Archer Armor, Leather Archer Armor, Forging, Iron Casting, Scale Mail Armor, Chain Mail Armor, Scale Barding Armor, Chain Barding Armor, Man-at-Arms, Light Cavalry, Squires, Coinage, Banking, Guilds, Caravan.

For anything else, use the numeric id: `research = 199`.

## What cannot be checked

The game data does not say which resource a villager gathers, so "6 to sheep" or "move 4 to berries" stay guidance. Villager counts at a given time, farm counts, and landing times are also not checked. Idle villagers, an idle Town Center, and housing are covered by the regular alerts.
