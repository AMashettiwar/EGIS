from __future__ import annotations

import argparse
import json
from pathlib import Path

from .certificate import issue_certificate
from .replay import replay_certificate


def run_demo(fixture_path: Path, query_path: Path, output_dir: Path) -> dict:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    query = json.loads(query_path.read_text(encoding="utf-8"))
    certificate = issue_certificate(fixture, query)
    replay = replay_certificate(fixture, certificate)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "certificate.json").write_text(json.dumps(certificate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "replay.json").write_text(json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not replay.get("valid"):
        raise SystemExit(f"CERTIFICATE_REJECTED: {replay}")
    print("CERTIFICATE_VERIFIED")
    print(json.dumps({"status": certificate["result"]["status"], "numerator": certificate["result"]["numerator"], "denominator": certificate["result"]["denominator"], "output_dir": str(output_dir)}, sort_keys=True))
    return certificate


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cancer-evidence-demo")
    parser.add_argument("--fixture", type=Path, default=Path("examples/synthetic_fixture.json"))
    parser.add_argument("--query", type=Path, default=Path("examples/synthetic_overlap.json"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/synthetic-demo"))
    args = parser.parse_args(argv)
    run_demo(args.fixture, args.query, args.out)

