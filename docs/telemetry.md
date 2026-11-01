# Telemetry field mappings

Verified against live **AoE2Control 1.1.5**, Agent Bridge V3, single-player, `player_view`, player 1 (Varangians, Dark → Feudal Age). The extractor lives in [`aoe2coach/telemetry.py`](../aoe2coach/telemetry.py); tests use a trimmed, sanitized real observation in [`tests/fixtures/observation_feudal_p1.json`](../tests/fixtures/observation_feudal_p1.json).

Times below are **game time**. At the default speed observed, game time ran ~1.6× wall-clock time.

## Villagers

| Rule | Evidence |
|---|---|
| Villager = owned entity with `class_id == 904` | `type_id` changes with the task (83 Villager male, 293 female, 590/592 Shepherd, Builder, Farmer, Lumberjack …); all share class 904. Sheep/goats (958), scouts (947) and buildings (903) are other classes. |
| Idle = `idle == true` | Idle IDs disappeared when villagers were given tasks (608–622 s) and reappeared when new villagers spawned without a rally point (e.g. 4208 idle ~13 s). Shepherds whose sheep were used up reported `idle: true` while `action` still held their last task (613), so `action` is **not** used. |
| Garrisoned villagers are excluded | The bridge documents that garrisoned units only report a few fields, so their idle state cannot be judged. |
| Missing/non-boolean `idle` → unknown, never idle | Prevents false alerts when `entity_fields` is filtered or the schema changes. |

## Town Center production

Only the TC part with `type_id == 109` reports production; the annex parts (618, 619, 620, 1649) stayed at zero throughout. Foundations (`status != 2`) are ignored. The TC entity's own `idle` flag stayed `true` while a villager was queued, so it is **not** used.

| State | Rule | Evidence |
|---|---|---|
| Training | `progress_type == 102` | `progress_value` rose 0→100 %; a villager took ~25 game-s; `train_count` equalled the queue size. |
| Researching | `progress_type == 103` | Loom (gold −50, ~25 s) and Feudal Age (food −500, ~130 s). The documented `researching` field stayed **0** during both, so it is not used. Use technology states to tell an age-up from other research (see Technologies). |
| Housed | `train_count > 0`, `progress_type == 0`, `housing_headroom <= 0` | Seen at queue start (623 s) and mid-queue (979 s); training resumed as soon as a house finished. |
| Idle | `train_count == 0` and `progress_type == 0` | Gaps of 5, 25, 6 and 30+ s observed between queues. |
| Unknown | anything else, including missing fields | Queued with room but not yet started, or an unrecognised `progress_type`. Never treated as idle. |

## Resources and population

| Value | Source |
|---|---|
| Food / wood / stone / gold | `player.attributes[0..3]` (`ResourceType` ids 0 food, 1 wood, 2 stone, 3 gold), truncated to integers. Falls back to `player.facts` `*_AMOUNT` (removed in protocol version 4). Both matched in every sample. |
| Housing headroom | `player.housing_headroom` |
| Villager queue | `object_types` entry `id == 83`: `count` and `count_with_queued` (includes all villager variants). Only present when `observe` requests `object_types: "relevant"` (~200 KB per response), so live analysis fetches it periodically and recordings do not store it. |

## Buildings and units

| Value | Source |
|---|---|
| Buildings | Owned entities with `category == 80`; `status` 2 finished, 0 foundation. Verified type IDs: House 70, Barracks 12, Mill 68, Stable 101, Lumber Camp 562, Town Center 109. Build-order checks match the entity `type_name` ("Lumber Camp", "Stable", ...), case-insensitive. |
| Unit counts | Owned entities with `category == 70` (falls back to `object_type`), counted by `type_id` and by `type_name`. Garrisoned units report no `type_name`, so their name comes from another unit of the same `type_id`. Scout Cavalry is 448. Villagers change `type_name` with their task (Lumberjack, Farmer, ...), so count villagers with `villager_count`, not by name. |
| Garrisoned villagers | Counted in `villager_count` but excluded from idle checks. |

## Technologies

Verified with a live probe (CONTROL 1.1.5) while Loom, Double-Bit Axe, Wheelbarrow, Bloodlines, Horse Collar and Gold Mining were researched.

| Finding | Detail |
|---|---|
| Request | `observe` with `technologies: "all"` adds about 40 KB (616 entries). The coach requests it every 4th poll. |
| Entry fields | `id`, `state`, `can_afford`, `cost`. **No names.** |
| States | 0 unavailable, 1 available, 2 researching, 3 researched. Each researched technology went 1 → 2 → 3. |
| Names | Mapped from ids in `telemetry.TECHNOLOGIES`. Each id was verified because its live `cost` matched the expected cost; 9 candidates that did not match (balance changes or other ids) were left out. |
| Age-ups | Feudal 101, Castle 102, Imperial 103. `state == 2` means advancing. The Town Center's `progress_type == 103` is also set while researching Loom or Wheelbarrow, so it alone cannot identify an age-up. |
| Recording | Compact recordings store `technology_states: {researched: [ids], researching: [ids]}` instead of the full list. |

## Not yet verified

- Multiple Town Centers, Castle/Imperial Age TC parts, and civilizations with different TC or villager types.
- Fishing ships, trade carts and other economic units (different classes; not villagers).
- Behaviour while the game is paused, at different game speeds, or in a replay.
