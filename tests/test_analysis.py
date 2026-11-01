import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from aoe2coach.analysis import (
    BARRACKS, HOUSE, LUMBER_CAMP, MAX_GAP_MS, MILL, SCOUT_CAVALRY, STABLE, AlertKind, Coach, next_villager_task,
)
from aoe2coach.telemetry import (
    Building, PlayerState, Resources, TownCenter, TownCenterState, Villager, extract,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "observation_feudal_p1.json").read_text(encoding="utf-8"))


def tc(state=TownCenterState.TRAINING, tc_id=1):
    return TownCenter(tc_id, 1, 102, 50, state, 0.0, 0.0)


def villagers(n, idle=()):
    return tuple(Villager(i, 83, i in idle, 0.0, 0.0) for i in range(100, 100 + n))


def make(t, *, world=1, age=0, vills=None, tcs=None, headroom=5, food=0, wood=0, stone=0, gold=0,
         buildings=(), units=None):
    return PlayerState(
        world=world, sequence=t, game_time_ms=t, player_id=1, current_age=age, housing_headroom=headroom,
        resources=Resources(food, wood, stone, gold),
        villagers=villagers(3) if vills is None else vills,
        town_centers=(tc(),) if tcs is None else tcs,
        villager_queue=None, buildings=tuple(buildings), unit_counts=units or {},
    )


def kinds(events, action="raised"):
    return [(e.alert.kind, e.alert.subject) for e in events if e.action == action]


def run(coach, states):
    events = []
    for s in states:
        events += coach.update(s)
    return events


def hold(coach, start, end, step=500, **kwargs):
    """Feed steady samples (as the live collector does) from start to end inclusive."""
    times = list(range(start, end, step)) + [end]
    return run(coach, [make(t, **kwargs) for t in times])


# --- villager idle (5 game-seconds) -------------------------------------------------

def test_villager_idle_threshold_exact():
    coach = Coach(build_order=())
    idle = villagers(3, idle={100})
    assert coach.update(make(10_000, vills=idle)) == []
    assert coach.update(make(14_999, vills=idle)) == []
    assert kinds(coach.update(make(15_000, vills=idle))) == [(AlertKind.VILLAGER_IDLE, "villager:100")]
    assert coach.update(make(16_000, vills=idle)) == []  # one alert per episode


def test_villager_idle_clears_and_rearms():
    coach = Coach(build_order=())
    idle, busy = villagers(3, idle={100}), villagers(3)
    run(coach, [make(0, vills=idle), make(5_000, vills=idle)])
    cleared = coach.update(make(6_000, vills=busy))
    assert kinds(cleared, "cleared") == [(AlertKind.VILLAGER_IDLE, "villager:100")]
    assert cleared[0].duration_ms == 6_000
    assert coach.active_alerts == ()
    assert run(coach, [make(7_000, vills=idle), make(11_999, vills=idle)]) == []
    assert kinds(coach.update(make(12_000, vills=idle))) == [(AlertKind.VILLAGER_IDLE, "villager:100")]


def test_short_idle_below_threshold_never_alerts():
    coach = Coach(build_order=())
    events = run(coach, [make(0, vills=villagers(3, idle={100})), make(4_400, vills=villagers(3, idle={100})),
                         make(5_000, vills=villagers(3))])
    assert events == []


def test_villager_removed_clears_alert():
    coach = Coach(build_order=())
    run(coach, [make(0, vills=villagers(3, idle={100})), make(5_000, vills=villagers(3, idle={100}))])
    events = coach.update(make(6_000, vills=()))
    assert kinds(events, "cleared") == [(AlertKind.VILLAGER_IDLE, "villager:100")]


def test_unknown_idle_state_does_not_alert():
    unknown = (Villager(100, 83, None, 0.0, 0.0),)
    assert run(Coach(build_order=()), [make(0, vills=unknown), make(60_000, vills=unknown)]) == []


def test_multiple_idle_villagers_alert_individually():
    coach = Coach(build_order=())
    idle = villagers(3, idle={100, 101})
    events = run(coach, [make(0, vills=idle), make(5_000, vills=idle)])
    assert sorted(kinds(events)) == [(AlertKind.VILLAGER_IDLE, "villager:100"), (AlertKind.VILLAGER_IDLE, "villager:101")]


# --- Town Center (1 game-second) ----------------------------------------------------

