"""Command-line entrypoint."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
import sys
from typing import Any

from . import settings as settings_mod
from .bridge import BridgeError, ReadOnlyBridge
from .collector import record
from .settings import SettingsError


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Command-line flags that override the settings file for this run only."""
    overrides: dict[str, Any] = {}
    for flag, path in (("player", "connection.player"), ("hz", "connection.hz"), ("port", "connection.port"),
                       ("build", "build_order")):
        if getattr(args, flag) is not None:
            overrides[path] = getattr(args, flag)
    if args.no_record:
        overrides["recording.enabled"] = False
    if args.full_recording:
        overrides["recording.compact"] = False
    return overrides


def _list_builds() -> int:
    from .build_orders import USER_DIR, load_library
    library = load_library()
    print(f"Build orders (built-in + {USER_DIR}):")
    for build in sorted(library.builds.values(), key=lambda b: b.name.lower()):
        checks = sum(1 for s in build.steps if s.checked)
        print(f"  {build.id:32} {build.name}  [{build.origin}, {len(build.steps)} steps, {checks} checked]")
    for warning in library.warnings:
        print(f"  warning: {warning}")
    for error in library.errors:
        print(f"  ERROR: {error}")
    return 1 if library.errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only AoE2 Agent Bridge V3 telemetry and coaching")
    parser.add_argument("command", choices=["status", "record", "snapshot", "coach", "summary", "builds"])
    parser.add_argument("--settings", type=Path, default=settings_mod.DEFAULT_PATH,
                        help=f"Settings file (default: {settings_mod.DEFAULT_PATH})")
    parser.add_argument("--player", type=int, choices=range(1, 9), metavar="1..8", default=None,
                        help="Override the player slot for this run")
    parser.add_argument("--hz", type=float, default=None, help="Override polls per second for this run")
    parser.add_argument("--count", type=int, default=None, help="Number of unique snapshots (default: continuous)")
    parser.add_argument("--output", type=Path, default=None,
                        help="JSONL path (default: data/telemetry-<time>.jsonl, or data/coach-<time>.jsonl for coach)")
    parser.add_argument("--full-recording", action="store_true",
                        help="Save every sample in full (default skips paused samples and fog-of-war entities)")
    coach = parser.add_argument_group("coach")
    coach.add_argument("--port", type=int, default=None, help="Override the dashboard port for this run")
    coach.add_argument("--build", default=None, help="Override the selected build order id for this run")
    coach.add_argument("--no-record", action="store_true", help="Coach without saving a JSONL recording")
    coach.add_argument("--no-browser", action="store_true", help="Do not open the dashboard automatically")
    coach.add_argument("--replay", type=Path, default=None, help="Feed a recorded JSONL file instead of the game")
    coach.add_argument("--speed", type=float, default=1.0, help="Replay speed in game-seconds per second (0 = instant)")
    summary = parser.add_argument_group("summary")
    summary.add_argument("--input", type=Path, default=None,
                         help="Recording to summarize; the report goes to --output or <input>.summary.html")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        if args.command == "builds":
            return _list_builds()
        if args.command == "summary":
            if args.input is None:
                parser.error("summary requires --input <recording.jsonl>")
            from .summary import write_summary
            report, text = write_summary(args.input, args.output)
            print(text)
            print(f"\nReport: {report.resolve()}")
            if not args.no_browser:
                import webbrowser
                webbrowser.open(report.resolve().as_uri())
            return 0
        if args.command == "coach":
            from .dashboard import run_coach
            logging.getLogger("aoe2coach.collector").setLevel(logging.WARNING)
            output = None if args.replay else args.output or Path("data") / f"coach-{stamp}.jsonl"
            run_coach(settings_path=args.settings, overrides=_overrides(args), output=output,
                      open_browser=not args.no_browser, replay_path=args.replay, replay_speed=args.speed)
            return 0
        try:
            effective = settings_mod.with_overrides(settings_mod.load(args.settings), _overrides(args))
        except SettingsError as exc:
            print(f"WARNING: settings ignored: {exc}", file=sys.stderr)
            effective = settings_mod.with_overrides(settings_mod.Settings(), _overrides(args))
        bridge = ReadOnlyBridge(player=effective.connection.player)
        if args.command == "status":
            print(json.dumps(bridge.status(), indent=2))
        elif args.command == "snapshot":
            with bridge:
                print(json.dumps(bridge.observe(), indent=2))
        else:
            output = args.output or Path("data") / f"telemetry-{stamp}.jsonl"
            compact = effective.recording.compact
            count = record(bridge, output, hz=effective.connection.hz, count=args.count,
                           skip_paused=compact, drop_position_only=compact)
            print(f"Recorded {count} observations to {output}")
        return 0
    except (BridgeError, RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
