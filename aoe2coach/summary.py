"""Post-game summaries: replay a recording through the same Coach used live."""
from __future__ import annotations

from dataclasses import dataclass, field
import html
import json
import logging
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .analysis import MAX_GAP_MS, AlertEvent, AlertKind, Coach
from .build_orders import AGE_TITLES, BuildOrder, Library, load_library
from .settings import SettingsError, from_dict
from .telemetry import AGE_NAMES, TelemetryError, extract

LOG = logging.getLogger(__name__)
LATE_START_MS = 10_000
RESOURCES = ("food", "wood", "stone", "gold")


class MetaRecord(dict):
    """A `{"meta": ...}` line from a recording: the build order and settings in force from here on."""


def clock(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass
class Episodes:
    count: int = 0
    total_ms: int = 0
    longest_ms: int = 0
    ongoing: int = 0

    def add(self, duration_ms: int, *, ongoing: bool = False) -> None:
        self.count += 1
        self.total_ms += duration_ms
        self.longest_ms = max(self.longest_ms, duration_ms)
        self.ongoing += ongoing

    def describe(self) -> str:
        if not self.count:
            return "none"
        text = (f"{self.count} episode{'s' if self.count != 1 else ''}, {clock(self.total_ms)} total, "
                f"longest {clock(self.longest_ms)}")
        return text + (f" ({self.ongoing} still active at end)" if self.ongoing else "")


@dataclass
class StepTiming:
    id: str
    message: str
    target_villagers: int | None = None
    reminded_ms: int | None = None
    done_ms: int | None = None
    done_villagers: int | None = None


@dataclass
class AgeUp:
    target: int
    clicked_ms: int | None = None
    clicked_villagers: int | None = None
    planned_villagers: int | None = None


@dataclass
class MatchSummary:
    world: int
    civilization: str | None
    first_ms: int
    last_ms: int
    samples: int = 0
    gaps: int = 0
    gap_ms: int = 0
    final_age: int | None = None
    final_villagers: int = 0
    build_name: str = ""
    build_notes: list[str] = field(default_factory=list)
    villager_idle: Episodes = field(default_factory=Episodes)
    tc_idle: Episodes = field(default_factory=Episodes)
    housed: Episodes = field(default_factory=Episodes)
    stockpile: dict[str, Episodes] = field(default_factory=lambda: {r: Episodes() for r in RESOURCES})
    age_reached: dict[int, int] = field(default_factory=dict)
    age_ups: dict[int, AgeUp] = field(default_factory=dict)
    build_steps: list[StepTiming] = field(default_factory=list)
    villager_timeline: list[tuple[int, int]] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return self.last_ms - self.first_ms

    @property
    def starts_late(self) -> bool:
        return self.first_ms > LATE_START_MS


class RecordingReader:
    """Yields observations (and MetaRecord lines) from a JSONL recording, skipping unreadable lines."""

    def __init__(self, path: Path):
        self.path = path
        self.skipped_lines = 0

    def __iter__(self) -> Iterator[dict[str, Any]]:
        with self.path.open(encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and isinstance(row.get("meta"), dict):
                        yield MetaRecord(row["meta"])
                        continue
                    observation = row["observation"]
                except (ValueError, KeyError, TypeError):
                    self.skipped_lines += 1
                    continue
                if isinstance(observation, dict):
                    yield observation
                else:
                    self.skipped_lines += 1


class _BuildTracker:
    """Applies recording metadata to the Coach and remembers notes about it."""

    def __init__(self, coach: Coach, library: Library | None):
        self.coach = coach
        self.library = library
        self.notes: list[str] = []
        self.seen_meta = False

    def apply(self, meta: Mapping[str, Any]) -> None:
        self.seen_meta = True
        try:
            self.coach.apply_settings(from_dict(meta.get("settings") or {}))
        except SettingsError as exc:
            self.notes.append(f"Recorded settings could not be used ({exc}); defaults applied.")
        info = meta.get("build")
        if not isinstance(info, Mapping) or self.library is None:
            return
        build: BuildOrder | None = self.library.get(str(info.get("id")))
        name = info.get("name") or info.get("id")
        if build is None:
            self.notes.append(f"Build order '{name}' used during the match is no longer available; "
                              f"steps are evaluated with '{self.coach.build.name}'.")
            return
        if info.get("digest") and info.get("digest") != build.digest:
            self.notes.append(f"Build order '{build.name}' has been edited since this recording; "
                              "steps are evaluated with the current file.")
        self.coach.select_build(build)


def summarize(observations: Iterable[Mapping[str, Any]], coach: Coach | None = None,
              library: Library | None = None) -> list[MatchSummary]:
    """One summary per match (`world`) using the live alert rules and the recorded build/settings."""
    coach = coach or Coach()
    tracker = _BuildTracker(coach, library if library is not None else load_library())
    matches: dict[int, MatchSummary] = {}
    current: MatchSummary | None = None
    next_minute = 0
    for observation in observations:
        if isinstance(observation, MetaRecord):
            tracker.apply(observation)
            continue
        try:
            state = extract(observation)
        except TelemetryError:
            continue
        t = state.game_time_ms
        new_match = current is None or state.world != current.world
        if new_match:
            player = observation.get("player")
            civ = player.get("civilization_name") if isinstance(player, Mapping) else None
            current = MatchSummary(state.world, civ if isinstance(civ, str) else None, t, t)
            matches[state.world] = current
            next_minute = t
        elif t - current.last_ms > MAX_GAP_MS:
            current.gaps += 1
            current.gap_ms += t - current.last_ms

        for event in coach.update(state):
            target = matches.get(event.world)
            if target is not None:
                _apply(target, event, ended_by_new_match=event.world != state.world)
        state = coach.state or state
        if new_match:
            _start_build(current, coach, tracker)

        current.samples += 1
        current.last_ms = max(current.last_ms, t)
        current.final_age = state.current_age
        current.final_villagers = state.villager_count
        if state.current_age is not None and state.current_age not in current.age_reached:
            current.age_reached[state.current_age] = t
        age = state.current_age or 0
        if state.age_up_in_progress and age + 1 in current.age_ups:
            click = current.age_ups[age + 1]
            if click.clicked_ms is None:
                click.clicked_ms, click.clicked_villagers = t, state.villager_count
        if t >= next_minute:
            current.villager_timeline.append((t, state.villager_count))
            next_minute = (t // 60_000 + 1) * 60_000
        for timing, (_, status) in zip(current.build_steps, coach.build_progress(state)):
            if status == "done" and timing.done_ms is None:
                timing.done_ms, timing.done_villagers = t, state.villager_count

    if current is not None:
        for alert in coach.active_alerts:
            _add_episode(current, alert.kind, alert.subject, current.last_ms - alert.started_ms, ongoing=True)
    return list(matches.values())


def _start_build(summary: MatchSummary, coach: Coach, tracker: _BuildTracker) -> None:
    build = coach.build
    villagers = {s.id: s.villagers for s in build.steps}
    summary.build_name = build.name
    summary.build_notes = list(tracker.notes)
    if not tracker.seen_meta:
        summary.build_notes.append(f"Recording has no build-order metadata; evaluated with '{build.name}'.")
    summary.build_steps = [StepTiming(step.id, step.message, villagers.get(step.id)) for step in build.checks]
    summary.age_ups = {age: AgeUp(age, planned_villagers=vils) for age, vils in build.age_up_targets}
    for age in (1, 2, 3):
        summary.age_ups.setdefault(age, AgeUp(age))


def _apply(summary: MatchSummary, event: AlertEvent, *, ended_by_new_match: bool) -> None:
    if event.action == "raised":
        if event.alert.kind is AlertKind.BUILD_ORDER:
            timing = next((s for s in summary.build_steps if s.id == event.alert.subject), None)
            if timing is not None and timing.reminded_ms is None:
                timing.reminded_ms = event.game_time_ms
        return
    _add_episode(summary, event.alert.kind, event.alert.subject, event.duration_ms, ongoing=ended_by_new_match)


def _add_episode(summary: MatchSummary, kind: AlertKind, subject: str, duration_ms: int, *, ongoing: bool) -> None:
    if kind is AlertKind.VILLAGER_IDLE:
        summary.villager_idle.add(duration_ms, ongoing=ongoing)
    elif kind is AlertKind.TC_IDLE:
        summary.tc_idle.add(duration_ms, ongoing=ongoing)
    elif kind is AlertKind.HOUSED:
        summary.housed.add(duration_ms, ongoing=ongoing)
    elif kind is AlertKind.STOCKPILE and subject in summary.stockpile:
        summary.stockpile[subject].add(duration_ms, ongoing=ongoing)


def _data_notes(m: MatchSummary, skipped_lines: int) -> list[str]:
    notes = list(m.build_notes)
    if m.starts_late:
        notes.append(f"Recording started at {clock(m.first_ms)} game time; earlier play is not included.")
    if m.gaps:
        notes.append(f"{m.gaps} gap{'s' if m.gaps != 1 else ''} without data ({clock(m.gap_ms)} unobserved).")
    if skipped_lines:
        notes.append(f"{skipped_lines} unreadable line{'s' if skipped_lines != 1 else ''} skipped "
                     "(for example, a recording stopped mid-write).")
    notes.append("Match result is not recorded; only coaching metrics are shown.")
    return notes


def _ages(m: MatchSummary) -> str:
    parts = []
    for age, t in sorted(m.age_reached.items()):
        name = AGE_NAMES.get(age, f"Age {age}")
        parts.append(f"{name} {'(at start of recording)' if t == m.first_ms else 'at ' + clock(t)}")
    return " -> ".join(parts) or "unknown"


def _step_text(m: MatchSummary, s: StepTiming) -> str:
    if s.done_ms is not None:
        if s.done_ms == m.first_ms and m.starts_late:
            done = "before recording began"
        else:
            done = f"done at {clock(s.done_ms)} with {s.done_villagers} villagers"
            if s.target_villagers is not None:
                done += f" (plan: {s.target_villagers})"
    else:
        done = "not done"
    reminder = f", reminder shown at {clock(s.reminded_ms)}" if s.reminded_ms is not None else ""
    return done + reminder


def _age_up_lines(m: MatchSummary) -> list[str]:
    lines = []
    for age, up in sorted(m.age_ups.items()):
        if up.clicked_ms is None and up.planned_villagers is None:
            continue
        name = AGE_TITLES[age]
        plan = f" (plan: {up.planned_villagers} villagers)" if up.planned_villagers is not None else ""
        if up.clicked_ms is not None:
            reached = m.age_reached.get(age)
            lines.append(f"{name}: clicked at {clock(up.clicked_ms)} with {up.clicked_villagers} villagers{plan}"
                         + (f", reached {clock(reached)}" if reached is not None else ""))
        elif age in m.age_reached and m.age_reached[age] > m.first_ms:
            lines.append(f"{name}: reached at {clock(m.age_reached[age])}; click not observed{plan}")
        elif (m.final_age or 0) < age:
            lines.append(f"{name}: not clicked{plan}")
    return lines


def render_text(summaries: list[MatchSummary], skipped_lines: int = 0) -> str:
    if not summaries:
        return "No usable observations in this recording."
    lines = []
    for m in summaries:
        civ = f" - {m.civilization}" if m.civilization else ""
        lines.append(f"Match (world {m.world}){civ}: {clock(m.duration_ms)} of game time recorded "
                     f"({clock(m.first_ms)} -> {clock(m.last_ms)}), {m.samples} samples")
        lines.append(f"  Idle villagers:   {m.villager_idle.describe()}")
        lines.append(f"  Town Center idle: {m.tc_idle.describe()}")
        lines.append(f"  Housed:           {m.housed.describe()}")
        banked = [f"{r} {clock(e.total_ms)}" for r, e in m.stockpile.items() if e.count]
        lines.append(f"  Stockpiling:      {', '.join(banked) or 'none'}")
        lines.append(f"  Ages:             {_ages(m)}")
        lines += [f"  Age-up:           {line}" for line in _age_up_lines(m)]
        lines.append(f"  Build order ({m.build_name}):")
        lines += [f"    - {s.message}: {_step_text(m, s)}" for s in m.build_steps]
        trend = " | ".join(f"{clock(t)} {count}" for t, count in m.villager_timeline)
        lines.append(f"  Villagers:        {trend + ' | ' if trend else ''}end {m.final_villagers}")
        lines += [f"  Note: {note}" for note in _data_notes(m, skipped_lines)]
    return "\n".join(lines)


def _chart(m: MatchSummary) -> str:
    data = list(m.villager_timeline)
    if data and m.last_ms > data[-1][0]:
        data.append((m.last_ms, m.final_villagers))
    if len(data) < 2:
        return "<p class='muted'>Not enough data for a chart.</p>"
    width, height, pad = 640, 220, 32
    min_x, max_x = data[0][0], data[-1][0]
    max_y = max(max(y for _, y in data), 1)

    def sx(x: int) -> float:
        return pad + (width - 2 * pad) * (x - min_x) / max(max_x - min_x, 1)

    def sy(y: int) -> float:
        return height - pad - (height - 2 * pad) * y / max_y

    poly = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in data)
    labels = "".join(f"<text x='{sx(x):.1f}' y='{sy(y) - 8:.1f}' text-anchor='middle'>{y}</text>" for x, y in data)
    return (f"<svg viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' "
            f"aria-label='Villagers over game time'>"
            f"<line x1='{pad}' y1='{height - pad}' x2='{width - pad}' y2='{height - pad}' class='axis'/>"
            f"<polyline points='{poly}' class='line'/>{labels}"
            f"<text x='{pad}' y='{height - 8}'>{clock(min_x)}</text>"
            f"<text x='{width - pad}' y='{height - 8}' text-anchor='end'>{clock(max_x)}</text></svg>")


def render_html(summaries: list[MatchSummary], source: str, skipped_lines: int = 0) -> str:
    e = html.escape
    sections = []
    for m in summaries:
        rows = [("Idle villagers", m.villager_idle.describe()), ("Town Center idle", m.tc_idle.describe()),
                ("Housed", m.housed.describe())]
        rows += [(f"Stockpiling {r}", ep.describe()) for r, ep in m.stockpile.items()]
        rows.append(("Ages", _ages(m)))
        rows += [("Age-up", line) for line in _age_up_lines(m)]
        metric_rows = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows)
        steps = "".join(f"<tr class='{'done' if s.done_ms is not None else 'missed'}'><th>{e(s.message)}</th>"
                        f"<td>{e(_step_text(m, s))}</td></tr>" for s in m.build_steps)
        notes = "".join(f"<li>{e(n)}</li>" for n in _data_notes(m, skipped_lines))
        civ = f" - {e(m.civilization)}" if m.civilization else ""
        sections.append(
            f"<section><h2>Match (world {m.world}){civ}</h2>"
            f"<p class='muted'>{e(clock(m.duration_ms))} of game time recorded "
            f"({e(clock(m.first_ms))} &rarr; {e(clock(m.last_ms))}), {m.samples} samples</p>"
            f"<h3>Coaching metrics</h3><table>{metric_rows}</table>"
            f"<h3>Build order - {e(m.build_name)}</h3><table>{steps}</table>"
            f"<h3>Villagers over game time</h3>{_chart(m)}"
            f"<h3>Data quality</h3><ul>{notes}</ul></section>")
    body = "".join(sections) or "<p>No usable observations in this recording.</p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AOE-caddy post-game summary</title>
<style>
body {{ margin: 0; padding: 24px; background: #101317; color: #e8edf2; font: 18px/1.4 "Segoe UI", system-ui, sans-serif; }}
section {{ background: #1a1f26; border-radius: 10px; padding: 16px 20px; margin-bottom: 20px; max-width: 900px; }}
h1 {{ font-size: 26px; }} h2 {{ margin-top: 0; }} h3 {{ color: #8b96a3; font-size: 15px; text-transform: uppercase; letter-spacing: .08em; }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #2a313b; vertical-align: top; }}
th {{ width: 45%; font-weight: 600; }} tr.missed td {{ color: #ffb347; }} tr.done td {{ color: #4cd97b; }}
.muted {{ color: #8b96a3; }} svg {{ width: 100%; max-width: 640px; height: auto; background: #222933; border-radius: 8px; }}
svg text {{ fill: #8b96a3; font-size: 12px; }} .axis {{ stroke: #2a313b; }} .line {{ fill: none; stroke: #6fb3ff; stroke-width: 3; }}
</style></head><body><h1>AOE-caddy post-game summary</h1><p class="muted">Source: {e(source)}</p>{body}</body></html>"""


def write_summary(recording: Path, output: Path | None = None) -> tuple[Path, str]:
    """Summarize `recording`; write the HTML report and return (report path, terminal text)."""
    reader = RecordingReader(recording)
    summaries = summarize(reader)
    output = output or default_summary_path(recording)
    output.write_text(render_html(summaries, recording.name, reader.skipped_lines), encoding="utf-8")
    return output, render_text(summaries, reader.skipped_lines)


def default_summary_path(recording: Path) -> Path:
    name = recording.name[:-len(".jsonl")] if recording.name.endswith(".jsonl") else recording.name
    return recording.with_name(name + ".summary.html")
