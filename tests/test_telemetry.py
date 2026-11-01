import copy
import json
from pathlib import Path

import pytest

from aoe2coach.telemetry import (
    TelemetryError, TownCenterState, classify_town_center, extract,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "observation_feudal_p1.json").read_text(encoding="utf-8"))
TC_ID = 3638


@pytest.fixture
def obs():
    return copy.deepcopy(FIXTURE)


def _entity(obs, entity_id):
    return next(e for e in obs["entities"] if e["id"] == entity_id)


def _villager_ids(obs):
    return [e["id"] for e in obs["entities"] if e.get("owner") == 1 and e["class_id"] == 904]


def test_extracts_real_feudal_observation(obs):
    state = extract(obs)
    assert (state.world, state.sequence, state.game_time_ms, state.player_id) == (2, 269333, 1426737, 1)
    assert state.current_age == 1
    assert state.housing_headroom == 14
    assert (state.resources.food, state.resources.wood, state.resources.stone, state.resources.gold) == (616, 405, 200, 193)
    assert len(state.villagers) == 15
    assert state.idle_villagers == ()
    assert [(tc.id, tc.state) for tc in state.town_centers] == [(TC_ID, TownCenterState.IDLE)]
    assert (state.villager_queue.count, state.villager_queue.queued) == (15, 0)


def test_villager_class_covers_task_variants(obs):
    type_names = {e["type_name"] for e in obs["entities"] if e.get("owner") == 1 and e["class_id"] == 904}
    assert {"Builder", "Farmer", "Lumberjack"} <= type_names
    assert len(extract(obs).villagers) == len(_villager_ids(obs))


def test_idle_villager_detected(obs):
    vid = _villager_ids(obs)[0]
    _entity(obs, vid).update(idle=True, action=-1)
    assert [v.id for v in extract(obs).idle_villagers] == [vid]


def test_garrisoned_villager_excluded(obs):
    vid = _villager_ids(obs)[0]
    _entity(obs, vid).update(garrison_object_id=TC_ID, idle=True)
    state = extract(obs)
    assert vid not in [v.id for v in state.villagers]
    assert state.idle_villagers == ()


def test_villager_without_idle_field_is_unknown_not_idle(obs):
    vid = _villager_ids(obs)[0]
    del _entity(obs, vid)["idle"]
    villager = next(v for v in extract(obs).villagers if v.id == vid)
    assert villager.idle is None
    assert villager not in extract(obs).idle_villagers


def test_ignores_other_owners_and_position_only_entities(obs):
    vid = _villager_ids(obs)[0]
    _entity(obs, vid)["owner"] = 2
    obs["entities"].append({"access": "position", "id": 999999, "type_id": 83, "class_id": 904, "owner": 1, "x": 1, "y": 1})
    ids = [v.id for v in extract(obs).villagers]
    assert vid not in ids and 999999 not in ids


@pytest.mark.parametrize("train_count, progress_type, progress_value, headroom, expected", [
    (2, 102, 45, 10, TownCenterState.TRAINING),     # probe 3 @ 641.6 s
    (0, 103, 23, 4, TownCenterState.RESEARCHING),   # Feudal Age, probe 5 @ 1270.6 s
    (1, 0, 0, 0, TownCenterState.HOUSED),           # probe 4 @ 979.0 s
    (0, 0, 0, 14, TownCenterState.IDLE),
])
def test_town_center_states_from_live_probes(obs, train_count, progress_type, progress_value, headroom, expected):
    _entity(obs, TC_ID).update(train_count=train_count, progress_type=progress_type, progress_value=progress_value)
    obs["player"]["housing_headroom"] = headroom
    assert extract(obs).town_centers[0].state is expected


@pytest.mark.parametrize("train_count, progress_type, headroom", [
    (1, 0, 5),          # queued but not started with room: transient, not idle
    (0, 999, 5),        # unrecognised progress type
    (None, 0, 5),
    (0, None, 5),
    (1, 0, None),       # housed cannot be confirmed without headroom
])
def test_unrecognised_town_center_state_is_unknown(train_count, progress_type, headroom):
    assert classify_town_center(train_count, progress_type, headroom) is TownCenterState.UNKNOWN


def test_town_center_foundation_excluded(obs):
    _entity(obs, TC_ID)["status"] = 0
    assert extract(obs).town_centers == ()


def test_resources_fall_back_to_facts(obs):
    del obs["player"]["attributes"]
    resources = extract(obs).resources
    assert (resources.food, resources.wood, resources.stone, resources.gold) == (616, 405, 200, 193)


def test_resources_missing_is_none(obs):
    del obs["player"]["attributes"]
    del obs["player"]["facts"]
    assert extract(obs).resources is None


def test_villager_queue_absent_without_object_types(obs):
    obs["object_types"] = []
    assert extract(obs).villager_queue is None


def test_villager_queue_reports_queued(obs):
    next(t for t in obs["object_types"] if t["id"] == 83)["count_with_queued"] = 17
    assert extract(obs).villager_queue.queued == 2


def test_rejects_omniscient(obs):
    obs["information_policy"] = "omniscient"
    with pytest.raises(TelemetryError, match="player_view"):
        extract(obs)


@pytest.mark.parametrize("field", ["world", "sequence", "game_time_ms"])
def test_rejects_missing_header(obs, field):
    del obs[field]
    with pytest.raises(TelemetryError, match=field):
        extract(obs)


def test_rejects_missing_player_id(obs):
    del obs["player"]["id"]
    with pytest.raises(TelemetryError, match="player.id"):
        extract(obs)
