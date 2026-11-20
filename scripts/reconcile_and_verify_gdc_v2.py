#!/usr/bin/env python3
"""Offline independent source reconciliation, evaluation, certification, and replay for GDC TCGA-LUAD v2."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

SITECUSTOMIZE_SOURCE = """\
import socket
import urllib.request

def _blocked(*args, **kwargs):
    raise RuntimeError("network disabled during offline gdc-v2 milestone verification")

socket.socket = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
urllib.request.urlopen = _blocked
"""


def _blocked_net(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("network disabled during offline source reconciliation")


def _find_evidence_cli(root: Path) -> list[str]:
    venv_bin = root / ".venv" / "bin" / "evidence"
    if venv_bin.exists():
        return [str(venv_bin)]
    return [sys.executable, "-m", "cancer_evidence"]


def _run_offline_cli(cli_prefix: list[str], args: list[str], scratch: Path) -> dict[str, Any]:
    sitecustomize = scratch / "sitecustomize.py"
    if not sitecustomize.exists():
        sitecustomize.write_text(SITECUSTOMIZE_SOURCE, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{scratch}{os.pathsep}{(ROOT / 'src').resolve()}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["EGIS_OFFLINE"] = "1"
    proc = subprocess.run(
        [*cli_prefix, *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        shell=False,
    )
    return {
        "cmd": [*cli_prefix, *args],
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "stdout_json": json.loads(proc.stdout) if proc.stdout.strip() else None,
    }


def main() -> int:
    # Disable network in-process as well
    orig_socket = socket.socket
    orig_create = socket.create_connection
    orig_addr = socket.getaddrinfo
    orig_urlopen = urllib.request.urlopen
    socket.socket = _blocked_net  # type: ignore[assignment]
    socket.create_connection = _blocked_net  # type: ignore[assignment]
    socket.getaddrinfo = _blocked_net  # type: ignore[assignment]
    urllib.request.urlopen = _blocked_net  # type: ignore[assignment]
    try:
        snapshot_dir = ROOT / "snapshots" / "real" / "gdc_tcga_luad_v2"
        out_dir = ROOT / "artifacts" / "gdc-luad-v2-milestone"
        out_dir.mkdir(parents=True, exist_ok=True)

        manifest_bytes = (snapshot_dir / "manifest.json").read_bytes()
        raw_bytes = (snapshot_dir / "raw.json").read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        raw = json.loads(raw_bytes.decode("utf-8"))

        raw_sha256_actual = hashlib.sha256(raw_bytes).hexdigest()
        raw_hash_matches_manifest = raw_sha256_actual == manifest.get("raw_content_sha256")

        # Independent calculation directly from GDC raw_capture (zero EGIS engine imports)
        raw_capture = raw["raw_capture"]
        raw_cases: dict[str, dict[str, Any]] = {}
        raw_samples: dict[str, tuple[str, str | None]] = {}
        raw_aliquots: dict[str, str] = {}
        sample_type_counts: dict[str, int] = {}

        for page in raw_capture["case_pages"]:
            for c in page["response"]["data"]["hits"]:
                cid = c["case_id"]
                raw_cases[cid] = c
                for s in c.get("samples", []) or []:
                    sid = s["sample_id"]
                    if sid not in raw_samples:
                        st = s.get("sample_type")
                        raw_samples[sid] = (cid, st)
                        sample_type_counts[str(st)] = sample_type_counts.get(str(st), 0) + 1
                    for p in s.get("portions", []) or []:
                        for an in p.get("analytes", []) or []:
                            for alq in an.get("aliquots", []) or []:
                                alqid = alq["aliquot_id"]
                                raw_aliquots[alqid] = sid

        raw_files: dict[str, dict[str, Any]] = {}
        qualifying_files: set[str] = set()
        samples_with_qualifying_file: set[str] = set()
        primary_tumor_samples = {sid for sid, (_, st) in raw_samples.items() if st == "Primary Tumor"}
        primary_tumor_samples_with_qualifying_file: set[str] = set()
        orphan_sample_links = 0
        orphan_aliquot_links = 0

        for page in raw_capture["file_pages"]:
            for fl in page["response"]["data"]["hits"]:
                fid = fl["file_id"]
                raw_files[fid] = fl
                qualifies = (
                    fl.get("data_category") == "Transcriptome Profiling"
                    and fl.get("data_type") == "Gene Expression Quantification"
                    and fl.get("experimental_strategy") == "RNA-Seq"
                    and fl.get("data_format") == "TSV"
                    and fl.get("access") == "open"
                    and fl.get("state") == "released"
                )
                if qualifies:
                    qualifying_files.add(fid)
                for c in fl.get("cases", []) or []:
                    for s in c.get("samples", []) or []:
                        sid = s.get("sample_id")
                        if sid not in raw_samples:
                            orphan_sample_links += 1
                            continue
                        for p in s.get("portions", []) or []:
                            for an in p.get("analytes", []) or []:
                                for alq in an.get("aliquots", []) or []:
                                    if alq.get("aliquot_id") not in raw_aliquots:
                                        orphan_aliquot_links += 1
                        if qualifies:
                            samples_with_qualifying_file.add(sid)
                            if sid in primary_tumor_samples:
                                primary_tumor_samples_with_qualifying_file.add(sid)

        expected_witnesses = sorted(primary_tumor_samples_with_qualifying_file)
        expected_num = len(expected_witnesses)
        expected_den = len(primary_tumor_samples)

        snapshot_id = manifest["snapshot_id"]
        cohort_id = manifest["cohort_id"]

        supported_query = {
            "operation": "cohort_count",
            "snapshot_id": snapshot_id,
            "cohort_id": cohort_id,
            "entity_level": "sample",
            "denominator_basis": "tcga_luad_gdc_r46_primary_tumor_samples",
            "predicate": {
                "source": "gdc",
                "project_id": "TCGA-LUAD",
                "sample_type": "Primary Tumor",
                "data_category": "Transcriptome Profiling",
                "data_type": "Gene Expression Quantification",
                "experimental_strategy": "RNA-Seq",
                "data_format": "TSV",
                "access": "open",
                "state": "released",
            },
        }
        abstaining_query = {
            "operation": "cohort_count",
            "snapshot_id": snapshot_id,
            "cohort_id": cohort_id,
            "entity_level": "sample",
            "denominator_basis": "tcga_luad_gdc_r46_primary_tumor_samples_with_completed_EGFR_assay",
            "predicate": {
                "source": "gdc",
                "project_id": "TCGA-LUAD",
                "sample_type": "Primary Tumor",
                "biomarker": "EGFR_ACTIVATING",
                "assay_status": "COMPLETED",
                "qc_status": "PASS",
                "positive_results_in": ["L858R_POSITIVE", "Ex19del_POSITIVE"],
            },
        }

        sup_q_path = out_dir / "supported_query.json"
        sup_res_path = out_dir / "supported_result.json"
        sup_cert_path = out_dir / "supported_certificate.json"
        sup_rep_path = out_dir / "supported_replay.json"
        abs_q_path = out_dir / "abstaining_query.json"
        abs_res_path = out_dir / "abstaining_result.json"
        abs_cert_path = out_dir / "abstaining_certificate.json"
        abs_rep_path = out_dir / "abstaining_replay.json"

        sup_q_path.write_text(json.dumps(supported_query, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        abs_q_path.write_text(json.dumps(abstaining_query, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        cli_prefix = _find_evidence_cli(ROOT)
        with tempfile.TemporaryDirectory(prefix="egis_gdc_v2_offline_") as tmp:
            scratch = Path(tmp)
            sup_ans = _run_offline_cli(
                cli_prefix,
                ["answer", "--snapshot", str(snapshot_dir), "--query", str(sup_q_path), "--certificate", str(sup_cert_path)],
                scratch,
            )
            if sup_ans["returncode"] != 0 or not isinstance(sup_ans["stdout_json"], dict):
                raise RuntimeError(f"supported answer failed: {sup_ans}")
            sup_rep = _run_offline_cli(
                cli_prefix,
                ["verify", "--snapshot", str(snapshot_dir), "--certificate", str(sup_cert_path)],
                scratch,
            )

            abs_ans = _run_offline_cli(
                cli_prefix,
                ["answer", "--snapshot", str(snapshot_dir), "--query", str(abs_q_path), "--certificate", str(abs_cert_path)],
                scratch,
            )
            if abs_ans["returncode"] != 0 or not isinstance(abs_ans["stdout_json"], dict):
                raise RuntimeError(f"abstaining answer failed: {abs_ans}")
            abs_rep = _run_offline_cli(
                cli_prefix,
                ["verify", "--snapshot", str(snapshot_dir), "--certificate", str(abs_cert_path)],
                scratch,
            )

        sup_result_obj = sup_ans["stdout_json"]
        sup_replay_obj = sup_rep["stdout_json"] or {}
        abs_result_obj = abs_ans["stdout_json"]
        abs_replay_obj = abs_rep["stdout_json"] or {}

        sup_res_path.write_text(json.dumps(sup_result_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sup_rep_path.write_text(json.dumps(sup_replay_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        abs_res_path.write_text(json.dumps(abs_result_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        abs_rep_path.write_text(json.dumps(abs_replay_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        egis_res = sup_result_obj.get("result") or {}
        egis_witness = sorted(sup_result_obj.get("witness") or [])
        egis_refs = sorted(sup_result_obj.get("evidence_refs") or [])

        mismatches: list[str] = []
        if not raw_hash_matches_manifest:
            mismatches.append("raw_content_sha256 mismatch between raw.json and manifest.json")
        if sup_result_obj.get("status") != "SUPPORTED":
            mismatches.append(f"supported status={sup_result_obj.get('status')!r} != 'SUPPORTED'")
        if egis_res.get("numerator_count") != expected_num:
            mismatches.append(f"numerator_count={egis_res.get('numerator_count')} != raw={expected_num}")
        if egis_res.get("denominator_count") != expected_den:
            mismatches.append(f"denominator_count={egis_res.get('denominator_count')} != raw={expected_den}")
        if egis_witness != expected_witnesses or egis_refs != expected_witnesses:
            only_raw = sorted(set(expected_witnesses) - set(egis_witness))
            only_egis = sorted(set(egis_witness) - set(expected_witnesses))
            mismatches.append(f"witness set mismatch: only_raw={only_raw[:10]} only_egis={only_egis[:10]}")
        if sup_rep["returncode"] != 0 or sup_replay_obj.get("valid") is not True:
            mismatches.append(f"supported replay failed: rc={sup_rep['returncode']} obj={sup_replay_obj}")

        if abs_result_obj.get("status") != "UNKNOWN":
            mismatches.append(f"abstaining status={abs_result_obj.get('status')!r} != 'UNKNOWN'")
        if abs_result_obj.get("reason_code") != "INCOMPLETE_SNAPSHOT":
            mismatches.append(f"abstaining reason={abs_result_obj.get('reason_code')!r} != 'INCOMPLETE_SNAPSHOT'")
        if abs_result_obj.get("witness") != [] or abs_result_obj.get("evidence_refs") != []:
            mismatches.append("abstaining query emitted non-empty witness/evidence_refs")
        if abs_rep["returncode"] != 0 or abs_replay_obj.get("valid") is not True:
            mismatches.append(f"abstaining replay failed: rc={abs_rep['returncode']} obj={abs_replay_obj}")

        reconciliation = {
            "report_version": "1.0.0",
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "network_mode": "disabled_socket_and_subprocess_guard",
            "snapshot_dir": "snapshots/real/gdc_tcga_luad_v2",
            "snapshot_id": snapshot_id,
            "cohort_id": cohort_id,
            "upstream_data_release": manifest.get("provenance_metadata", {}).get("upstream_status", {}).get("data_release"),
            "raw_content_sha256_verified": raw_hash_matches_manifest,
            "independent_raw_counts": {
                "cases": len(raw_cases),
                "samples_total": len(raw_samples),
                "samples_by_type": sample_type_counts,
                "primary_tumor_samples": expected_den,
                "aliquots_total": len(raw_aliquots),
                "scoped_files_fetched": len(raw_files),
                "qualifying_open_rnaseq_tsv_released_files": len(qualifying_files),
                "all_samples_with_qualifying_file": len(samples_with_qualifying_file),
                "primary_tumor_samples_with_qualifying_file": expected_num,
                "primary_tumor_samples_without_qualifying_file": expected_den - expected_num,
                "orphan_sample_links": orphan_sample_links,
                "orphan_aliquot_links": orphan_aliquot_links,
            },
            "egis_supported_comparison": {
                "status": sup_result_obj.get("status"),
                "reason_code": sup_result_obj.get("reason_code"),
                "entity_level": egis_res.get("entity_level"),
                "denominator_basis": egis_res.get("denominator_basis"),
                "numerator_count": egis_res.get("numerator_count"),
                "denominator_count": egis_res.get("denominator_count"),
                "percentage": egis_res.get("percentage"),
                "witness_count": len(egis_witness),
                "exact_witness_set_match": egis_witness == expected_witnesses,
                "replay_valid": sup_replay_obj.get("valid"),
            },
            "egis_abstaining_comparison": {
                "status": abs_result_obj.get("status"),
                "reason_code": abs_result_obj.get("reason_code"),
                "witness_count": len(abs_result_obj.get("witness") or []),
                "replay_valid": abs_replay_obj.get("valid"),
            },
            "mismatches": mismatches,
            "reconciled": len(mismatches) == 0,
        }
        rec_path = out_dir / "source_reconciliation_report.json"
        rec_path.write_text(json.dumps(reconciliation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"reconciled": reconciliation["reconciled"], "mismatches": mismatches, "independent_raw_counts": reconciliation["independent_raw_counts"], "egis_supported_comparison": reconciliation["egis_supported_comparison"], "egis_abstaining_comparison": reconciliation["egis_abstaining_comparison"], "report_path": str(rec_path)}, indent=2, sort_keys=True))
        return 1 if mismatches else 0
    finally:
        socket.socket = orig_socket  # type: ignore[assignment]
        socket.create_connection = orig_create  # type: ignore[assignment]
        socket.getaddrinfo = orig_addr  # type: ignore[assignment]
        urllib.request.urlopen = orig_urlopen  # type: ignore[assignment]


if __name__ == "__main__":
    raise SystemExit(main())
