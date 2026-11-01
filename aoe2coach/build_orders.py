"""Build orders: TOML files compiled into the Coach's build steps.

See docs/build-orders.md for the format. Built-in builds ship in
aoe2coach/builds/; personal builds live in data/build_orders/ (Git-ignored).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from importlib import resources
from pathlib import Path
import re
import tomllib
from typing import Any, Callable, Iterable, Mapping

from .telemetry import TECHNOLOGIES, PlayerState, normalize_name

USER_DIR = Path("data") / "build_orders"
FORMAT_VERSION = 1
AGES = {"dark": 0, "feudal": 1, "castle": 2, "imperial": 3}
AGE_TITLES = {0: "Dark Age", 1: "Feudal Age", 2: "Castle Age", 3: "Imperial Age"}
DEFAULT_GRACE_S = 10.0
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TECH_IDS = {normalize_name(name): tech_id for tech_id, name in TECHNOLOGIES.items()}

# Common shorthand -> in-game type name.
ALIASES = {
    "tc": "town center", "range": "archery range", "lumbercamp": "lumber camp", "mining camp": "mining camp",
    "scout": "scout cavalry", "scouts": "scout cavalry", "maa": "man-at-arms", "men-at-arms": "man-at-arms",
    "archers": "archer", "skirms": "skirmisher", "skirm": "skirmisher", "skirmishers": "skirmisher",
    "spear": "spearman", "spearmen": "spearman", "knights": "knight", "militia": "militia",
}
# Names the game reports for buildings and units (English client). Others load with a warning.
KNOWN_NAMES = frozenset(normalize_name(n) for n in (
    "House", "Town Center", "Lumber Camp", "Mill", "Mining Camp", "Barracks", "Archery Range", "Stable",
    "Blacksmith", "Market", "Dock", "Farm", "Palisade Wall", "Palisade Gate", "Stone Wall", "Gate", "Outpost",
    "Watch Tower", "Monastery", "University", "Siege Workshop", "Castle", "Fish Trap", "Krepost", "Donjon",
    "Folwark", "Feitoria", "Harbor", "Mule Cart", "Pasture",
    "Scout Cavalry", "Militia", "Man-at-Arms", "Spearman", "Archer", "Skirmisher", "Knight", "Cavalry Archer",
    "Eagle Scout", "Eagle Warrior", "Camel Scout", "Camel Rider", "Light Cavalry", "Crossbowman",
    "Fishing Ship", "Trade Cart", "Monk", "Mangonel", "Scorpion", "Battering Ram", "Shrivamsha Rider",
))


class BuildOrderError(ValueError):
    def __init__(self, source: str, errors: list[str]):
        self.source = source
        self.errors = errors
        super().__init__(f"{source}: " + "; ".join(errors))


@dataclass(frozen=True)
class BuildStep:
    """A checkable step: raises a reminder while `due` and not yet `done`."""
    id: str
    message: str
    due: Callable[[PlayerState], bool]
    done: Callable[[PlayerState], bool]
    grace_ms: int = 10_000


@dataclass(frozen=True)
class GuidanceStep:
    id: str
    villagers: int
    age: int
    task: str
    note: str = ""
    checked: bool = False


@dataclass(frozen=True)
class BuildOrder:
    id: str
    name: str
    steps: tuple[GuidanceStep, ...] = ()
    checks: tuple[BuildStep, ...] = ()
    civilizations: tuple[str, ...] = ()
    description: str = ""
    source: str = ""
    origin: str = "code"
    digest: str = ""
    warnings: tuple[str, ...] = ()
    age_up_targets: tuple[tuple[int, int], ...] = field(default=())  # (target age, villagers)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "civilizations": list(self.civilizations),
                "description": self.description, "source": self.source, "origin": self.origin,
                "digest": self.digest, "warnings": list(self.warnings),
                "steps": [{"villagers": s.villagers, "age": AGE_TITLES[s.age], "task": s.task, "note": s.note,
                           "checked": s.checked} for s in self.steps]}

    def guidance(self, state: PlayerState) -> tuple[GuidanceStep | None, GuidanceStep | None]:
        """(current, next) step for the player's age and villager count.

        Reaching an age makes its first step current even with fewer villagers than planned.
        """
        age, vils = state.current_age or 0, state.villager_count
        current_index = -1
        for index, step in enumerate(self.steps):
            if step.age < age or (step.age == age and step.villagers <= vils):
                current_index = index
        first_of_age = next((i for i, s in enumerate(self.steps) if s.age == age), None)
        if first_of_age is not None and current_index < first_of_age and age > 0:
            current_index = first_of_age
        current = self.steps[current_index] if current_index >= 0 else None
        upcoming = self.steps[current_index + 1] if current_index + 1 < len(self.steps) else None
        return current, upcoming


def anonymous(checks: Iterable[BuildStep]) -> BuildOrder:
    return BuildOrder(id="custom", name="Custom", checks=tuple(checks))


# --- parsing ------------------------------------------------------------------------------

_TOP_KEYS = {"format", "id", "name", "civilizations", "description", "source", "steps"}
_STEP_KEYS = {"id", "villagers", "age", "task", "note", "age_up", "reach_age", "build", "train", "research",
              "requires", "while_advancing", "grace_s"}


def parse(text: str, source: str, origin: str = "user") -> BuildOrder:
    """Parse and validate TOML text; raise BuildOrderError listing every problem."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BuildOrderError(source, [f"TOML syntax error: {exc}"]) from exc
    errors: list[str] = []
    warnings: list[str] = []
    for key in sorted(set(data) - _TOP_KEYS):
        errors.append(f"unknown key '{key}'")
    if data.get("format") != FORMAT_VERSION:
        errors.append(f"'format' must be {FORMAT_VERSION}")
    build_id = data.get("id")
    if not isinstance(build_id, str) or not _ID.match(build_id):
        errors.append("'id' must be lowercase letters, digits, '-' or '_' (max 64)")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("'name' is required")
    civs = data.get("civilizations", [])
    if not isinstance(civs, list) or not all(isinstance(c, str) for c in civs):
        errors.append("'civilizations' must be a list of text")
        civs = []
    for key in ("description", "source"):
        if not isinstance(data.get(key, ""), str):
            errors.append(f"'{key}' must be text")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        errors.append("at least one [[steps]] entry is required")
        raw_steps = []

    steps: list[GuidanceStep] = []
    checks: list[BuildStep] = []
    targets: list[tuple[int, int]] = []
    seen_ids: set[str] = set()
    previous_age = 0
    for index, raw in enumerate(raw_steps, start=1):
        where = f"step {index}"
        if not isinstance(raw, dict):
            errors.append(f"{where}: must be a table")
            continue
        before = len(errors)
        step = _parse_step(raw, index, previous_age, where, errors, warnings)
        if step is None or len(errors) > before:
            continue
        guidance, check, target = step
        if guidance.id in seen_ids:
            errors.append(f"{where}: duplicate step id '{guidance.id}'")
            continue
        seen_ids.add(guidance.id)
        previous_age = guidance.age
        steps.append(guidance)
        if check is not None:
            checks.append(check)
        if target is not None:
            targets.append(target)
    if errors:
        raise BuildOrderError(source, errors)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return BuildOrder(id=build_id, name=name.strip(), steps=tuple(steps), checks=tuple(checks),
                      civilizations=tuple(civs), description=data.get("description", ""),
                      source=data.get("source", ""), origin=origin, digest=digest,
                      warnings=tuple(f"{source}: {w}" for w in warnings), age_up_targets=tuple(targets))


