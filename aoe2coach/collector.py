"""Session-aware recording loop. Raw observations remain the source of truth."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable

from .bridge import BridgeError, BridgeRemoteError, ReadOnlyBridge
from .telemetry import compact_technologies

LOG = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(observation: dict[str, Any], drop_position_only: bool, compact_tech: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {"captured_at_utc": _now()}
    entities = observation.get("entities")
    if drop_position_only and isinstance(entities, list):
        kept = [e for e in entities if not (isinstance(e, dict) and e.get("access") == "position")]
        observation = {**observation, "entities": kept}
        row["omitted_position_only_entities"] = len(entities) - len(kept)
    if compact_tech and "technologies" in observation:
        states = compact_technologies(observation["technologies"])
        observation = {k: v for k, v in observation.items() if k != "technologies"}
        if states is not None:
            observation["technology_states"] = states
    row["observation"] = observation
    return row


def record(bridge: ReadOnlyBridge, output: Path | None, *, hz: float = 2.0,
           count: int | None = None, recovery_delay_s: float = 2.0,
           on_observation: Callable[[dict[str, Any]], None] | None = None,
           on_status: Callable[[str], None] | None = None,
           skip_paused: bool = False, drop_position_only: bool = False,
           compact_technologies: bool = False, technologies_every: int = 0,
           meta: Callable[[], dict[str, Any]] | None = None) -> int:
    """Poll the bridge; append new observations to `output` when given.

    `on_observation` receives each new (deduplicated) observation and `on_status`
    a readable message whenever the bridge is not delivering observations.
    `skip_paused` does not save (or count) observations whose game time equals the
    previous one in the same match; the bridge keeps producing those while paused.
    `drop_position_only` omits fog-of-war entities (`access: "position"`) from saved
    rows and records how many were omitted. Callbacks always get the full observation.
    `technologies_every` > 0 requests technology states on every Nth poll;
    `compact_technologies` stores them as researched/researching id lists.
    `meta` returns metadata (build order, settings); a `{"meta": ...}` line is written
    whenever it changes, before the next saved row.
    """
    if not (0 < hz <= 10):
        raise ValueError("hz must be > 0 and <= 10")
    if count is not None and count < 1:
        raise ValueError("count must be >= 1")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)

    def report(message: str) -> None:
        if on_status:
            on_status(message)

    collected = 0
    polls = 0
    previous_key: tuple[Any, Any] | None = None
    previous_time: Any = None
    last_meta: dict[str, Any] | None = None
    file = output.open("a", encoding="utf-8", buffering=1) if output is not None else None
    try:
        while count is None or collected < count:
            start = time.monotonic()
            try:
                if bridge.session is None:
                    status = bridge.status()
                    state = status.get("state")
                    if state == "multiplayer_refused":
                        raise RuntimeError("Bridge refuses multiplayer matches")
                    if state != "ready":
                        LOG.info("Bridge not ready: %s", state)
                        report(f"Bridge not ready: {state}")
                        time.sleep(recovery_delay_s)
                        continue
                    bridge.open()
                with_tech = technologies_every > 0 and polls % technologies_every == 0
                polls += 1
                obs = bridge.observe(technologies=True) if with_tech else bridge.observe()
            except BridgeRemoteError as exc:
                if exc.code in {"session_expired", "invalid_session", "observation_unavailable"}:
                    LOG.warning("Recoverable bridge state: %s", exc)
                    report(f"Recoverable bridge state: {exc}")
                    if exc.code != "observation_unavailable":
                        bridge.session = None
                    time.sleep(recovery_delay_s)
                    continue
                raise
            except BridgeError as exc:
                LOG.warning("Bridge unavailable: %s", exc)
                report(f"Bridge unavailable: {exc}")
                bridge.session = None
                time.sleep(recovery_delay_s)
                continue
            key = (obs.get("world"), obs.get("sequence"))
            if key != previous_key:
                frozen = (skip_paused and previous_time is not None and previous_key is not None
                          and key[0] == previous_key[0] and obs.get("game_time_ms") == previous_time)
                previous_key, previous_time = key, obs.get("game_time_ms")
                # Callbacks run first so metadata reflects rule changes (e.g. a new match's build order).
                if on_observation:
                    on_observation(obs)
                if not frozen:
                    if file is not None:
                        current_meta = meta() if meta else None
                        if current_meta is not None and current_meta != last_meta:
                            file.write(json.dumps({"captured_at_utc": _now(), "meta": current_meta},
                                                  separators=(",", ":"), ensure_ascii=False) + "\n")
                            last_meta = current_meta
                        file.write(json.dumps(_row(obs, drop_position_only, compact_technologies),
                                              separators=(",", ":"), ensure_ascii=False) + "\n")
                    collected += 1
                    LOG.info("sample %d world=%s sequence=%s game_time_ms=%s entities=%d",
                             collected, key[0], key[1], obs.get("game_time_ms"), len(obs.get("entities", [])))
            elapsed = time.monotonic() - start
            time.sleep(max(0.0, 1.0 / hz - elapsed))
    finally:
        if file is not None:
            file.close()
        try:
            bridge.close()
        except BridgeError as exc:
            LOG.warning("Could not close bridge session: %s", exc)
    return collected
