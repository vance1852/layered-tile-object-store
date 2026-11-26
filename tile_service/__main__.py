"""Command line entry point: ``python -m tile_service ...``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:  # support `python tile_service/__main__.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tile_service.replay import replay_fixture
    from tile_service.server import serve_forever
else:
    from .replay import replay_fixture
    from .server import serve_forever


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tile_service", description="版本化稀疏瓦片存储与离线图包服务")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run HTTP service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    serve.add_argument("--data", default=None, help="persistent data directory; omit for in-memory mode")
    serve.add_argument("--fixture", default=None, help="import a fixture directory before serving")

    replay = sub.add_parser("replay", help="replay a fixture events.json and print JSON report")
    replay.add_argument("fixture")
    replay.add_argument("--data", default=None)
    replay.add_argument("--summary", action="store_true", help="print only event summaries")

    args = parser.parse_args(argv)
    if args.command == "serve":
        serve_forever(args.host, args.port, data_dir=args.data, fixture=args.fixture)
        return 0
    if args.command == "replay":
        report = replay_fixture(args.fixture, args.data)
        if args.summary:
            for event in report["results"]:
                print(json.dumps({k: v for k, v in event.items() if k not in {"usage", "details"}}, ensure_ascii=False))
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=lambda b: b.decode("latin1") if isinstance(b, bytes) else str(b)))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
