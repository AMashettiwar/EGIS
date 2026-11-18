from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def inventory(path: Path) -> dict:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(path / "data.sqlite")
    try:
        count = connection.execute("SELECT COUNT(*) FROM entities WHERE kind = 'case'").fetchone()[0]
    finally:
        connection.close()
    return {"source": manifest["source"], "snapshot_id": manifest["snapshot_id"], "count": count, "complete": manifest["complete"], "raw_content_sha256": manifest["raw_content_sha256"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("gdc", type=Path)
    parser.add_argument("pdc", type=Path)
    args = parser.parse_args()
    print(json.dumps({"inventories": {"gdc": inventory(args.gdc), "pdc": inventory(args.pdc)}, "cross_cohort_overlap": {"status": "UNKNOWN", "reason_code": "NO_APPROVED_MAPPING"}}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
