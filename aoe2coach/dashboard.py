"""Local live coaching dashboard.

One read-only bridge session feeds the shared Coach; the resulting view is
served on 127.0.0.1 only, as a page that updates through Server-Sent Events.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import json
import logging
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any, Callable

from .analysis import AlertKind, Coach, next_villager_task
from .telemetry import PlayerState, TelemetryError, extract

LOG = logging.getLogger(__name__)
AGE_NAMES = {0: "Dark Age", 1: "Feudal Age", 2: "Castle Age", 3: "Imperial Age"}
_SEVERITY = {AlertKind.VILLAGER_IDLE: 0, AlertKind.HOUSED: 0, AlertKind.TC_IDLE: 0,
             AlertKind.HOUSE_SOON: 1, AlertKind.STOCKPILE: 1, AlertKind.BUILD_ORDER: 2}
_SEVERITY_NAMES = ("high", "medium", "low")
SSE_KEEPALIVE_S = 5.0


class Hub:
    """Latest dashboard view plus a version counter that SSE clients wait on."""

    def __init__(self, view: dict[str, Any] | None = None):
        self._cond = threading.Condition()
        self._view: dict[str, Any] = view or build_view(None, Coach(), status="connecting",
                                                        message="Starting", sound_seq=0, updated_at=None)
        self._version = 0
        self.closed = False

    def publish(self, view: dict[str, Any]) -> None:
        with self._cond:
            self._view = view
            self._version += 1
            self._cond.notify_all()

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        with self._cond:
            return self._version, self._view

    def wait(self, after_version: int, timeout: float) -> tuple[int, dict[str, Any]]:
        with self._cond:
            self._cond.wait_for(lambda: self._version != after_version or self.closed, timeout)
            return self._version, self._view

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()


def build_view(state: PlayerState | None, coach: Coach, *, status: str, message: str,
               sound_seq: int, updated_at: float | None, game_advanced_at: float | None = None) -> dict[str, Any]:
    """JSON-ready dashboard model.

    `updated_at` is the wall-clock time (epoch s) of the last observation and
    `game_advanced_at` the wall-clock time game time last moved forward; the page
    uses them to show STALE (no data) and PAUSED (data, but game time frozen).
    """
    view: dict[str, Any] = {
        "connection": {"status": status, "message": message, "updated_at": updated_at,
                       "game_advanced_at": game_advanced_at},
        "sound_seq": sound_seq, "game": None, "resources": None, "alerts": [], "build_order": [],
        "next_villager": None,
    }
    if state is None:
        return view
    now = state.game_time_ms
    active = coach.active_alerts
    idle = [a for a in active if a.kind is AlertKind.VILLAGER_IDLE]
    alerts: list[dict[str, Any]] = []
    if idle:
        n = len(idle)
        alerts.append(_alert_view(AlertKind.VILLAGER_IDLE, f"{n} idle villager{'s' if n > 1 else ''}",
                                  min(a.started_ms for a in idle)))
    alerts += [_alert_view(a.kind, a.message, a.started_ms) for a in active if a.kind is not AlertKind.VILLAGER_IDLE]
    alerts.sort(key=lambda a: (_SEVERITY_NAMES.index(a["severity"]), a["since_ms"]))
    for a in alerts:
        a["seconds"] = max(0, (now - a["since_ms"]) // 1000)

    stockpiling = {a.subject for a in active if a.kind is AlertKind.STOCKPILE}
    view["game"] = {
        "world": state.world,
        "game_time_ms": now,
        "age": AGE_NAMES.get(state.current_age, "Unknown age") if state.current_age is not None else "Unknown age",
        "villagers": state.villager_count,
        "idle_villagers": len(state.idle_villagers),
        "housing_headroom": state.housing_headroom,
        "town_centers": [{"state": tc.state.value, "queue": tc.train_count, "progress": tc.progress_value}
                         for tc in state.town_centers],
    }
    if state.resources is not None:
        view["resources"] = {name: {"amount": getattr(state.resources, name), "stockpiling": name in stockpiling}
                             for name in ("food", "wood", "stone", "gold")}
    view["alerts"] = alerts
    view["build_order"] = [{"id": step.id, "message": step.message, "status": status}
                           for step, status in coach.build_progress(state)]
    view["next_villager"] = next_villager_task(state.villager_count)
    return view


def _alert_view(kind: AlertKind, title: str, since_ms: int) -> dict[str, Any]:
    return {"kind": kind.value, "title": title, "severity": _SEVERITY_NAMES[_SEVERITY[kind]], "since_ms": since_ms}


class CoachPipeline:
    """Observation -> PlayerState -> Coach -> published dashboard view."""

    def __init__(self, hub: Hub, coach: Coach | None = None, clock: Callable[[], float] = time.time):
        self.hub = hub
        self.coach = coach or Coach()
        self.clock = clock
        self.sound_seq = 0
        self.status, self.message = "connecting", "Waiting for the bridge"
        self._state: PlayerState | None = None
        self._updated_at: float | None = None
        self._game_advanced_at: float | None = None

    def on_observation(self, observation: dict[str, Any]) -> None:
        try:
            state = extract(observation)
        except TelemetryError as exc:
            self.set_status("waiting", f"Unusable observation: {exc}")
            return
        events = self.coach.update(state)
        self.sound_seq += sum(1 for e in events if e.action == "raised" and e.alert.sound)
        for event in events:
            LOG.info("%s %s %s", event.action, event.alert.kind.value, event.alert.subject)
        now = self.clock()
        previous = self._state
        if (previous is None or previous.world != state.world
                or previous.game_time_ms != state.game_time_ms):
            self._game_advanced_at = now
        self._state, self._updated_at = state, now
        self.status, self.message = "live", "Receiving observations"
        self._publish()

    def set_status(self, status: str, message: str) -> None:
        self.status, self.message = status, message
        self._publish()

    def on_status(self, message: str) -> None:
        self.set_status("waiting", message)

    def _publish(self) -> None:
        self.hub.publish(build_view(self._state, self.coach, status=self.status, message=self.message,
                                    sound_seq=self.sound_seq, updated_at=self._updated_at,
                                    game_advanced_at=self._game_advanced_at))


def replay(path: Path, on_observation: Callable[[dict[str, Any]], None], *, speed: float = 1.0,
           sleep: Callable[[float], None] = time.sleep, max_pause_s: float = 5.0) -> int:
    """Feed a recorded JSONL file through `on_observation`, paced by game time / `speed` (0 = no pacing)."""
    count = 0
    previous: tuple[Any, int] | None = None
    with path.open(encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            observation = json.loads(line)["observation"]
            world, game_time = observation.get("world"), observation.get("game_time_ms")
            if (speed > 0 and previous is not None and isinstance(game_time, int)
                    and previous[0] == world and game_time > previous[1]):
                sleep(min((game_time - previous[1]) / 1000 / speed, max_pause_s))
            if isinstance(game_time, int):
                previous = (world, game_time)
            on_observation(observation)
            count += 1
    return count


def run_coach(*, player: int, port: int, hz: float, output: Path | None, open_browser: bool,
              replay_path: Path | None = None, replay_speed: float = 1.0, compact: bool = True) -> int:
    """Serve the dashboard and feed it live (or from a recording) until Ctrl+C.

    `compact` skips paused samples and fog-of-war entities in the saved recording.
    """
    from .bridge import ReadOnlyBridge
    from .collector import record

    hub = Hub()
    pipeline = CoachPipeline(hub)
    server = make_server(hub, port)
    threading.Thread(target=server.serve_forever, name="dashboard-http", daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Dashboard: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        if replay_path is not None:
            pipeline.set_status("waiting", f"Replaying {replay_path.name}")
            count = replay(replay_path, pipeline.on_observation, speed=replay_speed)
            pipeline.set_status("replay", f"Replay finished: {count} observations")
            print(f"Replay finished: {count} observations. Dashboard stays up until Ctrl+C.", flush=True)
            while True:
                time.sleep(1)
        if output is not None:
            print(f"Recording to {output}", flush=True)
        return record(ReadOnlyBridge(player), output, hz=hz,
                      on_observation=pipeline.on_observation, on_status=pipeline.on_status,
                      skip_paused=compact, drop_position_only=compact)
    finally:
        hub.close()
        server.shutdown()
        server.server_close()


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind the same port silently; require exclusive use.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(hub: Hub, port: int = 8765) -> ThreadingHTTPServer:
    """HTTP server bound to 127.0.0.1 only; rejects requests whose Host is not local."""
    page = resources.files("aoe2coach").joinpath("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            LOG.debug("http: " + format, *args)

        def _local_host(self) -> bool:
            bound = self.server.server_address[1]
            return self.headers.get("Host", "") in {f"127.0.0.1:{bound}", f"localhost:{bound}"}

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not self._local_host():
                self._send(403, "text/plain; charset=utf-8", b"Forbidden")
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif path == "/state":
                self._send(200, "application/json", json.dumps(hub.snapshot()[1]).encode("utf-8"))
            elif path == "/events":
                self._stream()
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            version = -1
            try:
                while not hub.closed:
                    new_version, view = hub.wait(version, SSE_KEEPALIVE_S)
                    if new_version != version:
                        self.wfile.write(b"data: " + json.dumps(view).encode("utf-8") + b"\n\n")
                        version = new_version
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    try:
        server = _LoopbackServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise OSError(f"Port {port} on 127.0.0.1 is already in use - is another coach running? "
                      f"Stop it or pass --port. ({exc})") from exc
    return server
