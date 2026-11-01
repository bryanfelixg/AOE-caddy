"""Shared coaching rules: turn a sequence of PlayerState snapshots into alert episodes.

All durations use game time, so pauses and game speed do not distort them.
The same Coach drives live alerts and offline (post-game) analysis. Thresholds
come from Settings; build-order reminders come from a BuildOrder.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Literal

from .build_orders import BuildOrder, BuildStep, GuidanceStep, anonymous, default_build
from .settings import Settings
from .telemetry import PlayerState, TownCenterState

__all__ = ["Alert", "AlertEvent", "AlertKind", "BuildStep", "Coach", "MAX_GAP_MS", "NEXT_AGE_COST"]

# Standard age-up costs; civilization discounts are not modelled.
NEXT_AGE_COST: dict[int, dict[str, int]] = {0: {"food": 500}, 1: {"food": 800, "gold": 200},
                                            2: {"food": 1000, "gold": 800}, 3: {}}
# Unobserved game time longer than this restarts pending episode timers instead of counting it.
MAX_GAP_MS = 5_000
HOUSE_TYPE_ID = 70


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
class _Condition:
    kind: AlertKind
    subject: str
    message: str
    threshold_ms: int
    sound: bool


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


class Coach:
    def __init__(self, build_order: BuildOrder | Iterable[BuildStep] | None = None,
                 settings: Settings | None = None):
        self.settings = settings or Settings()
        self.build = self._as_build(build_order)
        self.pending_build: BuildOrder | None = None
        self.state: PlayerState | None = None
        self._world: int | None = None
        self._last_time: int | None = None
        self._tech: tuple[int, frozenset[int], frozenset[int]] | None = None
        self._started: dict[tuple[AlertKind, str], int] = {}
        self._active: dict[tuple[AlertKind, str], Alert] = {}
        self._done_steps: set[str] = set()

    @staticmethod
    def _as_build(build_order: BuildOrder | Iterable[BuildStep] | None) -> BuildOrder:
        if build_order is None:
            return default_build()
        if isinstance(build_order, BuildOrder):
            return build_order
        return anonymous(build_order)

    @property
    def build_order(self) -> tuple[BuildStep, ...]:
        return self.build.checks

    @property
    def active_alerts(self) -> tuple[Alert, ...]:
        return tuple(sorted(self._active.values(), key=lambda a: a.raised_ms))

    def apply_settings(self, settings: Settings) -> None:
        """Takes effect from the next update; disabled alerts clear then."""
        self.settings = settings

    def select_build(self, build: BuildOrder) -> bool:
        """Use `build` now if no match has been seen, otherwise from the next match. Returns True if in force now."""
        if self._world is None or (build.id == self.build.id and build.digest == self.build.digest):
            self.build, self.pending_build = build, None
            return True
        self.pending_build = build
        return False

    def guidance(self, state: PlayerState | None = None) -> tuple[GuidanceStep | None, GuidanceStep | None]:
        state = state or self.state
        return self.build.guidance(state) if state is not None else (None, None)

    def build_progress(self, state: PlayerState | None = None) -> list[tuple[BuildStep, str]]:
        """Each build step with status 'done', 'alert', 'due' (within grace) or 'pending'."""
        state = state or self.state
        progress = []
        for step in self.build.checks:
            if step.id in self._done_steps:
                status = "done"
            elif (AlertKind.BUILD_ORDER, step.id) in self._active:
                status = "alert"
            elif state is not None and step.due(state):
                status = "due"
            else:
                status = "pending"
            progress.append((step, status))
        return progress

    def update(self, state: PlayerState) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        now = state.game_time_ms
        if self._world is not None and (state.world != self._world or (self._last_time or 0) > now):
            events += [AlertEvent("cleared", a, self._world, self._last_time or now) for a in self.active_alerts]
            self._started.clear()
            self._active.clear()
            self._done_steps.clear()
            self._last_time = None
            self._tech = None
            if self.pending_build is not None:
                self.build, self.pending_build = self.pending_build, None
        if self._last_time is not None and now - self._last_time > MAX_GAP_MS:
            for key in self._started:
                if key not in self._active:
                    self._started[key] = now
        self._world, self._last_time = state.world, now
        state = self._carry_technologies(state)
        self.state = state

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

    def _carry_technologies(self, state: PlayerState) -> PlayerState:
        """Technologies are fetched every few polls; reuse the latest known states in between."""
        if state.researched_ids is not None and state.researching_ids is not None:
            self._tech = (state.world, state.researched_ids, state.researching_ids)
            return state
        if self._tech is not None and self._tech[0] == state.world:
            return replace(state, researched_ids=self._tech[1], researching_ids=self._tech[2])
        return state

    def _conditions(self, state: PlayerState) -> list[_Condition]:
        alerts = self.settings.alerts
        out: list[_Condition] = []
        if alerts.villager_idle.enabled:
            out += [_Condition(AlertKind.VILLAGER_IDLE, f"villager:{v.id}", "Idle villager",
                               _ms(alerts.villager_idle.threshold_s), alerts.villager_idle.sound)
                    for v in state.idle_villagers]
        if alerts.tc_idle.enabled:
            tc_message = ("Town Center idle and housed - build a house" if state.housing_headroom == 0
                          else "Town Center idle - queue a villager")
            out += [_Condition(AlertKind.TC_IDLE, f"tc:{tc.id}", tc_message, _ms(alerts.tc_idle.threshold_s),
                               alerts.tc_idle.sound)
                    for tc in state.town_centers if tc.state is TownCenterState.IDLE]
        housed = any(tc.state is TownCenterState.HOUSED for tc in state.town_centers)
        if housed and alerts.housed.enabled:
            out.append(_Condition(AlertKind.HOUSED, "player", "Housed - build a house",
                                  _ms(alerts.housed.threshold_s), alerts.housed.sound))
        elif (not housed and alerts.house_soon.enabled and state.housing_headroom is not None
              and 0 < state.housing_headroom <= alerts.house_soon.headroom
              and not any(b.type_id == HOUSE_TYPE_ID and not b.finished for b in state.buildings)):
            out.append(_Condition(AlertKind.HOUSE_SOON, "player",
                                  f"Only {state.housing_headroom} population room left - build a house", 0,
                                  alerts.house_soon.sound))
        if alerts.stockpile.enabled:
            out += self._stockpile(state)
        for step in self.build.checks:
            if step.id in self._done_steps:
                continue
            if step.done(state):
                self._done_steps.add(step.id)
            elif step.due(state) and alerts.build_order.enabled:
                out.append(_Condition(AlertKind.BUILD_ORDER, step.id, step.message, step.grace_ms,
                                      alerts.build_order.sound))
        return out

    def _stockpile(self, state: PlayerState) -> list[_Condition]:
        cfg = self.settings.alerts.stockpile
        if state.resources is None or state.current_age not in NEXT_AGE_COST:
            return []
        limit = cfg.limit_dark_feudal if state.current_age <= 1 else cfg.limit_castle_imperial
        cost = NEXT_AGE_COST[state.current_age] if cfg.allow_age_up_savings else {}
        out = []
        for name in ("food", "wood", "stone", "gold"):
            amount = getattr(state.resources, name)
            if amount - cost.get(name, 0) > limit:
                out.append(_Condition(AlertKind.STOCKPILE, name, f"{amount} {name} banked - spend it",
                                      _ms(cfg.threshold_s), cfg.sound))
        return out
