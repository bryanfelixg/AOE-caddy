import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from aoe2coach.analysis import MAX_GAP_MS, AlertKind, BuildStep, Coach
from aoe2coach.build_orders import BuildOrderError, default_build, parse
from aoe2coach.settings import Settings, from_dict
from aoe2coach.telemetry import (
    Building, PlayerState, Resources, TownCenter, TownCenterState, Villager, extract, normalize_name,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "observation_feudal_p1.json").read_text(encoding="utf-8"))
LUMBER_CAMP, MILL, BARRACKS, STABLE, HOUSE = "Lumber Camp", "Mill", "Barracks", "Stable", "House"
SCOUT_CAVALRY = "Scout Cavalry"
TYPE = {LUMBER_CAMP: 562, MILL: 68, BARRACKS: 12, STABLE: 101, HOUSE: 70}


def tc(state=TownCenterState.TRAINING, tc_id=1):
    return TownCenter(tc_id, 1, 102, 50, state, 0.0, 0.0)


def villagers(n, idle=()):
    return tuple(Villager(i, 83, i in idle, 0.0, 0.0) for i in range(100, 100 + n))


def make(t, *, world=1, age=0, vills=None, tcs=None, headroom=5, food=0, wood=0, stone=0, gold=0,
         buildings=(), units=None, researched=None, researching=None):
    return PlayerState(
        world=world, sequence=t, game_time_ms=t, player_id=1, current_age=age, housing_headroom=headroom,
        resources=Resources(food, wood, stone, gold),
        villagers=villagers(3) if vills is None else vills,
        town_centers=(tc(),) if tcs is None else tcs,
        villager_queue=None, buildings=tuple(buildings),
        unit_names={normalize_name(k): v for k, v in (units or {}).items()},
        researched_ids=None if researched is None else frozenset(researched),
        researching_ids=None if researching is None else frozenset(researching),
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
    foundation = [Building(9, TYPE[HOUSE], False, HOUSE)]
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
    events = coach.update(make(10_500, vills=villagers(8), buildings=[Building(1, TYPE[LUMBER_CAMP], False, LUMBER_CAMP)]))
    assert (AlertKind.BUILD_ORDER, "lumber_camp") in kinds(events, "cleared")
    # done steps stay done even if the camp disappears later
    assert (AlertKind.BUILD_ORDER, "lumber_camp") not in kinds(hold(coach, 11_000, 30_000, vills=villagers(8)))


def test_click_feudal_reminder_hidden_while_researching():
    base = [Building(1, TYPE[LUMBER_CAMP], True, LUMBER_CAMP), Building(2, TYPE[MILL], True, MILL)]
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
    built = [Building(1, TYPE[LUMBER_CAMP], True, LUMBER_CAMP), Building(2, TYPE[MILL], True, MILL), Building(3, TYPE[BARRACKS], True, BARRACKS)]
    coach = Coach()
    events = hold(coach, 0, 10_000, age=1, vills=villagers(22), buildings=built)
    assert (AlertKind.BUILD_ORDER, "stable") in kinds(events)
    with_stable = built + [Building(4, TYPE[STABLE], True, STABLE)]
    events = hold(coach, 10_500, 40_500, age=1, vills=villagers(22), buildings=with_stable, units={SCOUT_CAVALRY: 1})
    assert (AlertKind.BUILD_ORDER, "stable") in kinds(events, "cleared")
    assert (AlertKind.BUILD_ORDER, "scouts") in kinds(events)
    events = coach.update(make(41_000, age=1, vills=villagers(22), buildings=with_stable, units={SCOUT_CAVALRY: 4}))
    assert (AlertKind.BUILD_ORDER, "scouts") in kinds(events, "cleared")


def test_build_order_alerts_are_silent():
    events = hold(Coach(), 0, 10_000, vills=villagers(8))
    assert any(e.alert.kind is AlertKind.BUILD_ORDER for e in events)
    assert all(not e.alert.sound for e in events if e.alert.kind is AlertKind.BUILD_ORDER)


@pytest.mark.parametrize("count, age, current, upcoming", [
    (0, 0, None, "Villagers 1-6"),
    (6, 0, "Villagers 1-6", "Villagers 7-10"),
    (10, 0, "Build a lumber camp", "Villager 11"),
    (20, 0, "Villagers 18-20", "22 pop"),
    (21, 1, "Train 3 scout cavalry", None),
    (15, 1, "Feudal Age: farms", "Feudal Age: build a stable"),
])
def test_guidance_follows_villagers_and_age(count, age, current, upcoming):
    cur, nxt = Coach().guidance(make(0, age=age, vills=villagers(count)))
    if current is None:
        assert cur is None
    else:
        assert cur.task.startswith(current)
    if upcoming is None:
        assert nxt is None
    else:
        assert nxt.task.startswith(upcoming)


# --- parity with the original hard-coded 22-pop Scouts steps -------------------------

def _old_researching(s):
    return any(t.state is TownCenterState.RESEARCHING for t in s.town_centers)


OLD_22_POP = {
    "lumber_camp": (lambda s: s.villager_count >= 8, lambda s: s.has_building(562), 10_000),
    "mill": (lambda s: s.villager_count >= 13, lambda s: s.has_building(68), 10_000),
    "click_feudal": (lambda s: s.current_age == 0 and s.villager_count >= 21 and not _old_researching(s),
                     lambda s: (s.current_age or 0) >= 1, 3_000),
    "barracks": (lambda s: (s.current_age == 0 and s.villager_count >= 21 and _old_researching(s))
                 or (s.current_age or 0) >= 1, lambda s: s.has_building(12), 15_000),
    "stable": (lambda s: (s.current_age or 0) >= 1 and s.has_building(12, finished_only=True),
               lambda s: s.has_building(101), 10_000),
    "scouts": (lambda s: s.has_building(101, finished_only=True),
               lambda s: s.unit_count("Scout Cavalry") >= 4, 30_000),
}


def _realistic_states():
    """Dark Age with 0-25 villagers, Feudal with 21-30 (the build reaches Feudal at 21)."""
    names = [LUMBER_CAMP, MILL, BARRACKS, STABLE]
    for age, counts in ((0, range(0, 26)), (1, range(21, 31))):
        for count in counts:
            for researching in (False, True):
                for mask in range(3 ** len(names)):
                    built, m = [], mask
                    for i, name in enumerate(names):
                        m, kind = divmod(m, 3)
                        if kind:
                            built.append(Building(i, TYPE[name], kind == 2, name))
                    if age == 0 and any(b.name == STABLE and b.finished for b in built):
                        continue  # a finished stable needs Feudal Age
                    for scouts in (1, 4):
                        yield make(0, age=age, vills=villagers(count), buildings=built, units={SCOUT_CAVALRY: scouts},
                                   tcs=(tc(TownCenterState.RESEARCHING if researching else TownCenterState.TRAINING),))


def test_toml_22_pop_matches_original_steps():
    steps = {s.id: s for s in default_build().checks}
    assert list(steps) == list(OLD_22_POP)
    checked = 0
    for state in _realistic_states():
        for step_id, (due, done, grace) in OLD_22_POP.items():
            new = steps[step_id]
            assert new.grace_ms == grace
            assert new.done(state) == done(state), (step_id, state)
            if not done(state):
                assert new.due(state) == due(state), (step_id, state)
        checked += 1
    assert checked > 8_000


# --- settings -------------------------------------------------------------------------

def test_thresholds_come_from_settings():
    settings = from_dict({"alerts": {"villager_idle": {"threshold_s": 2}, "tc_idle": {"enabled": False}}})
    coach = Coach(build_order=(), settings=settings)
    idle = villagers(1, idle={100})
    events = hold(coach, 0, 2_000, vills=idle, tcs=(tc(TownCenterState.IDLE),))
    assert kinds(events) == [(AlertKind.VILLAGER_IDLE, "villager:100")]


def test_disabling_an_alert_live_clears_it_and_sound_follows_settings():
    coach = Coach(build_order=())
    hold(coach, 0, 1_000, tcs=(tc(TownCenterState.IDLE),))
    assert [a.sound for a in coach.active_alerts] == [True]
    coach.apply_settings(from_dict({"alerts": {"tc_idle": {"enabled": False}}}))
    assert kinds(coach.update(make(1_500, tcs=(tc(TownCenterState.IDLE),))), "cleared") == [(AlertKind.TC_IDLE, "tc:1")]
    coach.apply_settings(from_dict({"alerts": {"tc_idle": {"sound": False}}}))
    events = hold(coach, 2_000, 3_000, tcs=(tc(TownCenterState.IDLE),))
    assert [e.alert.sound for e in events] == [False]


def test_stockpile_settings_limits_and_age_up_allowance():
    coach = Coach(build_order=(), settings=from_dict({"alerts": {"stockpile": {
        "limit_dark_feudal": 100, "threshold_s": 1, "allow_age_up_savings": False}}}))
    assert kinds(hold(coach, 0, 1_000, food=150)) == [(AlertKind.STOCKPILE, "food")]


def test_house_soon_headroom_setting():
    coach = Coach(build_order=(), settings=from_dict({"alerts": {"house_soon": {"headroom": 4}}}))
    assert kinds(coach.update(make(0, headroom=4))) == [(AlertKind.HOUSE_SOON, "player")]


# --- build selection and research -----------------------------------------------------

BUILD = """
format = 1
id = "test-build"
name = "Test build"
[[steps]]
villagers = 5
task = "Research Loom"
research = "Loom"
grace_s = 1
[[steps]]
villagers = 6
task = "Click up"
age_up = "feudal"
grace_s = 1
[[steps]]
villagers = 6
task = "Barracks while advancing"
while_advancing = true
build = "Barracks"
grace_s = 1
"""


def test_build_change_applies_now_before_any_match_and_next_match_after():
    coach = Coach()
    build = parse(BUILD, "test")
    assert coach.select_build(build) is True and coach.build.id == "test-build"
    coach = Coach()
    coach.update(make(0))
    assert coach.select_build(build) is False
    assert coach.build.id == "scouts-22-pop" and coach.pending_build is build
    coach.update(make(500))
    assert coach.build.id == "scouts-22-pop"
    coach.update(make(0, world=2))
    assert coach.build.id == "test-build" and coach.pending_build is None


def test_research_check_uses_technology_ids_and_carries_between_polls():
    coach = Coach(parse(BUILD, "test"), settings=Settings())
    events = hold(coach, 0, 1_000, vills=villagers(5), researched=set(), researching=set())
    assert (AlertKind.BUILD_ORDER, "step-1") in kinds(events)
    # Loom (22) researched; later polls without technology data keep that knowledge.
    events = coach.update(make(1_500, vills=villagers(5), researched={22}, researching=set()))
    assert (AlertKind.BUILD_ORDER, "step-1") in kinds(events, "cleared")
    coach.update(make(2_000, vills=villagers(5)))
    assert coach.state.researched_ids == frozenset({22})
    assert dict((s.id, st) for s, st in coach.build_progress())["step-1"] == "done"


def test_age_up_detection_is_exact_with_technology_data():
    build = parse(BUILD, "test")
    loom_only = make(0, vills=villagers(6), researched=set(), researching={22},
                     tcs=(tc(TownCenterState.RESEARCHING),))
    feudal = make(0, vills=villagers(6), researched=set(), researching={101},
                  tcs=(tc(TownCenterState.RESEARCHING),))
    click, barracks = build.checks[1], build.checks[2]
    assert click.due(loom_only) and not barracks.due(loom_only)  # researching Loom is not advancing
    assert not click.due(feudal) and barracks.due(feudal)


def test_build_parse_errors_are_reported_together():
    with pytest.raises(BuildOrderError) as caught:
        parse('format = 2\nid = "Bad Id"\n[[steps]]\nvillagers = -1\ntask = ""\nbogus = 1\n', "bad.toml")
    errors = caught.value.errors
    assert any("'format' must be 1" in e for e in errors)
    assert any("'id' must be" in e for e in errors)
    assert any("unknown key 'bogus'" in e for e in errors)
    assert any("'villagers' must be" in e for e in errors)


def test_unknown_names_warn_and_unknown_research_errors():
    build = parse(BUILD.replace('"Barracks"', '"Barraks"'), "typo.toml")
    assert any("'Barraks' is not a known" in w for w in build.warnings)
    with pytest.raises(BuildOrderError, match="unknown technology"):
        parse(BUILD.replace('"Loom"', '"Lom"'), "typo.toml")
    assert parse(BUILD.replace('"Loom"', "22"), "id.toml").checks[0].id == "step-1"


def test_custom_steps_iterable_still_supported():
    step = BuildStep("x", "Do x", due=lambda s: True, done=lambda s: False, grace_ms=0)
    coach = Coach(build_order=[step])
    assert kinds(coach.update(make(0))).count((AlertKind.BUILD_ORDER, "x")) == 1


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


def test_shipped_template_is_valid_but_not_listed_as_a_build():
    from aoe2coach.build_orders import builtin_texts
    template = (Path(__file__).parents[1] / "aoe2coach" / "builds" / "TEMPLATE.toml").read_text(encoding="utf-8")
    assert parse(template, "TEMPLATE.toml").warnings == ()
    assert [name for name, _ in builtin_texts()] == ["builtin:scouts-22-pop.toml"]
