"""Pure extractors for Agent Bridge V3 observations.

Field mappings were verified against live AoE2Control 1.1.5 data; see
docs/telemetry.md for the evidence behind each constant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

VILLAGER_CLASS_ID = 904
VILLAGER_TYPE_ID = 83
TOWN_CENTER_TYPE_ID = 109
CATEGORY_UNIT = 70
CATEGORY_BUILDING = 80
STATUS_FOUNDATION = 0
STATUS_FINISHED = 2
PROGRESS_NONE = 0
PROGRESS_TRAINING = 102
PROGRESS_RESEARCHING = 103
# Order matches the Resources dataclass fields.
RESOURCE_IDS = {"food": 0, "wood": 1, "stone": 2, "gold": 3}
_RESOURCE_FACTS = {"food": "FOOD_AMOUNT", "wood": "WOOD_AMOUNT", "stone": "STONE_AMOUNT", "gold": "GOLD_AMOUNT"}
AGE_NAMES = {0: "Dark Age", 1: "Feudal Age", 2: "Castle Age", 3: "Imperial Age"}

# Technology `state` values observed live: 0 unavailable, 1 available, 2 researching, 3 researched.
TECH_RESEARCHING = 2
TECH_RESEARCHED = 3
AGE_TECHNOLOGIES = {101: 1, 102: 2, 103: 3}  # technology id -> age it reaches
# The bridge reports technologies by id only. Each id below was confirmed against live costs
# (and, for Loom, Double-Bit Axe, Wheelbarrow, Bloodlines, Horse Collar and Gold Mining, live research).
TECHNOLOGIES = {
    101: "Feudal Age", 102: "Castle Age", 103: "Imperial Age",
    22: "Loom", 8: "Town Watch", 280: "Town Patrol", 213: "Wheelbarrow", 249: "Hand Cart",
    202: "Double-Bit Axe", 203: "Bow Saw", 221: "Two-Man Saw",
    14: "Horse Collar", 13: "Heavy Plow", 12: "Crop Rotation", 55: "Gold Mining", 278: "Stone Mining",
    435: "Bloodlines", 39: "Husbandry", 199: "Fletching", 200: "Bodkin Arrow", 201: "Bracer",
    211: "Padded Archer Armor", 212: "Leather Archer Armor", 67: "Forging", 68: "Iron Casting",
    74: "Scale Mail Armor", 76: "Chain Mail Armor", 81: "Scale Barding Armor", 82: "Chain Barding Armor",
    222: "Man-at-Arms", 254: "Light Cavalry", 215: "Squires", 23: "Coinage", 17: "Banking", 15: "Guilds",
    48: "Caravan",
}


class TelemetryError(ValueError):
    """Observation lacks required fields or is not a player_view observation."""


class TownCenterState(str, Enum):
    IDLE = "idle"
    TRAINING = "training"
    RESEARCHING = "researching"
    HOUSED = "housed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Villager:
    id: int
    type_id: int
    idle: bool | None
    x: float | None
    y: float | None


@dataclass(frozen=True)
class TownCenter:
    id: int
    train_count: int | None
    progress_type: int | None
    progress_value: int | None
    state: TownCenterState
    x: float | None
    y: float | None


@dataclass(frozen=True)
class Resources:
    food: int
    wood: int
    stone: int
    gold: int


@dataclass(frozen=True)
class VillagerQueue:
    count: int
    count_with_queued: int

    @property
    def queued(self) -> int:
        return max(0, self.count_with_queued - self.count)


@dataclass(frozen=True)
class Building:
    id: int
    type_id: int
    finished: bool
    name: str = ""


def normalize_name(name: str) -> str:
    """Case- and whitespace-insensitive key for matching in-game type names."""
    return " ".join(name.split()).casefold()


@dataclass(frozen=True)
class PlayerState:
    world: int
    sequence: int
    game_time_ms: int
    player_id: int
    current_age: int | None
    housing_headroom: int | None
    resources: Resources | None
    villagers: tuple[Villager, ...]
    town_centers: tuple[TownCenter, ...]
    villager_queue: VillagerQueue | None
    buildings: tuple[Building, ...] = ()
    garrisoned_villagers: int = 0
    unit_counts: Mapping[int, int] = field(default_factory=dict, compare=False)
    # Owned units by normalized type name (see normalize_name).
    unit_names: Mapping[str, int] = field(default_factory=dict, compare=False)
    # Technology IDs by state; None when the observation did not include technologies.
    researched_ids: frozenset[int] | None = None
    researching_ids: frozenset[int] | None = None

    @property
    def idle_villagers(self) -> tuple[Villager, ...]:
        return tuple(v for v in self.villagers if v.idle is True)

    @property
    def villager_count(self) -> int:
        return len(self.villagers) + self.garrisoned_villagers

    @property
    def researching(self) -> bool:
        """A Town Center is researching something (an age-up, Loom, Wheelbarrow, ...)."""
        return any(tc.state is TownCenterState.RESEARCHING for tc in self.town_centers)

    @property
    def age_up_in_progress(self) -> bool:
        """Advancing to the next age. Exact with technology data; otherwise any TC research counts."""
        if self.researching_ids is not None:
            return bool(self.researching_ids & AGE_TECHNOLOGIES.keys())
        return self.researching

    def has_building(self, type_id: int, *, finished_only: bool = False) -> bool:
        return any(b.type_id == type_id and (b.finished or not finished_only) for b in self.buildings)

    def building_count(self, name: str, *, finished_only: bool = False) -> int:
        key = normalize_name(name)
        return sum(1 for b in self.buildings
                   if normalize_name(b.name) == key and (b.finished or not finished_only))

    def unit_count(self, name: str) -> int:
        return self.unit_names.get(normalize_name(name), 0)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def classify_town_center(train_count: int | None, progress_type: int | None,
                         housing_headroom: int | None) -> TownCenterState:
    """Classify TC production; anything not positively recognised is UNKNOWN (never IDLE)."""
    if train_count is None or progress_type is None:
        return TownCenterState.UNKNOWN
    if progress_type == PROGRESS_RESEARCHING:
        return TownCenterState.RESEARCHING
    if progress_type == PROGRESS_TRAINING:
        return TownCenterState.TRAINING
    if progress_type == PROGRESS_NONE:
        if train_count == 0:
            return TownCenterState.IDLE
        if train_count > 0 and housing_headroom is not None and housing_headroom <= 0:
            return TownCenterState.HOUSED
    return TownCenterState.UNKNOWN


def _resources(player: Mapping[str, Any]) -> Resources | None:
    amounts: list[int] = []
    attributes = player.get("attributes")
    if isinstance(attributes, list) and len(attributes) >= 4:
        for idx in RESOURCE_IDS.values():
            value = _number(attributes[idx])
            if value is None:
                break
            amounts.append(int(value))
    facts = player.get("facts")
    if len(amounts) != 4 and isinstance(facts, Mapping):
        amounts = []
        for fact in _RESOURCE_FACTS.values():
            value = _int(facts.get(fact))
            if value is None:
                break
            amounts.append(value)
    return Resources(*amounts) if len(amounts) == 4 else None


def _villager_queue(object_types: Any) -> VillagerQueue | None:
    if not isinstance(object_types, list):
        return None
    for entry in object_types:
        if isinstance(entry, Mapping) and entry.get("id") == VILLAGER_TYPE_ID:
            count, queued = _int(entry.get("count")), _int(entry.get("count_with_queued"))
            if count is not None and queued is not None:
                return VillagerQueue(count, queued)
    return None


def extract(observation: Mapping[str, Any]) -> PlayerState:
    """Extract the coaching-relevant state of the bridge's own player."""
    if observation.get("information_policy") != "player_view":
        raise TelemetryError("observation is not player_view")
    player = observation.get("player")
    if not isinstance(player, Mapping):
        raise TelemetryError("observation has no player object")
    world, sequence = _int(observation.get("world")), _int(observation.get("sequence"))
    game_time_ms, player_id = _int(observation.get("game_time_ms")), _int(player.get("id"))
    if world is None or sequence is None or game_time_ms is None or player_id is None:
        fields = {"world": world, "sequence": sequence, "game_time_ms": game_time_ms, "player.id": player_id}
        missing = ", ".join(name for name, value in fields.items() if value is None)
        raise TelemetryError(f"observation missing integer fields: {missing}")

    headroom = _int(player.get("housing_headroom"))
    villagers: list[Villager] = []
    town_centers: list[TownCenter] = []
    buildings: list[Building] = []
    unit_counts: dict[int, int] = {}
    type_names: dict[int, str] = {}
    garrisoned_villagers = 0
    entities = observation.get("entities")
    for entity in entities if isinstance(entities, list) else []:
        if not isinstance(entity, Mapping) or entity.get("access") != "full" or entity.get("owner") != player_id:
            continue
        entity_id, type_id = _int(entity.get("id")), _int(entity.get("type_id"))
        if entity_id is None or type_id is None:
            continue
        type_name = entity.get("type_name")
        if isinstance(type_name, str) and type_name:
            type_names.setdefault(type_id, type_name)
        category = _int(entity.get("category"))
        if category is None:
            category = _int(entity.get("object_type"))
        if category == CATEGORY_UNIT:
            unit_counts[type_id] = unit_counts.get(type_id, 0) + 1
        x, y = _number(entity.get("x")), _number(entity.get("y"))
        if entity.get("class_id") == VILLAGER_CLASS_ID:
            if entity.get("garrison_object_id") is not None:
                garrisoned_villagers += 1
                continue
            idle = entity.get("idle")
            villagers.append(Villager(entity_id, type_id, idle if isinstance(idle, bool) else None, x, y))
            continue
        if category == CATEGORY_BUILDING and entity.get("status") in (STATUS_FOUNDATION, STATUS_FINISHED):
            buildings.append(Building(entity_id, type_id, entity.get("status") == STATUS_FINISHED,
                                      type_name if isinstance(type_name, str) else ""))
        if type_id == TOWN_CENTER_TYPE_ID and entity.get("status") == STATUS_FINISHED:
            train_count, progress_type = _int(entity.get("train_count")), _int(entity.get("progress_type"))
            town_centers.append(TownCenter(
                entity_id, train_count, progress_type, _int(entity.get("progress_value")),
                classify_town_center(train_count, progress_type, headroom), x, y))

    unit_names: dict[str, int] = {}
    for type_id, count in unit_counts.items():
        if type_id in type_names:
            key = normalize_name(type_names[type_id])
            unit_names[key] = unit_names.get(key, 0) + count

    return PlayerState(
        world=world,
        sequence=sequence,
        game_time_ms=game_time_ms,
        player_id=player_id,
        current_age=_int(player.get("current_age")),
        housing_headroom=headroom,
        resources=_resources(player),
        villagers=tuple(villagers),
        town_centers=tuple(town_centers),
        villager_queue=_villager_queue(observation.get("object_types")),
        buildings=tuple(buildings),
        garrisoned_villagers=garrisoned_villagers,
        unit_counts=unit_counts,
        unit_names=unit_names,
        **_technology_states(observation),
    )


