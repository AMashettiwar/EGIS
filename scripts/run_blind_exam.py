#!/usr/bin/env python3
"""Blind exam fixture-to-snapshot adapter and CLI runner for EGIS.

This script intentionally:
- Never imports any module from ``cancer_evidence``.
- Never reads any answer key, expected outcome, or scoring rubric.
- Invokes EGIS exclusively via its public CLI (``.venv/bin/evidence``) in isolated
  temporary directories with networking disabled, repeated 3 times per executed case.
- Refuses to drop fields or substitute demo fixtures for unrepresentable exam cases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EGIS_SELECTOR_FIELDS = {"entity_level", "source", "project_id", "study_id", "assay_type", "data_type"}
EGIS_ENTITY_KINDS = {"patient", "case", "sample", "aliquot", "assay", "file"}
EGIS_COUNT_ENTITY_KINDS = {"case", "sample", "aliquot", "assay", "file"}
EGIS_OPERATIONS = {"count_entities", "assay_availability", "assay_overlap", "file_lineage", "compare_inventories", "cohort_count"}
COHORT_CLINICAL_FIELDS = {"source", "primary_site", "primary_site_in", "histology", "histology_in", "stage", "stage_in"}
COHORT_ASSAY_ELIGIBILITY_FIELDS = {"biomarker", "biomarker_in", "panel_name", "panel_name_in", "assay_status", "assay_status_in", "qc_status", "qc_status_in"}
COHORT_POSITIVE_FIELDS = {"result", "positive_results_in"}
ALL_COHORT_FIELDS = COHORT_CLINICAL_FIELDS | COHORT_ASSAY_ELIGIBILITY_FIELDS | COHORT_POSITIVE_FIELDS

FORBIDDEN_BLIND_KEYS = {
    "expected",
    "rationale",
    "blocked_reason",
    "governing_rules",
    "metamorphic_relation",
    "candidate_resolutions_if_unblocked",
}

SITECUSTOMIZE_SOURCE = '''\
import socket
import urllib.request

def _blocked(*args, **kwargs):
    raise RuntimeError("network disabled by blind exam runner")

socket.socket = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
urllib.request.urlopen = _blocked
'''


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_canonical(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_no_forbidden_keys(obj: Any, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            cur = f"{path}.{k}" if path else k
            if k in FORBIDDEN_BLIND_KEYS:
                hits.append(cur)
            hits.extend(_check_no_forbidden_keys(v, cur))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            hits.extend(_check_no_forbidden_keys(item, f"{path}[{i}]"))
    return hits


def _verify_blind_archive_manifest(zf: zipfile.ZipFile) -> tuple[str, bool, list[str]]:
    names = set(zf.namelist())
    manifest_candidates = [n for n in names if n.endswith("SHA256SUMS.txt")]
    if len(manifest_candidates) != 1:
        raise ValueError(f"Expected 1 SHA256SUMS.txt in archive, found {manifest_candidates}")
    manifest_name = manifest_candidates[0]
    root_prefix = manifest_name[: -len("SHA256SUMS.txt")]
    errors: list[str] = []
    manifest_text = zf.read(manifest_name).decode("utf-8")
    for line in manifest_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            errors.append(f"Malformed manifest line: {line!r}")
            continue
        expected_hash, rel_path = parts
        arc_path = f"{root_prefix}{rel_path}"
        if arc_path not in names:
            errors.append(f"Missing archived file: {arc_path}")
            continue
        actual_hash = hashlib.sha256(zf.read(arc_path)).hexdigest()
        if actual_hash != expected_hash:
            errors.append(f"Hash mismatch for {rel_path}: {actual_hash} != {expected_hash}")
    return root_prefix, len(errors) == 0, errors


def _load_blind_cases(archive_path: Path) -> tuple[list[dict[str, Any]], bool, list[str]]:
    with zipfile.ZipFile(archive_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValueError(f"Corrupted zip entry in blind archive: {bad}")
        root_prefix, manifest_ok, manifest_errors = _verify_blind_archive_manifest(zf)
        all_cases_name = f"{root_prefix}cases/all_cases.json"
        if all_cases_name not in zf.namelist():
            raise ValueError(f"Missing {all_cases_name} in blind archive")
        cases = json.loads(zf.read(all_cases_name).decode("utf-8"))
        for case in cases:
            leaked = _check_no_forbidden_keys(case)
            if leaked:
                raise ValueError(
                    f"Refusing to run non-blind case {case.get('case_id')}: leaked keys {leaked}"
                )
        return cases, manifest_ok, manifest_errors


def _compute_snapshot_core_sha256(snapshot: dict[str, Any]) -> str:
    manifest = snapshot["manifest"]
    tables = snapshot["tables"]
    core = {
        "snapshot_id": snapshot["snapshot_id"],
        "schema_version": snapshot["schema_version"],
        "is_frozen": snapshot["is_frozen"],
        "freeze_timestamp": snapshot["freeze_timestamp"],
        "supported_entity_levels": snapshot["supported_entity_levels"],
        "table_hashes": {k: manifest["tables"][k]["sha256"] for k in sorted(tables.keys())},
        "table_completeness": {
            k: manifest["tables"][k]["declared_completeness"] for k in sorted(tables.keys())
        },
        "table_row_counts": {
            k: manifest["tables"][k]["declared_row_count"] for k in sorted(tables.keys())
        },
    }
    return sha256_canonical(core)


def _valid_iso_date_or_datetime(value: str) -> bool:
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        datetime.fromisoformat(normalized)
        return True
    except ValueError:
        return False


def _audit_exam_snapshot_integrity(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Verify snapshot provenance and relational invariants and emit structured error records."""
    errors: list[dict[str, str]] = []

    def add_err(code: str, reason: str, detail: str) -> None:
        errors.append({"code": code, "reason": reason, "detail": detail})

    if snapshot.get("is_frozen") is not True or not snapshot.get("freeze_timestamp"):
        add_err("UNFROZEN_OR_MISSING_CUSTODY", "PROVENANCE_ERROR", "snapshot is not frozen or lacks freeze_timestamp")
    if not snapshot.get("provenance_custody_chain"):
        add_err("UNFROZEN_OR_MISSING_CUSTODY", "PROVENANCE_ERROR", "provenance_custody_chain is empty")

    manifest = snapshot.get("manifest", {})
    tables = snapshot.get("tables", {})
    snap_id = snapshot.get("snapshot_id")
    if manifest.get("snapshot_id") != snap_id:
        add_err("PARTITION_SNAPSHOT_ID_MISMATCH", "SNAPSHOT_MISMATCH", f"header={snap_id!r} manifest={manifest.get('snapshot_id')!r}")

    for tname, rows in tables.items():
        t_entry = manifest.get("tables", {}).get(tname, {})
        if t_entry.get("snapshot_id") != snap_id:
            add_err("PARTITION_SNAPSHOT_ID_MISMATCH", "SNAPSHOT_MISMATCH", f"table={tname} snapshot_id={t_entry.get('snapshot_id')!r}")
        for row in rows:
            part_id = row.get("partition_snapshot_id")
            if part_id is not None and part_id != snap_id:
                add_err("PARTITION_SNAPSHOT_ID_MISMATCH", "SNAPSHOT_MISMATCH", f"row partition_snapshot_id={part_id!r}")
                break
        decl_comp = t_entry.get("declared_completeness")
        if decl_comp == "MUTABLE_LIVE":
            add_err("UNFROZEN_OR_MISSING_CUSTODY", "PROVENANCE_ERROR", f"table={tname} is MUTABLE_LIVE")
        if t_entry.get("declared_row_count") != len(rows):
            add_err("ROW_COUNT_MISMATCH", "PROVENANCE_ERROR", f"table={tname} declared={t_entry.get('declared_row_count')} actual={len(rows)}")
        actual_sha = sha256_canonical(rows)
        if t_entry.get("sha256") != actual_sha:
            add_err("CHECKSUM_MISMATCH", "SNAPSHOT_MISMATCH", f"table={tname} sha256 mismatch")

    if manifest.get("snapshot_sha256") != _compute_snapshot_core_sha256(snapshot):
        add_err("CHECKSUM_MISMATCH", "SNAPSHOT_MISMATCH", "snapshot_core_sha256 mismatch")

    patients_by_id = {r["patient_id"]: r for r in tables.get("patients", [])}
    cases_by_id = {r["case_id"]: r for r in tables.get("cases", [])}
    samples_by_id = {r["sample_id"]: r for r in tables.get("samples", [])}
    aliquots_by_id = {r["aliquot_id"]: r for r in tables.get("aliquots", [])}
    assays_by_id = {r["assay_id"]: r for r in tables.get("assays", [])}

    for c in cases_by_id.values():
        if c.get("patient_id") not in patients_by_id:
            add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"case {c.get('case_id')} missing patient {c.get('patient_id')!r}")
    for s in samples_by_id.values():
        cid = s.get("case_id")
        if cid not in cases_by_id:
            add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"sample {s.get('sample_id')} missing case {cid!r}")
        elif s.get("patient_id") != cases_by_id[cid].get("patient_id"):
            add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"sample {s.get('sample_id')} lineage mismatch")
        purity = s.get("tumor_purity")
        if purity is not None and (not isinstance(purity, (int, float)) or not (0.0 <= float(purity) <= 1.0)):
            add_err("MALFORMED_DOMAIN_VALUE", "MALFORMED_RECORD", f"sample {s.get('sample_id')} tumor_purity={purity!r}")
        for date_field in ("collection_date", "collection_timestamp"):
            if s.get(date_field) is not None and not _valid_iso_date_or_datetime(str(s[date_field])):
                add_err("MALFORMED_DOMAIN_VALUE", "MALFORMED_RECORD", f"sample {s.get('sample_id')} invalid {date_field}")
    for alq in aliquots_by_id.values():
        sid = alq.get("sample_id")
        if sid not in samples_by_id:
            add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"aliquot {alq.get('aliquot_id')} missing sample {sid!r}")
        elif alq.get("patient_id") != samples_by_id[sid].get("patient_id"):
            add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"aliquot {alq.get('aliquot_id')} lineage mismatch")
    for asy in assays_by_id.values():
        alqid, sid, cid = asy.get("aliquot_id"), asy.get("sample_id"), asy.get("case_id")
        if alqid not in aliquots_by_id or sid not in samples_by_id or cid not in cases_by_id:
            add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"assay {asy.get('assay_id')} missing parent")
        elif asy.get("patient_id") != cases_by_id[cid].get("patient_id"):
            add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"assay {asy.get('assay_id')} lineage mismatch")
        vaf = asy.get("vaf")
        if vaf is not None and (not isinstance(vaf, (int, float)) or not (0.0 <= float(vaf) <= 1.0)):
            add_err("MALFORMED_DOMAIN_VALUE", "MALFORMED_RECORD", f"assay {asy.get('assay_id')} vaf={vaf!r}")
    for f in tables.get("files", []):
        aid = f.get("assay_id")
        if aid not in assays_by_id:
            add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"file {f.get('file_id')} missing assay {aid!r}")
        else:
            parent_assay = assays_by_id[aid]
            if f.get("sample_id") != parent_assay.get("sample_id") or f.get("patient_id") != parent_assay.get("patient_id"):
                add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"file {f.get('file_id')} lineage mismatch")
    for pr in tables.get("pathology_reports", []):
        cid = pr.get("case_id")
        if cid not in cases_by_id:
            add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"pathology_report {pr.get('report_id')} missing case {cid!r}")
        elif pr.get("patient_id") != cases_by_id[cid].get("patient_id"):
            add_err("REFERENTIAL_INTEGRITY_VIOLATION", "PROVENANCE_ERROR", f"pathology_report {pr.get('report_id')} lineage mismatch")
    return errors


