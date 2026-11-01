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
    parser = argparse.ArgumentParser(description="Read-only AoE2 Agent Bridge V3 telemetry")
    parser.add_argument("command", choices=["status", "record", "snapshot"])
    parser.add_argument("--player", type=int, choices=range(1, 9), metavar="1..8", default=1)
    parser.add_argument("--hz", type=float, default=2.0)
    parser.add_argument("--count", type=int, default=None, help="Number of unique snapshots (default: continuous)")
    parser.add_argument("--output", type=Path, default=Path("data") / ("telemetry-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bridge = ReadOnlyBridge(player=args.player)
    try:
        if args.command == "status":
            print(json.dumps(bridge.status(), indent=2))
        elif args.command == "snapshot":
            with bridge:
                print(json.dumps(bridge.observe(), indent=2))
        else:
            count = record(bridge, args.output, hz=args.hz, count=args.count)
            print(f"Recorded {count} observations to {args.output}")
        return 0
    except (BridgeError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