def _age(value: Any, where: str, errors: list[str]) -> int | None:
    if isinstance(value, str) and value.lower() in AGES:
        return AGES[value.lower()]
    errors.append(f"{where}: must be one of {', '.join(AGES)}")
    return None


def _name(value: Any, where: str, errors: list[str], warnings: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: name must be text")
        return None
    key = normalize_name(value)
    key = ALIASES.get(key, key)
    if key not in KNOWN_NAMES:
        warnings.append(f"{where}: '{value}' is not a known building/unit name; it only matches the exact in-game name")
    return key


def _count_spec(value: Any, where: str, errors: list[str], warnings: list[str],
                allow_finished: bool) -> tuple[str, int, bool] | None:
    if isinstance(value, str):
        value = {"name": value}
    if not isinstance(value, dict):
        errors.append(f"{where}: use \"Name\" or {{ name = \"Name\", count = 1 }}")
        return None
    allowed = {"name", "count"} | ({"finished"} if allow_finished else set())
    for key in sorted(set(value) - allowed):
        errors.append(f"{where}: unknown key '{key}'")
    name = _name(value.get("name"), where, errors, warnings)
    count = value.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 200:
        errors.append(f"{where}: count must be a whole number from 1 to 200")
        return None
    finished = value.get("finished", False)
    if not isinstance(finished, bool):
        errors.append(f"{where}: finished must be true or false")
        return None
    return (name, count, finished) if name else None


def _parse_step(raw: Mapping[str, Any], index: int, previous_age: int, where: str,
                errors: list[str], warnings: list[str]):
    for key in sorted(set(raw) - _STEP_KEYS):
        errors.append(f"{where}: unknown key '{key}'")
    villagers = raw.get("villagers")
    if isinstance(villagers, bool) or not isinstance(villagers, int) or not 0 <= villagers <= 200:
        errors.append(f"{where}: 'villagers' must be a whole number from 0 to 200")
        return None
    where = f"step {index} ({villagers} villagers)"
    age = _age(raw["age"], f"{where} age", errors) if "age" in raw else previous_age
    task = raw.get("task")
    if not isinstance(task, str) or not task.strip():
        errors.append(f"{where}: 'task' is required")
    note = raw.get("note", "")
    if not isinstance(note, str):
        errors.append(f"{where}: 'note' must be text")
    step_id = raw.get("id", f"step-{index}")
    if not isinstance(step_id, str) or not _ID.match(step_id):
        errors.append(f"{where}: 'id' must be lowercase letters, digits, '-' or '_'")
    grace = raw.get("grace_s", DEFAULT_GRACE_S)
    if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not 0 <= grace <= 600:
        errors.append(f"{where}: 'grace_s' must be 0-600 seconds")
    advancing = raw.get("while_advancing", False)
    if not isinstance(advancing, bool):
        errors.append(f"{where}: 'while_advancing' must be true or false")
    requires_raw = raw.get("requires", [])
    if isinstance(requires_raw, str):
        requires_raw = [requires_raw]
    if not isinstance(requires_raw, list):
        errors.append(f"{where}: 'requires' must be a building name or a list of names")
        requires_raw = []
    requires = [n for n in (_name(r, f"{where} requires", errors, warnings) for r in requires_raw) if n]

    age_up = _age(raw["age_up"], f"{where} age_up", errors) if "age_up" in raw else None
    reach = _age(raw["reach_age"], f"{where} reach_age", errors) if "reach_age" in raw else None
    build = _count_spec(raw["build"], f"{where} build", errors, warnings, True) if "build" in raw else None
    train = _count_spec(raw["train"], f"{where} train", errors, warnings, False) if "train" in raw else None
    research = None
    if "research" in raw:
        research = _technology(raw["research"], f"{where} research", errors)
    if age is not None and age_up is not None and age_up <= age:
        errors.append(f"{where}: age_up must be a later age than the step's age")
    if age is None or not isinstance(task, str) or not task.strip():
        return None

    has_check = any(c is not None for c in (age_up, reach, build, train, research))
    guidance = GuidanceStep(step_id, villagers, age, task.strip(), note.strip() if isinstance(note, str) else "",
                            has_check)
    if not has_check:
        return guidance, None, None
    check = BuildStep(step_id, task.strip(),
                      due=_due(age, villagers, bool(advancing), requires, age_up),
                      done=_done(age_up, reach, build, train, research),
                      grace_ms=int(float(grace) * 1000))
    return guidance, check, ((age_up, villagers) if age_up is not None else None)


def _technology(value: Any, where: str, errors: list[str]) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
        return value
    if isinstance(value, str) and normalize_name(value) in _TECH_IDS:
        return _TECH_IDS[normalize_name(value)]
    errors.append(f"{where}: unknown technology {value!r}; use a known name (see docs/build-orders.md) "
                  "or its numeric id")
    return None


def _due(age: int, villagers: int, while_advancing: bool, requires: list[str], age_up: int | None):
    def due(state: PlayerState) -> bool:
        current = state.current_age or 0
        if age_up is not None and (current >= age_up or state.age_up_in_progress):
            return False
        reached = current > age or (current == age and state.villager_count >= villagers
                                    and (not while_advancing or state.age_up_in_progress))
        return reached and all(state.building_count(n, finished_only=True) >= 1 for n in requires)
    return due


def _done(age_up: int | None, reach: int | None, build: tuple[str, int, bool] | None,
          train: tuple[str, int, bool] | None, research: int | None):
    def done(state: PlayerState) -> bool:
        current = state.current_age or 0
        if age_up is not None and current < age_up:
            return False
        if reach is not None and current < reach:
            return False
        if build is not None and state.building_count(build[0], finished_only=build[2]) < build[1]:
            return False
        if train is not None and state.unit_count(train[0]) < train[1]:
            return False
        if research is not None and (state.researched_ids is None or research not in state.researched_ids):
            return False
        return True
    return done


# --- loading ------------------------------------------------------------------------------

@dataclass
class Library:
    builds: dict[str, BuildOrder]
    errors: list[str]

    @property
    def warnings(self) -> list[str]:
        return [w for b in self.builds.values() for w in b.warnings]

    def get(self, build_id: str) -> BuildOrder | None:
        return self.builds.get(build_id)


def builtin_texts() -> list[tuple[str, str]]:
    folder = resources.files("aoe2coach").joinpath("builds")
    return sorted((f"builtin:{f.name}", f.read_text(encoding="utf-8"))
                  for f in folder.iterdir() if f.name.endswith(".toml") and f.name != "TEMPLATE.toml")


def load_library(user_dir: Path | None = USER_DIR) -> Library:
    """Built-in builds plus valid user files; invalid files are reported, never fatal."""
    builds: dict[str, BuildOrder] = {}
    errors: list[str] = []
    sources = [(name, text, "builtin") for name, text in builtin_texts()]
    if user_dir is not None and user_dir.is_dir():
        for path in sorted(user_dir.glob("*.toml")):
            try:
                sources.append((str(path), path.read_text(encoding="utf-8-sig"), "user"))
            except (OSError, UnicodeError) as exc:
                errors.append(f"{path}: cannot read ({exc})")
    for source, text, origin in sources:
        try:
            build = parse(text, source, origin)
        except BuildOrderError as exc:
            errors.append(str(exc))
            continue
        if build.id in builds:
            errors.append(f"{source}: id '{build.id}' is already used by {builds[build.id].origin} build "
                          f"'{builds[build.id].name}'; skipped")
            continue
        builds[build.id] = build
    return Library(builds, errors)


_DEFAULT: BuildOrder | None = None


def default_build() -> BuildOrder:
    global _DEFAULT
    if _DEFAULT is None:
        text = resources.files("aoe2coach").joinpath("builds", "scouts-22-pop.toml").read_text(encoding="utf-8")
        _DEFAULT = parse(text, "builtin:scouts-22-pop.toml", "builtin")
    return _DEFAULT
