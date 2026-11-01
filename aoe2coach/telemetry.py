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

    @property
    def idle_villagers(self) -> tuple[Villager, ...]:
        return tuple(v for v in self.villagers if v.idle is True)

    @property
    def villager_count(self) -> int:
        return len(self.villagers) + self.garrisoned_villagers

    def has_building(self, type_id: int, *, finished_only: bool = False) -> bool:
        return any(b.type_id == type_id and (b.finished or not finished_only) for b in self.buildings)


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
    garrisoned_villagers = 0
    entities = observation.get("entities")
    for entity in entities if isinstance(entities, list) else []:
        if not isinstance(entity, Mapping) or entity.get("access") != "full" or entity.get("owner") != player_id:
            continue
        entity_id, type_id = _int(entity.get("id")), _int(entity.get("type_id"))
        if entity_id is None or type_id is None:
            continue
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
            buildings.append(Building(entity_id, type_id, entity.get("status") == STATUS_FINISHED))
        if type_id == TOWN_CENTER_TYPE_ID and entity.get("status") == STATUS_FINISHED:
            train_count, progress_type = _int(entity.get("train_count")), _int(entity.get("progress_type"))
            town_centers.append(TownCenter(
                entity_id, train_count, progress_type, _int(entity.get("progress_value")),
                classify_town_center(train_count, progress_type, headroom), x, y))

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
    )
