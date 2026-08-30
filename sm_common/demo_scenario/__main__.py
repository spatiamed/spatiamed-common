# sm_common/demo_scenario/__main__.py
"""Emit a scenario JSON artifact.

Usage:
    python -m sm_common.demo_scenario \
        --tenant 6ce47f21-64b6-4298-aebb-b8845648c8c0 \
        --from 2026-06-29 --to 2026-09-30 --out scenario.json
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date

from .serialise import scenario_to_dict
from .types import ScenarioConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sm_common.demo_scenario")
    parser.add_argument("--tenant", required=True, type=uuid.UUID)
    parser.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    parser.add_argument("--seed", type=int, default=20260829)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    if args.end < args.start:
        parser.error("--to must not be earlier than --from")

    cfg = ScenarioConfig(tenant_id=args.tenant, start=args.start, end=args.end, seed=args.seed)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(scenario_to_dict(cfg), handle, indent=2, sort_keys=True)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