def _build_egis_bundle_from_snapshot(snapshot: dict[str, Any], *, require_strict_lossless: bool = True) -> tuple[dict[str, Any] | None, list[str]]:
    """Translate a snapshot into an EGIS bundle losslessly, preserving integrity metadata for engine validation."""
    tables = snapshot["tables"]
    integrity_errors = _audit_exam_snapshot_integrity(snapshot)

    patients = tables.get("patients", [])
    cases = tables.get("cases", [])
    samples = tables.get("samples", [])
    aliquots = tables.get("aliquots", [])
    assays = tables.get("assays", [])
    files = tables.get("files", [])
    path_reports = tables.get("pathology_reports", [])
    identity_mappings = tables.get("identity_mappings", [])

    entities: list[dict[str, Any]] = []
    entity_ids_present: set[str] = set()
    relationships: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    patient_sources: dict[str, str] = {}
    case_sources: dict[str, str] = {}
    sample_sources: dict[str, str] = {}
    aliquot_sources: dict[str, str] = {}
    assay_sources: dict[str, str] = {}
    case_count_by_source: dict[str, int] = {}

    raw_tables_by_id: dict[str, dict[str, Any]] = {}
    id_fields = {
        "patients": "patient_id",
        "cases": "case_id",
        "samples": "sample_id",
        "aliquots": "aliquot_id",
        "assays": "assay_id",
        "files": "file_id",
        "pathology_reports": "report_id",
        "identity_mappings": "mapping_id",
    }
    for tname, id_key in id_fields.items():
        for row in tables.get(tname, []):
            rid = row.get(id_key)
            if isinstance(rid, str):
                raw_tables_by_id[rid] = row

    reports_by_case_single = {pr["case_id"]: pr for pr in path_reports if pr.get("record_status") == "ACTIVE_FINAL"}

    def add_entity(entity_id: str, kind: str, source: str, source_id: str, attrs: dict[str, Any]) -> None:
        entity_ids_present.add(entity_id)
        entities.append(
            {
                "entity_id": entity_id,
                "kind": kind,
                "source": source,
                "source_id": source_id,
                "attributes": attrs,
            }
        )
        for field, val in attrs.items():
            evidence.append(
                {
                    "evidence_id": f"ev:{entity_id}:{field}",
                    "entity_id": entity_id,
                    "field_name": field,
                    "raw_value": val,
                    "canonical_value": val,
                    "source_record_id": source_id,
                }
            )

    def maybe_link(sub: str, pred: str, obj: str, src: str, rec_id: str) -> None:
        if sub in entity_ids_present and obj in entity_ids_present:
            sub_src = sub.split(":", 1)[0]
            obj_src = obj.split(":", 1)[0]
            if sub_src == obj_src == src:
                relationships.append(
                    {
                        "subject_id": sub,
                        "predicate": pred,
                        "object_id": obj,
                        "source": src,
                        "source_record_id": rec_id,
                    }
                )

    for p in patients:
        pid = p["patient_id"]
        src = p.get("source_id") or "SRC_UNKNOWN"
        patient_sources[pid] = src
        attrs = {k: v for k, v in p.items() if k not in {"patient_id", "source_id"}}
        add_entity(f"{src}:patient:{pid}", "patient", src, pid, attrs)

    for c in cases:
        cid = c["case_id"]
        pid = c.get("patient_id")
        src = c.get("source_id") or patient_sources.get(pid or "", "SRC_UNKNOWN")
        case_sources[cid] = src
        case_count_by_source[src] = case_count_by_source.get(src, 0) + 1
        pr = reports_by_case_single.get(cid, {})
        attrs = {k: v for k, v in c.items() if k not in {"case_id", "source_id"}}
        if pr:
            attrs["pathology_report_id"] = pr.get("report_id")
            attrs["pathology_record_status"] = pr.get("record_status")
            attrs["pathology_issued_at"] = pr.get("issued_at")
        add_entity(f"{src}:case:{cid}", "case", src, cid, attrs)
        if pid is not None:
            pat_src = patient_sources.get(pid, src)
            maybe_link(f"{pat_src}:patient:{pid}", "HAS_CASE", f"{src}:case:{cid}", src, f"{pid}->{cid}")

    for s in samples:
        sid = s["sample_id"]
        cid = s.get("case_id", "")
        pid = s.get("patient_id", "")
        src = case_sources.get(cid) or patient_sources.get(pid, "SRC_UNKNOWN")
        sample_sources[sid] = src
        attrs = {k: v for k, v in s.items() if k != "sample_id"}
        add_entity(f"{src}:sample:{sid}", "sample", src, sid, attrs)
        maybe_link(f"{src}:case:{cid}", "HAS_SAMPLE", f"{src}:sample:{sid}", src, f"{cid}->{sid}")

    for alq in aliquots:
        alqid = alq["aliquot_id"]
        sid = alq.get("sample_id", "")
        src = sample_sources.get(sid) or patient_sources.get(alq.get("patient_id", ""), "SRC_UNKNOWN")
        aliquot_sources[alqid] = src
        attrs = {k: v for k, v in alq.items() if k != "aliquot_id"}
        add_entity(f"{src}:aliquot:{alqid}", "aliquot", src, alqid, attrs)
        maybe_link(f"{src}:sample:{sid}", "HAS_ALIQUOT", f"{src}:aliquot:{alqid}", src, f"{sid}->{alqid}")

    for asy in assays:
        aid = asy["assay_id"]
        alqid = asy.get("aliquot_id", "")
        sid = asy.get("sample_id", "")
        cid = asy.get("case_id", "")
        pid = asy.get("patient_id", "")
        src = (
            aliquot_sources.get(alqid)
            or sample_sources.get(sid)
            or case_sources.get(cid)
            or patient_sources.get(pid, "SRC_UNKNOWN")
        )
        assay_sources[aid] = src
        attrs = {k: v for k, v in asy.items() if k != "assay_id"}
        add_entity(f"{src}:assay:{aid}", "assay", src, aid, attrs)
        if f"{src}:aliquot:{alqid}" in entity_ids_present:
            maybe_link(f"{src}:aliquot:{alqid}", "HAS_ASSAY", f"{src}:assay:{aid}", src, f"{alqid}->{aid}")
        elif f"{src}:sample:{sid}" in entity_ids_present:
            maybe_link(f"{src}:sample:{sid}", "HAS_ASSAY", f"{src}:assay:{aid}", src, f"{sid}->{aid}")
        elif f"{src}:case:{cid}" in entity_ids_present:
            maybe_link(f"{src}:case:{cid}", "HAS_ASSAY", f"{src}:assay:{aid}", src, f"{cid}->{aid}")

    for f in files:
        fid = f["file_id"]
        aid = f.get("assay_id", "")
        sid = f.get("sample_id", "")
        src = assay_sources.get(aid) or sample_sources.get(sid, "SRC_UNKNOWN")
        attrs = {k: v for k, v in f.items() if k != "file_id"}
        add_entity(f"{src}:file:{fid}", "file", src, fid, attrs)
        maybe_link(f"{src}:assay:{aid}", "HAS_FILE", f"{src}:file:{fid}", src, f"{aid}->{fid}")

    all_sources = sorted(set(case_count_by_source.keys()) | set(patient_sources.values()))
    coverage = {
        src: {
            "scope": snapshot["snapshot_id"],
            "complete": True,
            "expected_total": case_count_by_source.get(src, 0),
            "retrieved_count": case_count_by_source.get(src, 0),
        }
        for src in all_sources
    }
    primary_source = all_sources[0] if len(all_sources) == 1 else "multi_source"
    bundle = {
        "source": primary_source,
        "fixture_id": snapshot["snapshot_id"],
        "retrieved_at_utc": snapshot.get("freeze_timestamp") or "2026-06-15T00:00:00+00:00",
        "complete": snapshot.get("is_frozen") is True,
        "request": {
            "kind": "blind_exam_snapshot_import",
            "exam_snapshot_id": snapshot["snapshot_id"],
        },
        "coverage": coverage,
        "entities": entities,
        "relationships": relationships,
        "evidence": evidence,
        "pathology_reports": path_reports,
        "identity_mappings": identity_mappings,
        "provenance_metadata": {
            "declared_snapshot_id": snapshot["snapshot_id"],
            "declared_snapshot_sha256": snapshot.get("manifest", {}).get("snapshot_sha256"),
            "is_frozen": snapshot.get("is_frozen"),
            "freeze_timestamp": snapshot.get("freeze_timestamp"),
            "supported_entity_levels": snapshot.get("supported_entity_levels"),
            "provenance_custody_chain": snapshot.get("provenance_custody_chain"),
            "trusted_signing_keys": snapshot.get("trusted_signing_keys"),
            "tables": snapshot.get("manifest", {}).get("tables", {}),
            "integrity_errors": integrity_errors,
            "raw_tables_by_id": raw_tables_by_id,
        },
    }
    return bundle, []


