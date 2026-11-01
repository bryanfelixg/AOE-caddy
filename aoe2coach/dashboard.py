"""Local live coaching dashboard.

One read-only bridge session feeds the shared Coach; the resulting view is
served on 127.0.0.1 only, as a page that updates through Server-Sent Events.
Settings are read and saved through /settings (same-origin JSON only).
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
from typing import Any, Callable, Mapping

from . import settings as settings_mod
from .analysis import AlertKind, Coach
from .build_orders import AGE_TITLES, USER_DIR, BuildOrder, GuidanceStep, Library, load_library
from .settings import Settings, SettingsError
from .telemetry import AGE_NAMES, PlayerState, TelemetryError, extract

LOG = logging.getLogger(__name__)
_SEVERITY = {AlertKind.VILLAGER_IDLE: 0, AlertKind.HOUSED: 0, AlertKind.TC_IDLE: 0,
             AlertKind.HOUSE_SOON: 1, AlertKind.STOCKPILE: 1, AlertKind.BUILD_ORDER: 2}
_SEVERITY_NAMES = ("high", "medium", "low")
SSE_KEEPALIVE_S = 5.0
MAX_POST_BYTES = 64 * 1024
APP_VERSION = "aoe2-coach-collector 0.2.0"


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


def _step_view(step: GuidanceStep | None) -> dict[str, Any] | None:
    if step is None:
        return None
    return {"villagers": step.villagers, "age": AGE_TITLES[step.age], "task": step.task, "note": step.note}


def _training_view(state: PlayerState, coach: Coach) -> dict[str, Any]:
    current, upcoming = coach.guidance(state)
    age = state.current_age or 0
    target = next(((a, v) for a, v in coach.build.age_up_targets if a > age), None)
    expected = current.age if current is not None else age
    return {
        "build": {"id": coach.build.id, "name": coach.build.name, "civilizations": list(coach.build.civilizations)},
        "pending": coach.pending_build.name if coach.pending_build else None,
        "current": _step_view(current),
        "next": _step_view(upcoming),
        "age": {"actual": AGE_NAMES.get(age, "Unknown age"), "expected": AGE_TITLES[expected],
                "advancing": state.age_up_in_progress,
                "target": f"{AGE_TITLES[target[0]]} at {target[1]} villagers" if target else None},
    }


def build_view(state: PlayerState | None, coach: Coach, *, status: str, message: str,
               sound_seq: int, updated_at: float | None, game_advanced_at: float | None = None,
               notice: str | None = None) -> dict[str, Any]:
    """JSON-ready dashboard model.

    `updated_at` is the wall-clock time (epoch s) of the last observation and
    `game_advanced_at` the wall-clock time game time last moved forward; the page
    uses them to show STALE (no data) and PAUSED (data, but game time frozen).
    """
    sound = coach.settings.sound
    view: dict[str, Any] = {
        "connection": {"status": status, "message": message, "updated_at": updated_at,
                       "game_advanced_at": game_advanced_at},
        "sound_seq": sound_seq, "sound": {"volume": sound.volume, "min_gap_s": sound.min_gap_s},
        "notice": notice, "game": None, "resources": None, "alerts": [], "build_order": [], "training": None,
    }
    view["build_order"] = [{"id": step.id, "message": step.message, "status": status_}
                           for step, status_ in coach.build_progress(state)]
    if state is None:
        view["training"] = {"build": {"id": coach.build.id, "name": coach.build.name,
                                      "civilizations": list(coach.build.civilizations)},
                            "pending": coach.pending_build.name if coach.pending_build else None,
                            "current": None, "next": None, "age": None}
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
    view["training"] = _training_view(state, coach)
    return view


def _alert_view(kind: AlertKind, title: str, since_ms: int) -> dict[str, Any]:
    return {"kind": kind.value, "title": title, "severity": _SEVERITY_NAMES[_SEVERITY[kind]], "since_ms": since_ms}


class CoachPipeline:
    """Observation -> PlayerState -> Coach -> published dashboard view. Thread-safe."""

    def __init__(self, hub: Hub, coach: Coach | None = None, clock: Callable[[], float] = time.time):
        self.hub = hub
        self.coach = coach or Coach()
        self.clock = clock
        self.sound_seq = 0
        self.status, self.message = "connecting", "Waiting for the bridge"
        self.notice: str | None = None
        self.lock = threading.RLock()
        self._state: PlayerState | None = None
        self._updated_at: float | None = None
        self._game_advanced_at: float | None = None

    def on_observation(self, observation: dict[str, Any]) -> None:
        try:
            state = extract(observation)
        except TelemetryError as exc:
            self.set_status("waiting", f"Unusable observation: {exc}")
            return
        with self.lock:
            events = self.coach.update(state)
            self.sound_seq += sum(1 for e in events if e.action == "raised" and e.alert.sound)
            for event in events:
                LOG.info("%s %s %s", event.action, event.alert.kind.value, event.alert.subject)
            now = self.clock()
            previous = self._state
            if (previous is None or previous.world != state.world
                    or previous.game_time_ms != state.game_time_ms):
                self._game_advanced_at = now
            self._state, self._updated_at = self.coach.state, now
            self.status, self.message = "live", "Receiving observations"
            self._publish()

    def set_status(self, status: str, message: str) -> None:
        with self.lock:
            self.status, self.message = status, message
            self._publish()

    def on_status(self, message: str) -> None:
        self.set_status("waiting", message)

    def apply(self, settings: Settings, build: BuildOrder | None) -> bool | None:
        """Apply live settings and select `build`. Returns True/False (applied now / next match), None if unchanged."""
        with self.lock:
            self.coach.apply_settings(settings)
            applied = None
            if build is not None and (build.id != self.coach.build.id or build.digest != self.coach.build.digest
                                      or self.coach.pending_build is not None):
                applied = self.coach.select_build(build)
            self._publish()
            return applied

    def meta(self) -> dict[str, Any]:
        """Recording metadata describing the rules currently in force."""
        with self.lock:
            build = self.coach.build
            return {"app": APP_VERSION, "build": {"id": build.id, "name": build.name, "digest": build.digest},
                    "settings": settings_mod.to_dict(self.coach.settings)}

    def _publish(self) -> None:
        self.hub.publish(build_view(self._state, self.coach, status=self.status, message=self.message,
                                    sound_seq=self.sound_seq, updated_at=self._updated_at,
                                    game_advanced_at=self._game_advanced_at, notice=self.notice))


class SettingsController:
    """Owns the settings file, command-line overrides and the build-order library."""

    def __init__(self, pipeline: CoachPipeline, path: Path = settings_mod.DEFAULT_PATH,
                 overrides: Mapping[str, Any] | None = None, user_dir: Path | None = USER_DIR):
        self.pipeline = pipeline
        self.path = path
        self.overrides = dict(overrides or {})
        self.user_dir = user_dir
        self.lock = threading.Lock()
        self.file_error: str | None = None
        try:
            self.saved = settings_mod.load(path)
        except SettingsError as exc:
            self.file_error = f"{path} could not be used, so defaults are active: {exc}"
            LOG.warning(self.file_error)
            self.saved = Settings()
        self.library: Library = load_library(user_dir)
        self.started = self.effective()

    def effective(self, saved: Settings | None = None) -> Settings:
        return settings_mod.with_overrides(saved or self.saved, self.overrides)

    def start(self) -> Settings:
        """Apply the effective settings and selected build before the first observation."""
        effective = self.effective()
        build = self._resolve_build(effective.build_order)
        self.pipeline.apply(effective, build)
        return effective

    def _resolve_build(self, build_id: str) -> BuildOrder:
        build = self.library.get(build_id)
        if build is None:
            fallback = self.library.get(Settings().build_order)
            self.pipeline.notice = (f"Build order '{build_id}' was not found or has errors; "
                                    f"using '{fallback.name}'. Check the settings panel.")
            return fallback
        return build

    def payload(self) -> dict[str, Any]:
        with self.lock:
            self.library = load_library(self.user_dir)
            coach = self.pipeline.coach
            return {
                "saved": settings_mod.to_dict(self.saved),
                "effective": settings_mod.to_dict(self.effective()),
                "overrides": self.overrides,
                "schema": settings_mod.describe(),
                "file": str(self.path), "file_error": self.file_error,
                "builds": [b.summary() for b in sorted(self.library.builds.values(), key=lambda b: b.name.lower())],
                "build_errors": self.library.errors, "build_warnings": self.library.warnings,
                "build_folder": str(self.user_dir) if self.user_dir else None,
                "active_build": coach.build.id,
                "pending_build": coach.pending_build.id if coach.pending_build else None,
                "restart_required": self._restart_needed(self.effective()),
            }

    def _restart_needed(self, effective: Settings) -> list[str]:
        before, after = settings_mod.to_dict(self.started), settings_mod.to_dict(effective)
        changed = []
        for dotted in settings_mod.RESTART_REQUIRED:
            a, b = before, after
            for part in dotted.split("."):
                a, b = a[part], b[part]
            if a != b:
                changed.append(dotted)
        return changed

    def update(self, data: Any) -> dict[str, Any]:
        """Validate a (partial) settings object, save it, apply live changes. Raises SettingsError."""
        with self.lock:
            new = settings_mod.from_dict(data, self.saved)
            self.library = load_library(self.user_dir)
            if self.library.get(new.build_order) is None:
                raise SettingsError([f"build_order: unknown build order '{new.build_order}'"])
            settings_mod.save(new, self.path)
            self.saved, self.file_error = new, None
            effective = self.effective()
            self.pipeline.notice = None
            applied = self.pipeline.apply(effective, self.library.get(effective.build_order))
        result = self.payload()
        result["build_applies"] = {True: "now", False: "next match", None: None}[applied]
        return result


def replay(path: Path, on_observation: Callable[[dict[str, Any]], None], *, speed: float = 1.0,
           sleep: Callable[[float], None] = time.sleep, max_pause_s: float = 5.0,
           on_meta: Callable[[dict[str, Any]], None] | None = None) -> int:
    """Feed a recorded JSONL file through `on_observation`, paced by game time / `speed` (0 = no pacing)."""
    count = 0
    previous: tuple[Any, int] | None = None
    with path.open(encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            row = json.loads(line)
            if "meta" in row:
                if on_meta and isinstance(row["meta"], dict):
                    on_meta(row["meta"])
                continue
            observation = row["observation"]
            world, game_time = observation.get("world"), observation.get("game_time_ms")
            if (speed > 0 and previous is not None and isinstance(game_time, int)
                    and previous[0] == world and game_time > previous[1]):
                sleep(min((game_time - previous[1]) / 1000 / speed, max_pause_s))
            if isinstance(game_time, int):
                previous = (world, game_time)
            on_observation(observation)
            count += 1
    return count


def run_coach(*, settings_path: Path = settings_mod.DEFAULT_PATH, overrides: Mapping[str, Any] | None = None,
              output: Path | None = None, open_browser: bool = True, replay_path: Path | None = None,
              replay_speed: float = 1.0, technologies_every: int = 4) -> int:
    """Serve the dashboard and feed it live (or from a recording) until Ctrl+C."""
    from .bridge import ReadOnlyBridge
    from .collector import record

    hub = Hub()
    pipeline = CoachPipeline(hub)
    controller = SettingsController(pipeline, settings_path, overrides)
    effective = controller.start()
    if controller.file_error:
        print(f"WARNING: {controller.file_error}", flush=True)
    for error in controller.library.errors:
        print(f"WARNING: build order skipped: {error}", flush=True)
    conn = effective.connection
    server = make_server(hub, conn.port, controller)
    threading.Thread(target=server.serve_forever, name="dashboard-http", daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Dashboard: {url}  (Ctrl+C to stop)", flush=True)
    print(f"Build order: {pipeline.coach.build.name}", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    if not effective.recording.enabled:
        output = None
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
        compact = effective.recording.compact
        return record(ReadOnlyBridge(conn.player), output, hz=conn.hz,
                      on_observation=pipeline.on_observation, on_status=pipeline.on_status,
                      skip_paused=compact, drop_position_only=compact, compact_technologies=compact,
                      technologies_every=technologies_every, meta=pipeline.meta)
    finally:
        hub.close()
        server.shutdown()
        server.server_close()
        if output is not None and output.exists() and output.stat().st_size > 0:
            _write_post_game_summary(output)


def _write_post_game_summary(recording: Path) -> None:
    from .summary import write_summary

    print("Writing post-game summary...", flush=True)
    try:
        report, text = write_summary(recording)
    except Exception as exc:  # a summary failure must not hide how the coach stopped
        LOG.warning("Could not write post-game summary: %s", exc)
        return
    print(text, flush=True)
    print(f"Report: {report.resolve()}", flush=True)


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind the same port silently; require exclusive use.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(hub: Hub, port: int = 8765, controller: SettingsController | None = None) -> ThreadingHTTPServer:
    """HTTP server bound to 127.0.0.1 only; rejects requests whose Host is not local.

    POST /settings additionally requires a same-origin `Origin` header and a JSON body,
    so other websites open in the browser cannot change settings.
    """
    page = resources.files("aoe2coach").joinpath("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            LOG.debug("http: " + format, *args)

        def _origins(self) -> set[str]:
            bound = self.server.server_address[1]
            return {f"127.0.0.1:{bound}", f"localhost:{bound}"}

        def _local_host(self) -> bool:
            return self.headers.get("Host", "") in self._origins()

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

        def _json(self, code: int, data: Any) -> None:
            self._send(code, "application/json", json.dumps(data).encode("utf-8"))

        def do_GET(self) -> None:
            if not self._local_host():
                self._send(403, "text/plain; charset=utf-8", b"Forbidden")
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif path == "/state":
                self._json(200, hub.snapshot()[1])
            elif path == "/events":
                self._stream()
            elif path == "/settings" and controller is not None:
                self._json(200, controller.payload())
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if not self._local_host() or self.headers.get("Origin", "") not in {f"http://{o}" for o in self._origins()}:
                self._json(403, {"errors": ["Forbidden: requests must come from the dashboard page"]})
                return
            if path != "/settings" or controller is None:
                self._json(404, {"errors": ["Not found"]})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self._json(415, {"errors": ["Content-Type must be application/json"]})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._json(411, {"errors": ["Content-Length required"]})
                return
            if length > MAX_POST_BYTES:
                self._json(413, {"errors": ["Request too large"]})
                return
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeError, ValueError):
                self._json(400, {"errors": ["Body is not valid JSON"]})
                return
            try:
                self._json(200, controller.update(data))
            except SettingsError as exc:
                self._json(400, {"errors": exc.errors})
            except OSError as exc:
                self._json(500, {"errors": [f"Could not save settings: {exc}"]})

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
