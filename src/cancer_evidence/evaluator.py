from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from .certificate import issue_certificate
from .replay import replay_certificate


def run_hidden(snapshot_dir: Path, query_dir: Path, output_dir: Path, expected_dir: Path | None = None) -> dict[str, Any]:
    """Run candidate outputs with sockets disabled; expected answers stay external."""
    original_socket = socket.socket
    socket.socket = _blocked_socket  # type: ignore[assignment]
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for query_path in sorted(Path(query_dir).glob("*.json")):
            query = json.loads(query_path.read_text(encoding="utf-8"))
            cert_path = output_dir / f"{query_path.stem}.certificate.json"
            cert = issue_certificate(snapshot_dir, query, cert_path)
            replay = replay_certificate(snapshot_dir, cert_path)
            results.append({"query": query_path.name, "certificate": cert_path.name, "status": cert["result"]["status"], "replay_valid": replay.get("valid", False)})
            if not replay.get("valid", False):
                raise ValueError(f"candidate certificate failed replay: {query_path.name}")
        report = {"network": "disabled", "results": results, "expected_answers_consumed": False, "external_scoring_required": True, "expected_dir_supplied": expected_dir is not None}
        (output_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return report
    finally:
        socket.socket = original_socket  # type: ignore[assignment]


def _blocked_socket(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("network disabled during hidden replay")