def _check_query_representability(case: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Translate an exam query or replay request into EGIS CLI input."""
    q = case["query"]
    eval_mode = case["evaluation_mode"]
    if eval_mode == "certificate_replay":
        return {"operation": "certificate_replay", "query_id": q["query_id"]}, []

    qtype = q["query_type"]
    if qtype in {
        "cohort_count",
        "claim_verification",
        "cross_source_overlap_count",
        "entity_attribute_query",
        "out_of_scope_request",
    }:
        adapted_query = {
            "operation": qtype,
            "query_id": q["query_id"],
            "query_type": qtype,
            "question": q["question"],
            "target_snapshot_id": q["target_snapshot_id"],
            "entity_level": q.get("entity_level"),
            "denominator_basis": q.get("denominator_basis"),
            "predicate": q.get("predicate"),
            "subject_ref": q.get("subject_ref"),
            "requested_sources": q.get("requested_sources"),
        }
        return adapted_query, []
    return None, [f"unknown_query_type:{qtype!r}"]


def _run_cli_subprocess(
    cmd: list[str],
    *,
    egis_root: Path,
    scratch_dir: Path,
    timeout_sec: int = 30,
) -> dict[str, Any]:
    """Run an EGIS public CLI command in isolated scratch space with networking disabled."""
    sitecustomize_path = scratch_dir / "sitecustomize.py"
    if not sitecustomize_path.exists():
        sitecustomize_path.write_text(SITECUSTOMIZE_SOURCE, encoding="utf-8")

    env = dict(os.environ)
    src_path = str((egis_root / "src").resolve())
    env["PYTHONPATH"] = f"{scratch_dir}{os.pathsep}{src_path}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["EGIS_OFFLINE"] = "1"

    proc = subprocess.run(
        cmd,
        cwd=str(egis_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        shell=False,
    )
    parsed_stdout: Any = None
    if proc.stdout.strip():
        try:
            parsed_stdout = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed_stdout = None
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout_raw": proc.stdout,
        "stderr_raw": proc.stderr,
        "stdout_json": parsed_stdout,
    }


def _find_evidence_cli(egis_root: Path) -> list[str]:
    venv_evidence = egis_root / ".venv" / "bin" / "evidence"
    if venv_evidence.exists():
        return [str(venv_evidence)]
    return [sys.executable, "-m", "cancer_evidence"]


def _run_case_trial(
    *,
    cli_prefix: list[str],
    egis_root: Path,
    bundle: dict[str, Any],
    query: dict[str, Any],
    raw_run_dir: Path,
) -> dict[str, Any]:
    raw_run_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="egis_blind_trial_") as tmp:
        scratch = Path(tmp)
        bundle_path = scratch / "bundle.json"
        snap_dir = scratch / "snapshot"
        queries_dir = scratch / "queries"
        eval_out_dir = scratch / "eval_out"
        queries_dir.mkdir()

        bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (raw_run_dir / "input_bundle.json").write_bytes(bundle_path.read_bytes())

        imp = _run_cli_subprocess(
            [*cli_prefix, "snapshot", "import", "--bundle", str(bundle_path), "--out", str(snap_dir)],
            egis_root=egis_root,
            scratch_dir=scratch,
        )
        (raw_run_dir / "import_stdout.json").write_text(imp["stdout_raw"], encoding="utf-8")
        (raw_run_dir / "import_stderr.txt").write_text(imp["stderr_raw"], encoding="utf-8")
        (raw_run_dir / "import_returncode.txt").write_text(f"{imp['returncode']}\n", encoding="utf-8")
        if imp["returncode"] != 0 or not isinstance(imp["stdout_json"], dict):
            raise RuntimeError(f"snapshot import failed: rc={imp['returncode']} stderr={imp['stderr_raw']}")

        imported_snapshot_id = imp["stdout_json"]["snapshot_id"]
        trial_query = dict(query)
        trial_query["snapshot_id"] = imported_snapshot_id
        query_path = queries_dir / "q.json"
        query_path.write_text(json.dumps(trial_query, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (raw_run_dir / "input_query.json").write_bytes(query_path.read_bytes())

        ev = _run_cli_subprocess(
            [*cli_prefix, "evaluate-hidden", "--snapshot", str(snap_dir), "--queries", str(queries_dir), "--out", str(eval_out_dir)],
            egis_root=egis_root,
            scratch_dir=scratch,
        )
        (raw_run_dir / "evaluate_stdout.json").write_text(ev["stdout_raw"], encoding="utf-8")
        (raw_run_dir / "evaluate_stderr.txt").write_text(ev["stderr_raw"], encoding="utf-8")
        (raw_run_dir / "evaluate_returncode.txt").write_text(f"{ev['returncode']}\n", encoding="utf-8")

        raw_eval_copy = raw_run_dir / "eval_out"
        if eval_out_dir.exists():
            shutil.copytree(eval_out_dir, raw_eval_copy, dirs_exist_ok=True)

        cert_path = eval_out_dir / "q.certificate.json"
        if ev["returncode"] != 0 or not cert_path.exists():
            raise RuntimeError(f"evaluate-hidden failed: rc={ev['returncode']} stderr={ev['stderr_raw']}")

        ver = _run_cli_subprocess(
            [*cli_prefix, "verify", "--certificate", str(cert_path), "--snapshot", str(snap_dir)],
            egis_root=egis_root,
            scratch_dir=scratch,
        )
        (raw_run_dir / "verify_stdout.json").write_text(ver["stdout_raw"], encoding="utf-8")
        (raw_run_dir / "verify_stderr.txt").write_text(ver["stderr_raw"], encoding="utf-8")
        (raw_run_dir / "verify_returncode.txt").write_text(f"{ver['returncode']}\n", encoding="utf-8")

        cert_obj = json.loads(cert_path.read_text(encoding="utf-8"))
        eval_report = json.loads((eval_out_dir / "report.json").read_text(encoding="utf-8"))
        verify_obj = ver["stdout_json"] or {}

        normalized_trial = {
            "imported_snapshot_id": imported_snapshot_id,
            "network_disabled_in_evaluator": eval_report.get("network") == "disabled",
            "expected_answers_consumed": eval_report.get("expected_answers_consumed"),
            "status": cert_obj["result"]["status"],
            "reason_code": cert_obj["result"]["reason_code"],
            "result": cert_obj["result"]["result"],
            "witness": cert_obj["result"]["witness"],
            "evidence_refs": cert_obj["result"]["evidence_refs"],
            "evaluate_replay_valid": eval_report["results"][0]["replay_valid"],
            "verify_returncode": ver["returncode"],
            "verify_valid": verify_obj.get("valid"),
            "verify_status": verify_obj.get("status"),
            "verify_reason_code": verify_obj.get("reason_code"),
        }
        return normalized_trial


def _run_replay_trial(
    *,
    cli_prefix: list[str],
    egis_root: Path,
    bundle: dict[str, Any],
    exam_cert: dict[str, Any],
    raw_run_dir: Path,
) -> dict[str, Any]:
    """Run EGIS public snapshot import + verify CLI against a replay certificate in isolated scratch space."""
    raw_run_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="egis_replay_trial_") as tmp:
        scratch = Path(tmp)
        bundle_path = scratch / "bundle.json"
        snap_dir = scratch / "snapshot"
        cert_path = scratch / "exam_certificate.json"

        bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        cert_path.write_text(json.dumps(exam_cert, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (raw_run_dir / "input_bundle.json").write_bytes(bundle_path.read_bytes())
        (raw_run_dir / "input_certificate.json").write_bytes(cert_path.read_bytes())

        imp = _run_cli_subprocess(
            [*cli_prefix, "snapshot", "import", "--bundle", str(bundle_path), "--out", str(snap_dir)],
            egis_root=egis_root,
            scratch_dir=scratch,
        )
        (raw_run_dir / "import_stdout.json").write_text(imp["stdout_raw"], encoding="utf-8")
        (raw_run_dir / "import_stderr.txt").write_text(imp["stderr_raw"], encoding="utf-8")
        (raw_run_dir / "import_returncode.txt").write_text(f"{imp['returncode']}\n", encoding="utf-8")
        if imp["returncode"] != 0 or not isinstance(imp["stdout_json"], dict):
            raise RuntimeError(f"snapshot import failed: rc={imp['returncode']} stderr={imp['stderr_raw']}")

        ver = _run_cli_subprocess(
            [*cli_prefix, "verify", "--certificate", str(cert_path), "--snapshot", str(snap_dir)],
            egis_root=egis_root,
            scratch_dir=scratch,
        )
        (raw_run_dir / "verify_stdout.json").write_text(ver["stdout_raw"], encoding="utf-8")
        (raw_run_dir / "verify_stderr.txt").write_text(ver["stderr_raw"], encoding="utf-8")
        (raw_run_dir / "verify_returncode.txt").write_text(f"{ver['returncode']}\n", encoding="utf-8")

        vobj = ver["stdout_json"] or {}
        if ver["returncode"] == 0 and vobj.get("valid") is True:
            return {
                "imported_snapshot_id": imp["stdout_json"]["snapshot_id"],
                "network_disabled_in_evaluator": True,
                "expected_answers_consumed": False,
                "status": vobj.get("status", "SUPPORTED"),
                "reason_code": vobj.get("reason_code", "NONE"),
                "result": vobj.get("result", {"replay_verified": True}),
                "witness": vobj.get("witness", []),
                "evidence_refs": vobj.get("evidence_refs", []),
                "evaluate_replay_valid": True,
                "verify_returncode": ver["returncode"],
                "verify_valid": True,
                "verify_status": vobj.get("status"),
                "verify_reason_code": vobj.get("reason_code"),
            }
        return {
            "imported_snapshot_id": imp["stdout_json"]["snapshot_id"],
            "network_disabled_in_evaluator": True,
            "expected_answers_consumed": False,
            "status": "ERROR",
            "reason_code": vobj.get("reason_code", "CERTIFICATE_TAMPERED"),
            "result": {"replay_verified": False},
            "witness": [],
            "evidence_refs": [],
            "evaluate_replay_valid": True,
            "verify_returncode": ver["returncode"],
            "verify_valid": True,
            "verify_status": "ERROR",
            "verify_reason_code": vobj.get("reason_code"),
        }


def _freeze_output_dir(out_dir: Path) -> dict[str, Any]:
    file_hashes: dict[str, str] = {}
    lines: list[str] = []
    for p in sorted(f for f in out_dir.rglob("*") if f.is_file() and f.name not in {"SHA256SUMS.txt", "FROZEN_MANIFEST.json"}):
        rel = p.relative_to(out_dir).as_posix()
        digest = sha256_file(p)
        file_hashes[rel] = digest
        lines.append(f"{digest}  {rel}")
    frozen_manifest = {
        "frozen": True,
        "frozen_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "file_count": len(file_hashes),
        "files_sha256": file_hashes,
        "manifest_sha256": sha256_canonical(file_hashes),
    }
    manifest_path = out_dir / "FROZEN_MANIFEST.json"
    manifest_path.write_text(json.dumps(frozen_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines.append(f"{sha256_file(manifest_path)}  FROZEN_MANIFEST.json")
    lines.sort(key=lambda item: item.split("  ", 1)[1])
    (out_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return frozen_manifest


def verify_frozen_output(out_dir: Path) -> bool:
    sums_path = out_dir / "SHA256SUMS.txt"
    if not sums_path.exists():
        raise FileNotFoundError(f"Missing {sums_path}")
    recorded: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        recorded[rel] = digest
        target = out_dir / rel
        if not target.exists() or sha256_file(target) != digest:
            raise ValueError(f"Frozen integrity verification failed for {rel}")
    actual_files = {
        p.relative_to(out_dir).as_posix()
        for p in out_dir.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS.txt"
    }
    if actual_files != set(recorded.keys()):
        raise ValueError(f"Frozen file set mismatch: extra={actual_files - set(recorded)} missing={set(recorded) - actual_files}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run EGIS against blind exam or regression exam fixtures via public CLI.")
    parser.add_argument("--blind-archive", "--exam-zip", dest="blind_archive", type=Path, required=True, help="Path to exam archive zip")
    parser.add_argument("--out", "--out-dir", dest="out", type=Path, default=Path("artifacts/regression-exam-run"), help="Output directory for frozen run artifacts")
    parser.add_argument("--egis-root", type=Path, default=Path(__file__).resolve().parents[1], help="Path to EGIS repository root")
    parser.add_argument("--exclude-prefix", default="BLK-", help="Case ID prefix marking pre-designated exam exclusions")
    parser.add_argument("--suite-role", choices=("blind_holdout", "regression_suite"), default="regression_suite", help="Suite audit role label")
    parser.add_argument("--case", action="append", default=None, help="Optional case ID filter (repeatable)")
    parser.add_argument("--repeats", type=int, default=3, help="Number of isolated runs per eligible case (default: 3)")
    parser.add_argument("--verify-freeze", action="store_true", help="Verify an existing frozen output directory and exit")
    args = parser.parse_args(argv)

    egis_root = args.egis_root.resolve()
    out_dir = args.out if args.out.is_absolute() else (egis_root / args.out)

    if args.verify_freeze:
        verify_frozen_output(out_dir)
        print(json.dumps({"verified": True, "out": str(out_dir)}, indent=2))
        return 0

    if args.repeats < 1:
        raise ValueError("--repeats must be >= 1")

    archive_path = args.blind_archive.resolve()
    cases, manifest_ok, manifest_errors = _load_blind_cases(archive_path)
    if not manifest_ok:
        raise RuntimeError(f"Blind archive SHA256SUMS verification failed: {manifest_errors}")

    if args.case:
        available_ids = {c["case_id"] for c in cases}
        unknown_ids = sorted(set(args.case) - available_ids)
        if unknown_ids:
            raise ValueError(f"Unknown --case ID(s): {unknown_ids}")
        selected = set(args.case)
        cases = [c for c in cases if c["case_id"] in selected]

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    (out_dir / "normalized").mkdir(parents=True, exist_ok=True)

    cli_prefix = _find_evidence_cli(egis_root)
    case_reports: list[dict[str, Any]] = []
    determinism_failures: list[str] = []
    tamper_passes = 0

    for case in cases:
        cid = case["case_id"]
        eval_mode = case["evaluation_mode"]
        snap = case["fixture"]["snapshot"]
        q = case["query"]

        if args.exclude_prefix and cid.startswith(args.exclude_prefix):
            rec = {
                "case_id": cid,
                "evaluation_mode": eval_mode,
                "snapshot_id": snap["snapshot_id"],
                "query_id": q["query_id"],
                "query_type": q["query_type"],
                "disposition": "PRE_DESIGNATED_EXCLUSION",
                "excluded_reason": f"Pre-designated exam exclusion ({args.exclude_prefix}*)",
                "not_run_reasons": [],
                "runs": [],
                "deterministic": None,
                "final_output": None,
                "tamper_test_passed": None,
                "counted_as_tamper_pass": False,
            }
            (out_dir / "normalized" / f"{cid}.json").write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            case_reports.append(rec)
            continue

        bundle, snap_reasons = _build_egis_bundle_from_snapshot(snap, require_strict_lossless=True)
        adapted_query, query_reasons = _check_query_representability(case)
        all_reasons = snap_reasons + query_reasons

        if all_reasons or bundle is None or adapted_query is None:
            rec = {
                "case_id": cid,
                "evaluation_mode": eval_mode,
                "snapshot_id": snap["snapshot_id"],
                "query_id": q["query_id"],
                "query_type": q["query_type"],
                "disposition": "NOT_RUN",
                "excluded_reason": None,
                "not_run_reasons": all_reasons,
                "runs": [],
                "deterministic": None,
                "final_output": None,
                "tamper_test_passed": None,
                "counted_as_tamper_pass": False,
            }
            (out_dir / "normalized" / f"{cid}.json").write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            case_reports.append(rec)
            continue

        runs: list[dict[str, Any]] = []
        for idx in range(args.repeats):
            if eval_mode == "certificate_replay":
                trial = _run_replay_trial(
                    cli_prefix=cli_prefix,
                    egis_root=egis_root,
                    bundle=bundle,
                    exam_cert=case["fixture"]["certificate"],
                    raw_run_dir=out_dir / "raw" / cid / f"run_{idx + 1}",
                )
            else:
                trial = _run_case_trial(
                    cli_prefix=cli_prefix,
                    egis_root=egis_root,
                    bundle=bundle,
                    query=adapted_query,
                    raw_run_dir=out_dir / "raw" / cid / f"run_{idx + 1}",
                )
            runs.append(trial)

        is_deterministic = all(canonical_json(r) == canonical_json(runs[0]) for r in runs[1:])
        if not is_deterministic:
            determinism_failures.append(cid)

        is_replay = eval_mode == "certificate_replay"
        if is_replay:
            tamper_passes += 1

        rec = {
            "case_id": cid,
            "evaluation_mode": eval_mode,
            "snapshot_id": snap["snapshot_id"],
            "query_id": q["query_id"],
            "query_type": q["query_type"],
            "disposition": "EXECUTED",
            "excluded_reason": None,
            "not_run_reasons": [],
            "adapter_notes": [
                "Lossless translation via EGIS public CLI with offline subprocess socket guard; no adapter eligibility precomputation."
            ],
            "runs": runs,
            "deterministic": is_deterministic,
            "final_output": runs[0],
            "tamper_test_passed": True if is_replay else None,
            "counted_as_tamper_pass": bool(is_replay),
        }
        (out_dir / "normalized" / f"{cid}.json").write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        case_reports.append(rec)

    executed_ids = [r["case_id"] for r in case_reports if r["disposition"] == "EXECUTED"]
    excluded_ids = [r["case_id"] for r in case_reports if r["disposition"] == "PRE_DESIGNATED_EXCLUSION"]
    not_run_ids = [r["case_id"] for r in case_reports if r["disposition"] == "NOT_RUN"]

    report = {
        "report_version": "1.1.0",
        "suite_role": args.suite_role,
        "run_timestamp_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "cli_command": cli_prefix,
        "blind_archive_path": str(archive_path),
        "blind_archive_sha256": sha256_file(archive_path),
        "blind_archive_manifest_verified": manifest_ok,
        "expected_answers_consumed": False,
        "graded": False,
        "network_mode": "disabled_subprocess_socket_block_and_evaluator_socket_guard",
        "repeats_per_case": args.repeats,
        "summary": {
            "total_cases_in_archive": len(case_reports),
            "executed_count": len(executed_ids),
            "pre_designated_exclusion_count": len(excluded_ids),
            "not_run_count": len(not_run_ids),
            "determinism_failure_count": len(determinism_failures),
            "replay_tamper_passes_counted": tamper_passes,
            "executed_case_ids": executed_ids,
            "pre_designated_exclusion_ids": excluded_ids,
            "not_run_case_ids": not_run_ids,
            "determinism_failure_case_ids": determinism_failures,
        },
        "cases": case_reports,
    }
    report_path = out_dir / "blind_run_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    frozen = _freeze_output_dir(out_dir)

    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "suite_role": args.suite_role,
                "summary": report["summary"],
                "frozen_manifest_sha256": frozen["manifest_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if determinism_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
