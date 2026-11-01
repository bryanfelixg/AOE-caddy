"""Session-aware recording loop. Raw observations remain the source of truth."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any

from .bridge import BridgeError, BridgeRemoteError, ReadOnlyBridge

LOG = logging.getLogger(__name__)


def record(bridge: ReadOnlyBridge, output: Path, *, hz: float = 2.0,
           count: int | None = None, recovery_delay_s: float = 2.0) -> int:
    if not (0 < hz <= 10):
        raise ValueError("hz must be > 0 and <= 10")
    if count is not None and count < 1:
        raise ValueError("count must be >= 1")
    output.parent.mkdir(parents=True, exist_ok=True)
    collected = 0
    previous_key: tuple[Any, Any] | None = None
    try:
        with output.open("a", encoding="utf-8", buffering=1) as file:
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
                            time.sleep(recovery_delay_s)
                            continue
                        bridge.open()
                    obs = bridge.observe()
                except BridgeRemoteError as exc:
                    if exc.code in {"session_expired", "invalid_session", "observation_unavailable"}:
                        LOG.warning("Recoverable bridge state: %s", exc)
                        if exc.code != "observation_unavailable":
                            bridge.session = None
                        time.sleep(recovery_delay_s)
                        continue
                    raise
                except BridgeError as exc:
                    LOG.warning("Bridge unavailable: %s", exc)
                    bridge.session = None
                    time.sleep(recovery_delay_s)
                    continue
                key = (obs.get("world"), obs.get("sequence"))
                if key != previous_key:
                    row = {"captured_at_utc": datetime.now(timezone.utc).isoformat(), "observation": obs}
                    file.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
                    previous_key = key
                    collected += 1
                    LOG.info("sample %d world=%s sequence=%s game_time_ms=%s entities=%d",
                             collected, key[0], key[1], obs.get("game_time_ms"), len(obs.get("entities", [])))
                elapsed = time.monotonic() - start
                time.sleep(max(0.0, 1.0 / hz - elapsed))
    finally:
        try:
            bridge.close()
        except BridgeError as exc:
            LOG.warning("Could not close bridge session: %s", exc)
    return collected
