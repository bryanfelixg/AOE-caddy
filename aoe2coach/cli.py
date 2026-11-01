"""Command-line entrypoint."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
import sys

from .bridge import BridgeError, ReadOnlyBridge
from .collector import record


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only AoE2 Agent Bridge V3 telemetry and coaching")
    parser.add_argument("command", choices=["status", "record", "snapshot", "coach", "summary"])
    parser.add_argument("--player", type=int, choices=range(1, 9), metavar="1..8", default=1)
    parser.add_argument("--hz", type=float, default=2.0)
    parser.add_argument("--count", type=int, default=None, help="Number of unique snapshots (default: continuous)")
    parser.add_argument("--output", type=Path, default=None,
                        help="JSONL path (default: data/telemetry-<time>.jsonl, or data/coach-<time>.jsonl for coach)")
    parser.add_argument("--full-recording", action="store_true",
                        help="Save every sample in full (default skips paused samples and fog-of-war entities)")
    coach = parser.add_argument_group("coach")
    coach.add_argument("--port", type=int, default=8765, help="Dashboard port on 127.0.0.1")
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
    bridge = ReadOnlyBridge(player=args.player)
    try:
        if args.command == "status":
            print(json.dumps(bridge.status(), indent=2))
        elif args.command == "snapshot":
            with bridge:
                print(json.dumps(bridge.observe(), indent=2))
        elif args.command == "coach":
            from .dashboard import run_coach
            logging.getLogger("aoe2coach.collector").setLevel(logging.WARNING)
            output = None if args.no_record or args.replay else args.output or Path("data") / f"coach-{stamp}.jsonl"
            run_coach(player=args.player, port=args.port, hz=args.hz, output=output,
                      open_browser=not args.no_browser, replay_path=args.replay, replay_speed=args.speed,
                      compact=not args.full_recording)
        elif args.command == "summary":
            if args.input is None:
                parser.error("summary requires --input <recording.jsonl>")
            from .summary import write_summary
            report, text = write_summary(args.input, args.output)
            print(text)
            print(f"\nReport: {report.resolve()}")
            if not args.no_browser:
                import webbrowser
                webbrowser.open(report.resolve().as_uri())
        else:
            output = args.output or Path("data") / f"telemetry-{stamp}.jsonl"
            count = record(bridge, output, hz=args.hz, count=args.count,
                           skip_paused=not args.full_recording, drop_position_only=not args.full_recording)
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