def test_tc_idle_threshold_exact():
    coach = Coach(build_order=())
    idle_tc = (tc(TownCenterState.IDLE),)
    assert coach.update(make(0, tcs=idle_tc)) == []
    assert coach.update(make(999, tcs=idle_tc)) == []
    events = coach.update(make(1_000, tcs=idle_tc))
    assert kinds(events) == [(AlertKind.TC_IDLE, "tc:1")]
    assert events[0].alert.sound is True


@pytest.mark.parametrize("busy", [TownCenterState.TRAINING, TownCenterState.RESEARCHING, TownCenterState.UNKNOWN])
def test_busy_or_unknown_tc_never_idle(busy):
    assert run(Coach(build_order=()), [make(0, tcs=(tc(busy),)), make(30_000, tcs=(tc(busy),))]) == []


def test_tc_resuming_production_clears_alert():
    coach = Coach(build_order=())
    run(coach, [make(0, tcs=(tc(TownCenterState.IDLE),)), make(1_000, tcs=(tc(TownCenterState.IDLE),))])
    assert kinds(coach.update(make(2_000)), "cleared") == [(AlertKind.TC_IDLE, "tc:1")]


def test_idle_tc_while_housed_says_build_house():
    coach = Coach(build_order=())
    events = run(coach, [make(0, tcs=(tc(TownCenterState.IDLE),), headroom=0),
                         make(1_000, tcs=(tc(TownCenterState.IDLE),), headroom=0)])
    assert "build a house" in events[0].alert.message


def test_housed_alert_separate_from_tc_idle():
    coach = Coach(build_order=())
    housed = (tc(TownCenterState.HOUSED),)
    events = run(coach, [make(0, tcs=housed, headroom=0), make(1_000, tcs=housed, headroom=0)])
    assert kinds(events) == [(AlertKind.HOUSED, "player")]


def test_house_soon_warning_suppressed_by_foundation():
    coach = Coach(build_order=())
    assert kinds(coach.update(make(0, headroom=2))) == [(AlertKind.HOUSE_SOON, "player")]
    foundation = [Building(9, HOUSE, False)]
    assert kinds(coach.update(make(1_000, headroom=2, buildings=foundation)), "cleared") == [(AlertKind.HOUSE_SOON, "player")]
    assert Coach(build_order=()).update(make(0, headroom=3)) == []


# --- stockpiling (20 game-seconds, excess above next age-up) ------------------------

def test_stockpile_threshold_and_duration():
    coach = Coach(build_order=())
    assert hold(coach, 0, 19_999, wood=401) == []
    assert kinds(coach.update(make(20_000, wood=401))) == [(AlertKind.STOCKPILE, "wood")]


def test_stockpile_exempts_age_up_savings():
    assert hold(Coach(build_order=()), 0, 30_000, food=900) == []  # 500 Feudal cost + 400
    assert kinds(hold(Coach(build_order=()), 0, 20_000, food=901)) == [(AlertKind.STOCKPILE, "food")]


def test_stockpile_later_ages_use_800():
    assert hold(Coach(build_order=()), 0, 30_000, age=2, stone=800) == []
    assert kinds(hold(Coach(build_order=()), 0, 20_000, age=2, stone=801)) == [(AlertKind.STOCKPILE, "stone")]


def test_stockpile_dropping_below_limit_resets():
    coach = Coach(build_order=())
    assert hold(coach, 0, 15_000, wood=500) == []
    assert coach.update(make(15_500, wood=100)) == []
    assert hold(coach, 16_000, 35_999, wood=500) == []
    assert kinds(coach.update(make(36_000, wood=500))) == [(AlertKind.STOCKPILE, "wood")]


# --- timing robustness --------------------------------------------------------------

def test_paused_game_time_does_not_advance_episode():
    coach = Coach(build_order=())
    idle = villagers(1, idle={100})
    assert run(coach, [make(1_000, vills=idle)] * 50) == []


def test_observation_gap_restarts_pending_timers():
    coach = Coach(build_order=())
    idle = villagers(1, idle={100})
    coach.update(make(0, vills=idle))
    assert coach.update(make(MAX_GAP_MS + 1, vills=idle)) == []
    assert coach.update(make(MAX_GAP_MS + 4_999, vills=idle)) == []
    assert kinds(coach.update(make(2 * MAX_GAP_MS + 1, vills=idle))) == [(AlertKind.VILLAGER_IDLE, "villager:100")]


