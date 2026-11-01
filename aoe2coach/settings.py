"""User settings: typed defaults, validated JSON file, atomic saves.

Defaults reproduce the behaviour before settings existed. Precedence at run
time is defaults < settings file < command-line flags (flags apply to one run).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
import json
import os
from pathlib import Path
import tempfile
import typing
from typing import Any, Mapping

DEFAULT_PATH = Path("data") / "settings.json"
MAX_FILE_BYTES = 64 * 1024


class SettingsError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _bounded(default: Any, lo: float | None = None, hi: float | None = None, label: str = "") -> Any:
    return field(default=default, metadata={"min": lo, "max": hi, "label": label})


@dataclass(frozen=True)
class TimedAlert:
    enabled: bool = True
    threshold_s: float = _bounded(5.0, 0, 600, "Alert after (game seconds)")
    sound: bool = True


@dataclass(frozen=True)
class HouseSoonAlert:
    enabled: bool = True
    headroom: int = _bounded(2, 1, 10, "Warn at population room")
    sound: bool = False


@dataclass(frozen=True)
class StockpileAlert:
    enabled: bool = True
    threshold_s: float = _bounded(20.0, 0, 600, "Alert after (game seconds)")
    limit_dark_feudal: int = _bounded(400, 0, 10_000, "Excess limit, Dark/Feudal")
    limit_castle_imperial: int = _bounded(800, 0, 10_000, "Excess limit, Castle/Imperial")
    allow_age_up_savings: bool = True
    sound: bool = False


@dataclass(frozen=True)
class BuildOrderAlert:
    enabled: bool = True
    sound: bool = False


@dataclass(frozen=True)
class Alerts:
    villager_idle: TimedAlert = TimedAlert(threshold_s=5.0)
    tc_idle: TimedAlert = TimedAlert(threshold_s=1.0)
    housed: TimedAlert = TimedAlert(threshold_s=1.0)
    house_soon: HouseSoonAlert = HouseSoonAlert()
    stockpile: StockpileAlert = StockpileAlert()
    build_order: BuildOrderAlert = BuildOrderAlert()


@dataclass(frozen=True)
class Sound:
    volume: int = _bounded(80, 0, 100, "Volume (%)")
    min_gap_s: float = _bounded(2.0, 0.5, 30, "Minimum seconds between chimes")


@dataclass(frozen=True)
class Recording:
    enabled: bool = True
    compact: bool = True


@dataclass(frozen=True)
class Connection:
    player: int = _bounded(1, 1, 8, "Player slot")
    port: int = _bounded(8765, 1024, 65535, "Dashboard port")
    hz: float = _bounded(2.0, 0.2, 10, "Polls per second")


@dataclass(frozen=True)
class Settings:
    alerts: Alerts = Alerts()
    sound: Sound = Sound()
    recording: Recording = Recording()
    connection: Connection = Connection()
    build_order: str = "scouts-22-pop"


# Changes to these only take effect after restarting the coach.
RESTART_REQUIRED = ("connection.player", "connection.port", "connection.hz", "recording.enabled",
                    "recording.compact")


def to_dict(settings: Settings) -> dict[str, Any]:
    return asdict(settings)


def from_dict(data: Mapping[str, Any], base: Settings | None = None) -> Settings:
    """Overlay `data` (possibly partial) on `base`; raise SettingsError listing every problem."""
    errors: list[str] = []
    result = _overlay(base or Settings(), data, "", errors)
    if errors:
        raise SettingsError(errors)
    return result


def _overlay(obj: Any, data: Any, path: str, errors: list[str]) -> Any:
    if not isinstance(data, Mapping):
        errors.append(f"{path or 'settings'}: expected an object")
        return obj
    hints = typing.get_type_hints(type(obj))
    known = {f.name: f for f in fields(obj)}
    for key in data:
        if key not in known:
            errors.append(f"{path}{key}: unknown setting")
    changes: dict[str, Any] = {}
    for name, f in known.items():
        if name not in data:
            continue
        value, where, kind = data[name], f"{path}{name}", hints[name]
        current = getattr(obj, name)
        if is_dataclass(current):
            changes[name] = _overlay(current, value, where + ".", errors)
        elif kind is bool:
            if isinstance(value, bool):
                changes[name] = value
            else:
                errors.append(f"{where}: expected true or false")
        elif kind in (int, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind is int and value != int(value)):
                errors.append(f"{where}: expected {'a whole number' if kind is int else 'a number'}")
                continue
            lo, hi = f.metadata.get("min"), f.metadata.get("max")
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                errors.append(f"{where}: must be between {lo} and {hi}")
                continue
            changes[name] = kind(value)
        elif kind is str:
            if isinstance(value, str) and value.strip():
                changes[name] = value.strip()
            else:
                errors.append(f"{where}: expected non-empty text")
    return replace(obj, **changes) if changes else obj


def load(path: Path = DEFAULT_PATH) -> Settings:
    """Defaults when the file is missing; SettingsError when it is unreadable or invalid."""
    if not path.exists():
        return Settings()
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SettingsError([f"{path}: file is larger than {MAX_FILE_BYTES} bytes"])
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SettingsError([f"{path}: not valid JSON ({exc})"]) from exc
    return from_dict(data)


def save(settings: Settings, path: Path = DEFAULT_PATH) -> None:
    """Write atomically so a crash never leaves a half-written settings file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_dict(settings), indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def with_overrides(settings: Settings, overrides: Mapping[str, Any]) -> Settings:
    """Apply dotted-path overrides such as {"connection.port": 9000} (command-line flags)."""
    nested: dict[str, Any] = {}
    for dotted, value in overrides.items():
        node = nested
        *parents, leaf = dotted.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return from_dict(nested, settings)


def describe() -> list[dict[str, Any]]:
    """Field metadata for the dashboard form: dotted path, type, bounds, label, default."""
    out: list[dict[str, Any]] = []

    def walk(obj: Any, prefix: str) -> None:
        hints = typing.get_type_hints(type(obj))
        for f in fields(obj):
            value = getattr(obj, f.name)
            if is_dataclass(value):
                walk(value, f"{prefix}{f.name}.")
                continue
            kind = hints[f.name]
            out.append({"path": prefix + f.name, "type": kind.__name__, "default": value,
                        "min": f.metadata.get("min"), "max": f.metadata.get("max"),
                        "label": f.metadata.get("label") or f.name.replace("_", " ").capitalize(),
                        "restart": prefix + f.name in RESTART_REQUIRED})
    walk(Settings(), "")
    return out
