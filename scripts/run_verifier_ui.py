#!/usr/bin/env python3
"""Build or serve the read-only offline EGIS Human-Verifier Demo UI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cancer_evidence.verifier_ui import build_verifier_site, serve_verifier_ui


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or serve the read-only offline EGIS Human-Verifier Demo UI.")
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts" / "human-verifier-ui")
    parser.add_argument("--serve", action="store_true", help="Serve the read-only UI on localhost.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.serve:
        serve_verifier_ui(host=args.host, port=args.port, output_dir=args.out)
    else:
        paths = build_verifier_site(args.out)
        print(
            json.dumps(
                {
                    "mode": "read_only_offline_static",
                    "html_path": str(paths["html_path"]),
                    "view_model_path": str(paths["view_model_path"]),
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
