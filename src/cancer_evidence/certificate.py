from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .engine import RULE_VERSION, evaluate, query_hash
from .signing import DEFAULT_PRIVATE_KEY, DEFAULT_PUBLIC_KEY, sign
from .snapshot import canonical_json, read_snapshot


def _unsigned_bytes(certificate: dict[str, Any]) -> bytes:
    unsigned = dict(certificate)
    unsigned.pop("certificate_sha256", None)
    unsigned.pop("signature", None)
    return canonical_json(unsigned).encode("utf-8")


def certificate_digest(certificate: dict[str, Any]) -> str:
    unsigned = dict(certificate)
    unsigned.pop("certificate_sha256", None)
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def issue_certificate(
    snapshot_dir: Path,
    query: dict[str, Any],
    output_path: Path,
    *,
    private_key_path: Path = DEFAULT_PRIVATE_KEY,
) -> dict[str, Any]:
    manifest, connection = read_snapshot(snapshot_dir)
    connection.close()
    result = evaluate(snapshot_dir, query)
    certificate: dict[str, Any] = {
        "certificate_version": "2.0.0",
        "engine": "cancer-evidence-engine",
        "rule_version": RULE_VERSION,
        "query": query,
        "query_sha256": query_hash(query),
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_manifest_sha256": manifest["manifest_sha256"],
        "snapshot_sha256": manifest["raw_content_sha256"],
        "derivation": manifest["derivation"],
        "result": result,
        "evidence_scope": {"source": manifest["source"], "coverage": manifest.get("coverage", {})},
        "limitations": ["Metadata only; no clinical, prognostic, biomarker, causal, or patient-identity claim."],
        "signature_algorithm": "Ed25519",
        "verification_public_key": str(DEFAULT_PUBLIC_KEY).replace("\\", "/"),
    }
    certificate["signature"] = sign(_unsigned_bytes(certificate), private_key_path)
    certificate["certificate_sha256"] = certificate_digest(certificate)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(certificate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return certificate
