from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from oven_runtime.server.composite_backend import load_composite_profile, validate_composite_assets


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oven-server-preflight")
    parser.add_argument("--backend-profile", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--checkpoint-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    profile = load_composite_profile(
        args.backend_profile,
        artifact_root=args.artifact_root,
        checkpoint_root=args.checkpoint_root,
    )
    result = validate_composite_assets(profile)
    print(json.dumps({"status": "READY", **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
