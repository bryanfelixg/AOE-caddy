"""Shared coaching rules: turn a sequence of PlayerState snapshots into alert episodes.

All durations use game time, so pauses and game speed do not distort them.
The same Coach drives live alerts and offline (post-game) analysis.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Literal

from .telemetry import PlayerState, TownCenterState

VILLAGER_IDLE_MS = 5_000
TC_IDLE_MS = 1_000
HOUSED_MS = 1_000
STOCKPILE_MS = 20_000
# Excess above the next age-up cost that counts as stockpiling, by current age (0 = Dark).
STOCKPILE_LIMIT = {0: 400, 1: 400, 2: 800, 3: 800}
# Standard age-up costs; civilization discounts are not modelled.
NEXT_AGE_COST: dict[int, dict[str, int]] = {0: {"food": 500}, 1: {"food": 800, "gold": 200},
                                            2: {"food": 1000, "gold": 800}, 3: {}}
# Unobserved game time longer than this restarts pending episode timers instead of counting it.
MAX_GAP_MS = 5_000
HOUSE_SOON_HEADROOM = 2

HOUSE, BARRACKS, MILL, STABLE, LUMBER_CAMP = 70, 12, 68, 101, 562
SCOUT_CAVALRY = 448


class AlertKind(str, Enum):
    VILLAGER_IDLE = "villager_idle"
    TC_IDLE = "tc_idle"
    HOUSED = "housed"
    HOUSE_SOON = "house_soon"
    STOCKPILE = "stockpile"
    BUILD_ORDER = "build_order"


@dataclass(frozen=True)
class Alert:
    kind: AlertKind
    subject: str
    message: str
    started_ms: int
    raised_ms: int
    sound: bool


@dataclass(frozen=True)
class AlertEvent:
    action: Literal["raised", "cleared"]
    alert: Alert
    world: int
    game_time_ms: int

    @property
    def duration_ms(self) -> int:
        return self.game_time_ms - self.alert.started_ms


@dataclass(frozen=True)
class BuildStep:
    id: str
    message: str
    due: Callable[[PlayerState], bool]
    done: Callable[[PlayerState], bool]
    grace_ms: int = 10_000


def _researching(state: PlayerState) -> bool:
    return any(tc.state is TownCenterState.RESEARCHING for tc in state.town_centers)


# Standard 22-pop Scouts (Dark Age -> Feudal). Only checks verified telemetry can confirm.
SCOUTS_22_POP: tuple[BuildStep, ...] = (
    BuildStep("lumber_camp", "Build a lumber camp - villagers 7-10 go to wood",
              due=lambda s: s.villager_count >= 8, done=lambda s: s.has_building(LUMBER_CAMP)),
    BuildStep("mill", "Build a mill at berries - villagers 12-15 forage",
              due=lambda s: s.villager_count >= 13, done=lambda s: s.has_building(MILL)),
    BuildStep("click_feudal", "22 pop: research Feudal Age (500 food)",
              due=lambda s: s.current_age == 0 and s.villager_count >= 21 and not _researching(s),
              done=lambda s: (s.current_age or 0) >= 1, grace_ms=3_000),
    BuildStep("barracks", "Build a barracks while advancing - the stable requires it",
              due=lambda s: (s.current_age == 0 and s.villager_count >= 21 and _researching(s))
              or (s.current_age or 0) >= 1,
              done=lambda s: s.has_building(BARRACKS), grace_ms=15_000),
    BuildStep("stable", "Feudal Age: build a stable",
              due=lambda s: (s.current_age or 0) >= 1 and s.has_building(BARRACKS, finished_only=True),
              done=lambda s: s.has_building(STABLE)),
    BuildStep("scouts", "Train 3 scout cavalry from the stable",
              due=lambda s: s.has_building(STABLE, finished_only=True),
              done=lambda s: s.unit_counts.get(SCOUT_CAVALRY, 0) >= 4, grace_ms=30_000),
)

# Informational villager assignments for the same build (not checked).
SCOUTS_22_POP_PLAN: tuple[tuple[int, int, str], ...] = (
    (1, 6, "Sheep (first villagers build 2 houses)"),
    (7, 10, "Wood + lumber camp"),
    (11, 11, "Lure the first boar"),
    (12, 15, "Berries + mill"),
    (16, 17, "Lure the second boar / sheep"),
    (18, 20, "Wood (second lumber camp)"),
    (21, 21, "Food (farm)"),
)


def next_villager_task(villager_count: int) -> str:
    """Assignment for the next villager under the 22-pop Scouts plan."""
    upcoming = villager_count + 1
    for first, last, task in SCOUTS_22_POP_PLAN:
        if first <= upcoming <= last:
            return task
    return "Feudal Age: farms and wood; keep villagers coming"


@dataclass(frozen=True)
class _Condition:
    kind: AlertKind
    subject: str
    message: str
    threshold_ms: int
    sound: bool


class Coach:
    def __init__(self, build_order: Iterable[BuildStep] = SCOUTS_22_POP):
        self.build_order = tuple(build_order)
        self._world: int | None = None
        self._last_time: int | None = None
        self._started: dict[tuple[AlertKind, str], int] = {}
        self._active: dict[tuple[AlertKind, str], Alert] = {}
        self._done_steps: set[str] = set()

    @property
    def active_alerts(self) -> tuple[Alert, ...]:
        return tuple(sorted(self._active.values(), key=lambda a: a.raised_ms))

    def update(self, state: PlayerState) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        now = state.game_time_ms
        if self._world is not None and (state.world != self._world or (self._last_time or 0) > now):
            events += [AlertEvent("cleared", a, self._world, self._last_time or now) for a in self.active_alerts]
            self._started.clear()
            self._active.clear()
            self._done_steps.clear()
            self._last_time = None
        if self._last_time is not None and now - self._last_time > MAX_GAP_MS:
            for key in self._started:
                if key not in self._active:
                    self._started[key] = now
        self._world, self._last_time = state.world, now

        conditions = {(c.kind, c.subject): c for c in self._conditions(state)}
        for key in [k for k in self._started if k not in conditions]:
            del self._started[key]
            alert = self._active.pop(key, None)
            if alert:
                events.append(AlertEvent("cleared", alert, state.world, now))
        for key, cond in conditions.items():
            started = self._started.setdefault(key, now)
            if key not in self._active and now - started >= cond.threshold_ms:
                alert = Alert(cond.kind, cond.subject, cond.message, started, now, cond.sound)
                self._active[key] = alert
                events.append(AlertEvent("raised", alert, state.world, now))
        return events

    def _conditions(self, state: PlayerState) -> list[_Condition]:
        out = [_Condition(AlertKind.VILLAGER_IDLE, f"villager:{v.id}", "Idle villager", VILLAGER_IDLE_MS, True)
               for v in state.idle_villagers]
        tc_message = ("Town Center idle and housed - build a house" if state.housing_headroom == 0
                      else "Town Center idle - queue a villager")
        out += [_Condition(AlertKind.TC_IDLE, f"tc:{tc.id}", tc_message, TC_IDLE_MS, True)
                for tc in state.town_centers if tc.state is TownCenterState.IDLE]
        if any(tc.state is TownCenterState.HOUSED for tc in state.town_centers):
            out.append(_Condition(AlertKind.HOUSED, "player", "Housed - build a house", HOUSED_MS, True))
        elif (state.housing_headroom is not None and 0 < state.housing_headroom <= HOUSE_SOON_HEADROOM
              and not any(b.type_id == HOUSE and not b.finished for b in state.buildings)):
            out.append(_Condition(AlertKind.HOUSE_SOON, "player",
                                  f"Only {state.housing_headroom} population room left - build a house", 0, False))
        out += self._stockpile(state)
        for step in self.build_order:
            if step.id in self._done_steps:
                continue
            if step.done(state):
                self._done_steps.add(step.id)
            elif step.due(state):
                out.append(_Condition(AlertKind.BUILD_ORDER, step.id, step.message, step.grace_ms, False))
        return out

    @staticmethod
    def _stockpile(state: PlayerState) -> list[_Condition]:
        if state.resources is None or state.current_age not in STOCKPILE_LIMIT:
            return []
        limit, cost = STOCKPILE_LIMIT[state.current_age], NEXT_AGE_COST.get(state.current_age, {})
        out = []
        for name in ("food", "wood", "stone", "gold"):
            amount = getattr(state.resources, name)
            if amount - cost.get(name, 0) > limit:
                out.append(_Condition(AlertKind.STOCKPILE, name, f"{amount} {name} banked - spend it",
                                      STOCKPILE_MS, False))
        return out