def _technology_states(observation: Mapping[str, Any]) -> dict[str, frozenset[int] | None]:
    """Researched / researching technology IDs from a full `technologies` list or its compact form.

    Both are None when the observation did not request technologies.
    """
    compact = observation.get("technology_states")
    if isinstance(compact, Mapping):
        done, active = compact.get("researched"), compact.get("researching")
        if isinstance(done, list) and isinstance(active, list):
            return {"researched_ids": frozenset(i for i in done if _int(i) is not None),
                    "researching_ids": frozenset(i for i in active if _int(i) is not None)}
    techs = observation.get("technologies")
    if not isinstance(techs, list) or not techs:
        return {"researched_ids": None, "researching_ids": None}
    done_ids, active_ids = set(), set()
    for tech in techs:
        if not isinstance(tech, Mapping):
            continue
        tech_id, tech_state = _int(tech.get("id")), _int(tech.get("state"))
        if tech_id is None:
            continue
        if tech_state == TECH_RESEARCHED:
            done_ids.add(tech_id)
        elif tech_state == TECH_RESEARCHING:
            active_ids.add(tech_id)
    return {"researched_ids": frozenset(done_ids), "researching_ids": frozenset(active_ids)}


def compact_technologies(technologies: Any) -> dict[str, list[int]] | None:
    """Small replacement for the ~40 KB `technologies` list, used in compact recordings."""
    states = _technology_states({"technologies": technologies})
    if states["researched_ids"] is None:
        return None
    return {"researched": sorted(states["researched_ids"]), "researching": sorted(states["researching_ids"])}
