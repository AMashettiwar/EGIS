from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine import RULE_VERSION, evaluate, query_hash
from .snapshot import canonical_json, read_snapshot, sha256_bytes


def issue_certificate(snapshot_dir: Path, query: dict[str, Any], output_path: Path) -> dict[str, Any]:
    manifest, connection = read_snapshot(snapshot_dir)
    connection.close()
    result = evaluate(snapshot_dir, query)
    unsigned = {
        "certificate_version": "1.0.0",
        "engine": "cancer-evidence-engine",
        "rule_version": RULE_VERSION,
        "query": query,
        "query_sha256": query_hash(query),
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_manifest_sha256": manifest["manifest_sha256"],
        "result": result,
        "evidence_scope": {"source": manifest["source"], "coverage": manifest.get("coverage", {})},
        "limitations": ["Metadata only; no clinical, prognostic, biomarker, causal, or patient-identity claim."],
    }
    certificate = dict(unsigned)
    certificate["certificate_sha256"] = sha256_bytes(canonical_json(unsigned).encode("utf-8"))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(certificate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return certificate