def test_new_match_clears_alerts_and_state():
    coach = Coach(build_order=())
    idle = villagers(1, idle={100})
    run(coach, [make(0, vills=idle), make(5_000, vills=idle)])
    events = coach.update(make(1_000, world=2, vills=idle))
    assert kinds(events, "cleared") == [(AlertKind.VILLAGER_IDLE, "villager:100")]
    assert events[0].world == 1
    assert coach.active_alerts == ()
    assert kinds(coach.update(make(6_000, world=2, vills=idle))) == [(AlertKind.VILLAGER_IDLE, "villager:100")]


# --- 22-pop Scouts build order ------------------------------------------------------

def test_lumber_camp_reminder_after_grace_and_cleared_by_foundation():
    coach = Coach()
    assert hold(coach, 0, 9_999, vills=villagers(8)) == []
    assert (AlertKind.BUILD_ORDER, "lumber_camp") in kinds(coach.update(make(10_000, vills=villagers(8))))
    events = coach.update(make(10_500, vills=villagers(8), buildings=[Building(1, LUMBER_CAMP, False)]))
    assert (AlertKind.BUILD_ORDER, "lumber_camp") in kinds(events, "cleared")
    # done steps stay done even if the camp disappears later
    assert (AlertKind.BUILD_ORDER, "lumber_camp") not in kinds(hold(coach, 11_000, 30_000, vills=villagers(8)))


def test_click_feudal_reminder_hidden_while_researching():
    base = [Building(1, LUMBER_CAMP, True), Building(2, MILL, True)]
    coach = Coach()
    events = hold(coach, 0, 3_000, vills=villagers(21), buildings=base)
    assert (AlertKind.BUILD_ORDER, "click_feudal") in kinds(events)
    researching = (tc(TownCenterState.RESEARCHING),)
    events = coach.update(make(3_500, vills=villagers(21), buildings=base, tcs=researching))
    assert (AlertKind.BUILD_ORDER, "click_feudal") in kinds(events, "cleared")
    # during the age-up the barracks reminder becomes due
    events = hold(coach, 4_000, 18_500, vills=villagers(21), buildings=base, tcs=researching)
    assert (AlertKind.BUILD_ORDER, "barracks") in kinds(events)


def test_feudal_stable_and_scouts_sequence():
    built = [Building(1, LUMBER_CAMP, True), Building(2, MILL, True), Building(3, BARRACKS, True)]
    coach = Coach()
    events = hold(coach, 0, 10_000, age=1, vills=villagers(22), buildings=built)
    assert (AlertKind.BUILD_ORDER, "stable") in kinds(events)
    with_stable = built + [Building(4, STABLE, True)]
    events = hold(coach, 10_500, 40_500, age=1, vills=villagers(22), buildings=with_stable, units={SCOUT_CAVALRY: 1})
    assert (AlertKind.BUILD_ORDER, "stable") in kinds(events, "cleared")
    assert (AlertKind.BUILD_ORDER, "scouts") in kinds(events)
    events = coach.update(make(41_000, age=1, vills=villagers(22), buildings=with_stable, units={SCOUT_CAVALRY: 4}))
    assert (AlertKind.BUILD_ORDER, "scouts") in kinds(events, "cleared")


def test_build_order_alerts_are_silent():
    events = hold(Coach(), 0, 10_000, vills=villagers(8))
    assert any(e.alert.kind is AlertKind.BUILD_ORDER for e in events)
    assert all(not e.alert.sound for e in events if e.alert.kind is AlertKind.BUILD_ORDER)


@pytest.mark.parametrize("count, expected", [(0, "Sheep"), (6, "Wood"), (10, "boar"), (11, "Berries"),
                                             (20, "farm"), (21, "Feudal")])
def test_next_villager_task(count, expected):
    assert expected in next_villager_task(count)


# --- real fixture -------------------------------------------------------------------

def test_real_feudal_fixture_through_coach():
    state = extract(copy.deepcopy(FIXTURE))
    coach = Coach()
    t0 = state.game_time_ms
    events = run(coach, [replace(state, game_time_ms=t0 + dt) for dt in range(0, 30_001, 500)])
    raised = kinds(events)
    # Fixture: Feudal, finished barracks, stable foundation, idle TC; 405 wood > 400 with no wood in the Castle cost.
    assert (AlertKind.TC_IDLE, "tc:3638") in raised
    assert (AlertKind.BUILD_ORDER, "stable") not in raised  # foundation counts as started
    assert (AlertKind.BUILD_ORDER, "scouts") not in raised  # stable not finished yet
    assert [s for k, s in raised if k is AlertKind.STOCKPILE] == ["wood"]  # 616 food < 800 + 400
    assert all(k is not AlertKind.VILLAGER_IDLE for k, _ in raised)
