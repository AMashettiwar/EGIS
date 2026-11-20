#!/usr/bin/env python3
"""Independent developer recheck and reviewer-package builder for GDC TCGA-LUAD v3.

Intentionally imports ZERO modules from ``cancer_evidence``. Verifies frozen snapshot
hashes, independently recomputes the 571 denominator and 40 positive EGFR witnesses
directly from ``raw.json``, ``normalized.json``, and raw ``data.sqlite`` SQL queries,
runs the public CLI offline into a fresh versioned directory, and packages a
self-contained developer-prepared bundle for an external reviewer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

SITECUSTOMIZE_SOURCE = """\
import socket
import urllib.request

def _blocked(*args, **kwargs):
    raise RuntimeError("network disabled during offline independent recheck")

socket.socket = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
urllib.request.urlopen = _blocked
"""


def _blocked_net(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("network disabled during offline independent recheck")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        timeout=90,
        shell=False,
    )
    return {
        "cmd": [*cli_prefix, *args],
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "stdout_json": json.loads(proc.stdout) if proc.stdout.strip() else None,
    }


def _git_head_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT, text=True
        ).strip()
    except Exception as exc:  # pragma: no cover - fallback when git unavailable
        return f"GIT_UNAVAILABLE: {exc}"


def main() -> int:
    orig_socket = socket.socket
    orig_create = socket.create_connection
    orig_addr = socket.getaddrinfo
    orig_urlopen = urllib.request.urlopen
    socket.socket = _blocked_net  # type: ignore[assignment]
    socket.create_connection = _blocked_net  # type: ignore[assignment]
    socket.getaddrinfo = _blocked_net  # type: ignore[assignment]
    urllib.request.urlopen = _blocked_net  # type: ignore[assignment]
    try:
        snap_dir = ROOT / "snapshots" / "real" / "gdc_tcga_luad_v3"
        v2_dir = ROOT / "snapshots" / "real" / "gdc_tcga_luad_v2"
        recheck_dir = ROOT / "artifacts" / "gdc-luad-v3-independent-recheck-v1"
        pkg_dir = ROOT / "artifacts" / "external-reviewer-package-gdc-v3-v1"
        prior_milestone_dir = ROOT / "artifacts" / "gdc-luad-v3-molecular-milestone"

        recheck_dir.mkdir(parents=True, exist_ok=True)
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # 1. Record code commit, source file hashes, and prior frozen manifests
        source_files = sorted(
            list((ROOT / "src" / "cancer_evidence").glob("*.py")) + list((ROOT / "scripts").glob("*.py"))
        )
        source_hashes = {p.relative_to(ROOT).as_posix(): _sha256_file(p) for p in source_files}

        preserved_targets = [
            "snapshots/real/gdc_tcga_luad/manifest.json",
            "snapshots/real/gdc_tcga_luad_v2/manifest.json",
            "snapshots/real/gdc_tcga_luad_v3/manifest.json",
            "snapshots/real/pdc_pdc000153/manifest.json",
            "artifacts/blind-exam-run/FROZEN_MANIFEST.json",
            "artifacts/cor-001-blind-run/FROZEN_MANIFEST.json",
            "artifacts/regression-exam-run/FROZEN_MANIFEST.json",
            "artifacts/gdc-luad-v2-milestone/source_reconciliation_report.json",
            "artifacts/gdc-luad-v3-molecular-milestone/source_reconciliation_report.json",
        ]
        preserved_records: dict[str, Any] = {}
        for rel in preserved_targets:
            fpath = ROOT / rel
            entry: dict[str, Any] = {"file_sha256": _sha256_file(fpath)}
            loaded = json.loads(fpath.read_text(encoding="utf-8"))
            if "manifest_sha256" in loaded:
                entry["internal_manifest_sha256"] = loaded["manifest_sha256"]
            preserved_records[rel] = entry

        # 2. Verify v3 snapshot integrity & provenance hashes
        manifest_bytes = (snap_dir / "manifest.json").read_bytes()
        raw_bytes = (snap_dir / "raw.json").read_bytes()
        norm_bytes = (snap_dir / "normalized.json").read_bytes()
        sqlite_bytes = (snap_dir / "data.sqlite").read_bytes()

        manifest = json.loads(manifest_bytes.decode("utf-8"))
        raw = json.loads(raw_bytes.decode("utf-8"))
        normalized = json.loads(norm_bytes.decode("utf-8"))

        manifest_copy = dict(manifest)
        stored_manifest_sha = manifest_copy.pop("manifest_sha256", None)
        computed_manifest_sha = _sha256_bytes(_canonical_json(manifest_copy).encode("utf-8"))

        raw_sha = _sha256_bytes(raw_bytes)
        norm_sha = _sha256_bytes(norm_bytes)
        sqlite_sha = _sha256_bytes(sqlite_bytes)

        v2_manifest_bytes = (v2_dir / "manifest.json").read_bytes()
        v2_raw_bytes = (v2_dir / "raw.json").read_bytes()
        v2_manifest = json.loads(v2_manifest_bytes.decode("utf-8"))
        base_found = manifest["provenance_metadata"]["base_snapshot_foundation"]

        # Verify page metadata consistency between manifest.requests_and_pagination and raw.raw_capture
        rc = raw["raw_capture"]
        rp = manifest["provenance_metadata"]["requests_and_pagination"]
        page_consistency: dict[str, Any] = {}
        for mkey, rkey in (
            ("cases", "case_pages"),
            ("rnaseq_files", "rnaseq_file_pages"),
            ("wxs_snv_files", "snv_file_pages"),
            ("egfr_ssm_occurrences", "egfr_ssm_pages"),
        ):
            m_pages = rp[mkey]["pages"]
            r_pages = rc[rkey]
            page_consistency[mkey] = {
                "page_count": len(r_pages),
                "pagination_total": rp[mkey]["pagination_total"],
                "all_page_hashes_match_raw_capture": len(m_pages) == len(r_pages)
                and all(mp["response_sha256"] == rp_["response_sha256"] for mp, rp_ in zip(m_pages, r_pages)),
                "all_page_hit_counts_match": len(m_pages) == len(r_pages)
                and all(
                    mp["returned_count"] == len(rp_["response"]["data"]["hits"])
                    for mp, rp_ in zip(m_pages, r_pages)
                ),
            }

        hash_checks = {
            "raw_content_sha256_match": raw_sha == manifest["raw_content_sha256"],
            "normalized_content_sha256_match": norm_sha == manifest["normalized_content_sha256"],
            "sqlite_sha256_match": sqlite_sha == manifest["sqlite_sha256"],
            "manifest_sha256_match": computed_manifest_sha == stored_manifest_sha,
            "ready_marker_match": (snap_dir / "READY").read_text(encoding="utf-8").strip() == manifest["publication"],
            "base_v2_raw_sha256_match": _sha256_bytes(v2_raw_bytes) == base_found["raw_content_sha256"] == v2_manifest["raw_content_sha256"],
            "base_v2_manifest_file_sha256_match": _sha256_bytes(v2_manifest_bytes) == base_found["manifest_sha256"],
            "raw_capture_page_metadata_consistent": all(
                v["all_page_hashes_match_raw_capture"] and v["all_page_hit_counts_match"]
                for v in page_consistency.values()
            ),
        }

        # 3. Independent Method A: direct raw_capture GDC JSON parsing
        case_to_pt_samples: dict[str, set[str]] = defaultdict(set)
        sample_to_case: dict[str, str] = {}
        sample_type_map: dict[str, str | None] = {}
        aliquot_to_sample: dict[str, str] = {}
        sample_to_aliquots: dict[str, set[str]] = defaultdict(set)
        raw_cases: set[str] = set()

        for page in rc["case_pages"]:
            for c in page["response"]["data"]["hits"]:
                cid = c["case_id"]
                raw_cases.add(cid)
                for s in c.get("samples", []) or []:
                    sid = s["sample_id"]
                    st = s.get("sample_type")
                    sample_to_case[sid] = cid
                    sample_type_map[sid] = st
                    if st == "Primary Tumor":
                        case_to_pt_samples[cid].add(sid)
                    for port in s.get("portions", []) or []:
                        for an in port.get("analytes", []) or []:
                            for alq in an.get("aliquots", []) or []:
                                alqid = alq["aliquot_id"]
                                sample_to_aliquots[sid].add(alqid)
                                aliquot_to_sample[alqid] = sid

        primary_tumor_samples = {sid for sid, st in sample_type_map.items() if st == "Primary Tumor"}
        normal_types = {"Blood Derived Normal", "Solid Tissue Normal"}

        wxs_maf_files: dict[str, dict[str, Any]] = {}
        wxs_pt_samples: set[str] = set()
        wxs_pt_cases: set[str] = set()
        wxs_pt_aliquots: set[str] = set()
        pt_sample_to_wxs_files: dict[str, set[str]] = defaultdict(set)
        pt_sample_to_wxs_aliquots: dict[str, set[str]] = defaultdict(set)
        pair_to_files: dict[frozenset[str], list[str]] = defaultdict(list)
        snv_unmatched_aliquots = 0
        snv_unmatched_samples = 0

        for page in rc["snv_file_pages"]:
            for fl in page["response"]["data"]["hits"]:
                fid = fl["file_id"]
                qualifies = (
                    fl.get("data_category") == "Simple Nucleotide Variation"
                    and fl.get("data_type") == "Masked Somatic Mutation"
                    and fl.get("experimental_strategy") == "WXS"
                    and fl.get("data_format") == "MAF"
                    and fl.get("access") == "open"
                    and fl.get("state") == "released"
                )
                if qualifies:
                    wxs_maf_files[fid] = fl
                alq_set: set[str] = set()
                for c in fl.get("cases", []) or []:
                    for s in c.get("samples", []) or []:
                        sid = s["sample_id"]
                        st = s.get("sample_type")
                        if sid not in sample_type_map:
                            snv_unmatched_samples += 1
                        for port in s.get("portions", []) or []:
                            for an in port.get("analytes", []) or []:
                                for alq in an.get("aliquots", []) or []:
                                    aid = alq["aliquot_id"]
                                    if aid not in aliquot_to_sample:
                                        snv_unmatched_aliquots += 1
                                    alq_set.add(aid)
                                    if qualifies and st == "Primary Tumor" and sid in primary_tumor_samples:
                                        wxs_pt_samples.add(sid)
                                        wxs_pt_cases.add(sample_to_case[sid])
                                        wxs_pt_aliquots.add(aid)
                                        pt_sample_to_wxs_files[sid].add(fid)
                                        pt_sample_to_wxs_aliquots[sid].add(aid)
                if qualifies and alq_set:
                    pair_to_files[frozenset(alq_set)].append(fid)

        target_hgvsp = {"p.L858R", "p.E746_A750del"}
        raw_occ_all: list[dict[str, Any]] = []
        raw_unmatched_occ = 0
        raw_multiply_matched_occ = 0
        raw_multi_file_occ = 0
        raw_any_egfr_samples: set[str] = set()
        sample_egfr_variants: dict[str, list[tuple[str, str | None, str]]] = defaultdict(list)

        raw_pos_samples: set[str] = set()
        raw_pos_cases: set[str] = set()
        raw_pos_occs: set[str] = set()
        raw_pos_aliquots: set[str] = set()
        raw_pos_files: set[str] = set()
        raw_pos_breakdown: Counter[str] = Counter()
        semantic_checks = {
            "all_93_ncbi_build_GRCh38": True,
            "all_93_chromosome_chr7": True,
            "all_93_gene_ENSG00000146648_EGFR": True,
            "all_93_canonical_ENST00000275493_present": True,
            "all_93_variant_process_masked": True,
            "all_93_mutation_status_Somatic": True,
            "all_93_positive_only_no_wt_reference_calls": True,
        }

        for page in rc["egfr_ssm_pages"]:
            for occ in page["response"]["data"]["hits"]:
                occ_id = occ["ssm_occurrence_id"]
                ssm = occ["ssm"]
                obs = (occ.get("case") or {}).get("observation") or []
                t_alqs = {(o.get("sample") or {}).get("tumor_sample_uuid") for o in obs if (o.get("sample") or {}).get("tumor_sample_uuid")}
                smps = {aliquot_to_sample[a] for a in t_alqs if a in aliquot_to_sample}
                if len(t_alqs) != 1 or len(smps) == 0:
                    raw_unmatched_occ += 1
                    continue
                if len(smps) > 1 or len(t_alqs) > 1:
                    raw_multiply_matched_occ += 1
                    continue

                sid = next(iter(smps))
                t_alq = next(iter(t_alqs))
                pairs = {
                    frozenset(
                        x
                        for x in (
                            (o.get("sample") or {}).get("tumor_sample_uuid"),
                            (o.get("sample") or {}).get("matched_norm_sample_uuid"),
                        )
                        if x
                    )
                    for o in obs
                }
                matched_files = sorted({fid for pk in pairs for fid in pair_to_files.get(pk, [])})
                if len(matched_files) != 1:
                    raw_multi_file_occ += 1

                callers = {(o.get("variant_calling") or {}).get("variant_caller") for o in obs}
                vprocs = {(o.get("variant_calling") or {}).get("variant_process") for o in obs}
                mstats = {o.get("mutation_status") for o in obs}
                canon = [
                    cons.get("transcript")
                    for cons in ssm.get("consequence", []) or []
                    if (cons.get("transcript") or {}).get("gene", {}).get("symbol") == "EGFR"
                    and (cons.get("transcript") or {}).get("is_canonical") is True
                ]
                if len(canon) != 1 or canon[0].get("transcript_id") != "ENST00000275493":
                    semantic_checks["all_93_canonical_ENST00000275493_present"] = False
                if ssm.get("ncbi_build") != "GRCh38":
                    semantic_checks["all_93_ncbi_build_GRCh38"] = False
                if ssm.get("chromosome") != "chr7":
                    semantic_checks["all_93_chromosome_chr7"] = False
                if canon and canon[0].get("gene", {}).get("gene_id") != "ENSG00000146648":
                    semantic_checks["all_93_gene_ENSG00000146648_EGFR"] = False
                if vprocs != {"masked"}:
                    semantic_checks["all_93_variant_process_masked"] = False
                if mstats != {"Somatic"}:
                    semantic_checks["all_93_mutation_status_Somatic"] = False

                ann = (canon[0].get("annotation") or {}) if canon else {}
                hgvsp = ann.get("hgvsp_short")
                raw_any_egfr_samples.add(sid)
                sample_egfr_variants[sid].append((occ_id, hgvsp, t_alq))
                raw_occ_all.append(occ)

                if sid in wxs_pt_samples and hgvsp in target_hgvsp and ssm.get("ncbi_build") == "GRCh38":
                    raw_pos_samples.add(sid)
                    raw_pos_cases.add(sample_to_case[sid])
                    raw_pos_occs.add(occ_id)
                    raw_pos_aliquots.add(t_alq)
                    raw_pos_files.update(matched_files)
                    raw_pos_breakdown[hgvsp] += 1

        # 4. Independent Method B: direct normalized.json parsing
        norm_ents = {e["entity_id"]: e for e in normalized["entities"]}
        norm_rels = normalized["relationships"]
        pt_eids = {eid for eid, e in norm_ents.items() if e["kind"] == "sample" and e["attributes"].get("sample_type") == "Primary Tumor"}
        sample_to_alq_eids: dict[str, set[str]] = defaultdict(set)
        alq_to_file_eids: dict[str, set[str]] = defaultdict(set)
        alq_to_assay_eids: dict[str, set[str]] = defaultdict(set)
        assay_to_file_eids: dict[str, set[str]] = defaultdict(set)
        for rel in norm_rels:
            if rel["predicate"] == "HAS_ALIQUOT":
                sample_to_alq_eids[rel["subject_id"]].add(rel["object_id"])
            elif rel["predicate"] == "HAS_FILE" and norm_ents[rel["subject_id"]]["kind"] == "aliquot":
                alq_to_file_eids[rel["subject_id"]].add(rel["object_id"])
            elif rel["predicate"] == "HAS_ASSAY" and norm_ents[rel["subject_id"]]["kind"] == "aliquot":
                alq_to_assay_eids[rel["subject_id"]].add(rel["object_id"])
            elif rel["predicate"] == "HAS_FILE" and norm_ents[rel["subject_id"]]["kind"] == "assay":
                assay_to_file_eids[rel["subject_id"]].add(rel["object_id"])

        norm_den_eids = {
            sid
            for sid in pt_eids
            if any(
                norm_ents[fid]["attributes"].get("data_category") == "Simple Nucleotide Variation"
                and norm_ents[fid]["attributes"].get("data_type") == "Masked Somatic Mutation"
                and norm_ents[fid]["attributes"].get("experimental_strategy") == "WXS"
                and norm_ents[fid]["attributes"].get("data_format") == "MAF"
                and norm_ents[fid]["attributes"].get("access") == "open"
                and norm_ents[fid]["attributes"].get("state") == "released"
                for aid in sample_to_alq_eids[sid]
                for fid in alq_to_file_eids[aid]
            )
        }
        norm_pos_samples: set[str] = set()
        norm_pos_occs: set[str] = set()
        norm_pos_alqs: set[str] = set()
        norm_pos_files: set[str] = set()
        for sid in norm_den_eids:
            for aid in sample_to_alq_eids[sid]:
                for asy in alq_to_assay_eids[aid]:
                    attrs = norm_ents[asy]["attributes"]
                    if (
                        attrs.get("gene_symbol") == "EGFR"
                        and attrs.get("ncbi_build") == "GRCh38"
                        and attrs.get("canonical_transcript_id") == "ENST00000275493"
                        and attrs.get("hgvsp_short") in target_hgvsp
                        and attrs.get("call_status") == "POSITIVE"
                    ):
                        norm_pos_samples.add(norm_ents[sid]["source_id"])
                        norm_pos_occs.add(norm_ents[asy]["source_id"])
                        norm_pos_alqs.add(norm_ents[aid]["source_id"])
                        for fid in assay_to_file_eids[asy]:
                            norm_pos_files.add(norm_ents[fid]["source_id"])

        # 5. Independent Method C: raw data.sqlite SQL query (no cancer_evidence imports)
        conn = sqlite3.connect(snap_dir / "data.sqlite")
        try:
            sql_den = {
                r[0]
                for r in conn.execute(
                    """
                    SELECT DISTINCT s.source_id
                    FROM entities c
                    JOIN relationships hs ON hs.subject_id = c.entity_id AND hs.predicate = 'HAS_SAMPLE'
                    JOIN entities s ON s.entity_id = hs.object_id AND s.kind = 'sample'
                    JOIN relationships ha ON ha.subject_id = s.entity_id AND ha.predicate = 'HAS_ALIQUOT'
                    JOIN entities alq ON alq.entity_id = ha.object_id AND alq.kind = 'aliquot'
                    JOIN relationships hf ON hf.subject_id = alq.entity_id AND hf.predicate = 'HAS_FILE'
                    JOIN entities f ON f.entity_id = hf.object_id AND f.kind = 'file'
                    WHERE c.kind = 'case'
                      AND json_extract(s.attributes_json, '$.sample_type') = 'Primary Tumor'
                      AND json_extract(f.attributes_json, '$.data_category') = 'Simple Nucleotide Variation'
                      AND json_extract(f.attributes_json, '$.data_type') = 'Masked Somatic Mutation'
                      AND json_extract(f.attributes_json, '$.experimental_strategy') = 'WXS'
                      AND json_extract(f.attributes_json, '$.data_format') = 'MAF'
                      AND json_extract(f.attributes_json, '$.access') = 'open'
                      AND json_extract(f.attributes_json, '$.state') = 'released'
                    """
                ).fetchall()
            }
            sql_pos_rows = conn.execute(
                """
                SELECT DISTINCT s.source_id, a.source_id, alq.source_id, f.source_id
                FROM entities s
                JOIN relationships ha ON ha.subject_id = s.entity_id AND ha.predicate = 'HAS_ALIQUOT'
                JOIN entities alq ON alq.entity_id = ha.object_id AND alq.kind = 'aliquot'
                JOIN relationships hasy ON hasy.subject_id = alq.entity_id AND hasy.predicate = 'HAS_ASSAY'
                JOIN entities a ON a.entity_id = hasy.object_id AND a.kind = 'assay'
                JOIN relationships hf ON hf.subject_id = a.entity_id AND hf.predicate = 'HAS_FILE'
                JOIN entities f ON f.entity_id = hf.object_id AND f.kind = 'file'
                WHERE s.kind = 'sample'
                  AND json_extract(s.attributes_json, '$.sample_type') = 'Primary Tumor'
                  AND json_extract(a.attributes_json, '$.gene_symbol') = 'EGFR'
                  AND json_extract(a.attributes_json, '$.ncbi_build') = 'GRCh38'
                  AND json_extract(a.attributes_json, '$.canonical_transcript_id') = 'ENST00000275493'
                  AND json_extract(a.attributes_json, '$.hgvsp_short') IN ('p.L858R', 'p.E746_A750del')
                  AND json_extract(a.attributes_json, '$.call_status') = 'POSITIVE'
                """
            ).fetchall()
            sql_pos_samples = {r[0] for r in sql_pos_rows if r[0] in sql_den}
            sql_pos_occs = {r[1] for r in sql_pos_rows if r[0] in sql_den}
            sql_pos_alqs = {r[2] for r in sql_pos_rows if r[0] in sql_den}
            sql_pos_files = {r[3] for r in sql_pos_rows if r[0] in sql_den}
            sql_non_pos_assays = conn.execute(
                "SELECT COUNT(*) FROM entities WHERE kind = 'assay' AND COALESCE(json_extract(attributes_json, '$.call_status'), '') != 'POSITIVE'"
            ).fetchone()[0]
            if sql_non_pos_assays != 0:
                semantic_checks["all_93_positive_only_no_wt_reference_calls"] = False
        finally:
            conn.close()

        # Multiplicity breakdown
        cases_by_pt_count = dict(sorted(Counter(len(case_to_pt_samples[c]) for c in raw_cases).items()))
        wxs_pt_samples_by_file_count = dict(sorted(Counter(len(v) for v in pt_sample_to_wxs_files.values()).items()))
        wxs_cases_by_pt_sample_count = dict(
            sorted(Counter(len(case_to_pt_samples[c] & wxs_pt_samples) for c in raw_cases).items())
        )
        egfr_samples_by_occ_count = dict(sorted(Counter(len(v) for v in sample_egfr_variants.values()).items()))
        multi_maf_egfr_samples = {
            sid: {
                "wxs_maf_file_count": len(pt_sample_to_wxs_files[sid]),
                "wxs_tumor_aliquot_count": len(pt_sample_to_wxs_aliquots[sid]),
                "egfr_occurrences": [
                    {"ssm_occurrence_id": oid, "hgvsp_short": hgv, "tumor_aliquot_id": alq}
                    for oid, hgv, alq in sample_egfr_variants[sid]
                ],
            }
            for sid in sorted(pt_sample_to_wxs_files)
            if len(pt_sample_to_wxs_files[sid]) > 1 and sid in sample_egfr_variants
        }
        compound_target_samples = {
            sid: [
                {"ssm_occurrence_id": oid, "hgvsp_short": hgv}
                for oid, hgv, _ in sample_egfr_variants[sid]
            ]
            for sid in sorted(raw_pos_samples)
            if len(sample_egfr_variants[sid]) > 1
        }
        multi_wxs_case_count = sum(1 for c in raw_cases if len(case_to_pt_samples[c] & wxs_pt_samples) > 1)

        # 6. Run public CLI offline in fresh recheck_dir
        snapshot_id = manifest["snapshot_id"]
        cohort_id = manifest["cohort_id"]

        supported_query = {
            "operation": "cohort_count",
            "snapshot_id": snapshot_id,
            "cohort_id": cohort_id,
            "entity_level": "sample",
            "denominator_basis": "tcga_luad_gdc_r46_wxs_masked_ssm_primary_tumor_samples",
            "predicate": {
                "source": "gdc",
                "project_id": "TCGA-LUAD",
                "sample_type": "Primary Tumor",
                "data_category": "Simple Nucleotide Variation",
                "data_type": "Masked Somatic Mutation",
                "experimental_strategy": "WXS",
                "data_format": "MAF",
                "access": "open",
                "state": "released",
                "gene_symbol": "EGFR",
                "ncbi_build": "GRCh38",
                "canonical_transcript_id": "ENST00000275493",
                "hgvsp_short_in": ["p.L858R", "p.E746_A750del"],
                "call_status": "POSITIVE",
            },
        }
        abstaining_negative_query = {
            "operation": "cohort_count",
            "snapshot_id": snapshot_id,
            "cohort_id": cohort_id,
            "entity_level": "sample",
            "denominator_basis": "tcga_luad_gdc_r46_wxs_masked_ssm_primary_tumor_samples",
            "predicate": {
                "source": "gdc",
                "project_id": "TCGA-LUAD",
                "sample_type": "Primary Tumor",
                "data_category": "Simple Nucleotide Variation",
                "data_type": "Masked Somatic Mutation",
                "experimental_strategy": "WXS",
                "data_format": "MAF",
                "access": "open",
                "state": "released",
                "gene_symbol": "EGFR",
                "ncbi_build": "GRCh38",
                "canonical_transcript_id": "ENST00000275493",
                "hgvsp_short_in": ["p.L858R", "p.E746_A750del"],
                "call_status": "NEGATIVE",
                "require_explicit_negative_coverage": True,
            },
        }
        abstaining_unacquired_gene_query = {
            "operation": "cohort_count",
            "snapshot_id": snapshot_id,
            "cohort_id": cohort_id,
            "entity_level": "sample",
            "denominator_basis": "tcga_luad_gdc_r46_wxs_masked_ssm_primary_tumor_samples",
            "predicate": {
                "source": "gdc",
                "project_id": "TCGA-LUAD",
                "sample_type": "Primary Tumor",
                "data_category": "Simple Nucleotide Variation",
                "data_type": "Masked Somatic Mutation",
                "experimental_strategy": "WXS",
                "data_format": "MAF",
                "access": "open",
                "state": "released",
                "gene_symbol": "KRAS",
                "hgvsp_short": "p.G12C",
                "call_status": "POSITIVE",
            },
        }

        queries = [
            ("supported", supported_query),
            ("abstaining_negative", abstaining_negative_query),
            ("abstaining_unacquired_gene", abstaining_unacquired_gene_query),
        ]

        cli_prefix = _find_evidence_cli(ROOT)
        cli_outputs: dict[str, Any] = {}
        with tempfile.TemporaryDirectory(prefix="egis_recheck_v1_") as tmp:
            scratch = Path(tmp)
            for tag, q_obj in queries:
                q_path = recheck_dir / f"{tag}_query.json"
                res_path = recheck_dir / f"{tag}_result.json"
                cert_path = recheck_dir / f"{tag}_certificate.json"
                rep_path = recheck_dir / f"{tag}_replay.json"
                q_path.write_text(json.dumps(q_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")

                ans = _run_offline_cli(
                    cli_prefix,
                    ["answer", "--snapshot", str(snap_dir), "--query", str(q_path), "--certificate", str(cert_path)],
                    scratch,
                )
                if ans["returncode"] != 0 or not isinstance(ans["stdout_json"], dict):
                    raise RuntimeError(f"CLI answer failed for {tag}: {ans}")
                rep = _run_offline_cli(
                    cli_prefix,
                    ["verify", "--snapshot", str(snap_dir), "--certificate", str(cert_path)],
                    scratch,
                )
                res_obj = ans["stdout_json"]
                rep_obj = rep["stdout_json"] or {}
                res_path.write_text(json.dumps(res_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                rep_path.write_text(json.dumps(rep_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                cli_outputs[tag] = {"result": res_obj, "replay": rep_obj, "verify_rc": rep["returncode"]}

        # Compare all 3 independent methods, new CLI run, and prior milestone artifacts
        expected_samples = sorted(raw_pos_samples)
        expected_occs = sorted(raw_pos_occs)
        expected_alqs = sorted(raw_pos_aliquots)
        expected_files = sorted(raw_pos_files)
        expected_refs = sorted(raw_pos_samples | raw_pos_occs | raw_pos_aliquots | raw_pos_files)

        sup_res = cli_outputs["supported"]["result"]
        sup_inner = sup_res.get("result") or {}
        sup_rep = cli_outputs["supported"]["replay"]

        prior_sup_res = json.loads((prior_milestone_dir / "supported_result.json").read_text(encoding="utf-8"))

        comparisons = {
            "raw_vs_norm_denominator_match": len(wxs_pt_samples) == len(norm_den_eids) == 571,
            "raw_vs_sqlite_denominator_match": wxs_pt_samples == sql_den,
            "raw_vs_norm_positive_sets_match": (
                raw_pos_samples == norm_pos_samples
                and raw_pos_occs == norm_pos_occs
                and raw_pos_aliquots == norm_pos_alqs
                and raw_pos_files == norm_pos_files
            ),
            "raw_vs_sqlite_positive_sets_match": (
                raw_pos_samples == sql_pos_samples
                and raw_pos_occs == sql_pos_occs
                and raw_pos_aliquots == sql_pos_alqs
                and raw_pos_files == sql_pos_files
            ),
            "cli_supported_status_match": sup_res.get("status") == "SUPPORTED" and sup_res.get("reason_code") == "NONE",
            "cli_supported_counts_match": (
                sup_inner.get("numerator_count") == len(expected_samples) == 40
                and sup_inner.get("denominator_count") == len(wxs_pt_samples) == 571
            ),
            "cli_witness_set_match": sorted(sup_res.get("witness") or []) == expected_samples,
            "cli_variant_ids_match": sorted(sup_inner.get("supporting_variant_record_ids") or []) == expected_occs,
            "cli_aliquot_ids_match": sorted(sup_inner.get("supporting_aliquot_ids") or []) == expected_alqs,
            "cli_file_ids_match": sorted(sup_inner.get("supporting_file_ids") or []) == expected_files,
            "cli_evidence_refs_match": sorted(sup_res.get("evidence_refs") or []) == expected_refs,
            "cli_supported_replay_valid": cli_outputs["supported"]["verify_rc"] == 0 and sup_rep.get("valid") is True,
            "matches_prior_milestone_result_exact": sup_res == prior_sup_res,
            "cli_abstaining_negative_ok": (
                cli_outputs["abstaining_negative"]["result"].get("status") == "UNKNOWN"
                and cli_outputs["abstaining_negative"]["result"].get("reason_code") == "INCOMPLETE_SNAPSHOT"
                and cli_outputs["abstaining_negative"]["result"].get("witness") == []
                and cli_outputs["abstaining_negative"]["verify_rc"] == 0
                and cli_outputs["abstaining_negative"]["replay"].get("valid") is True
            ),
            "cli_abstaining_unacquired_gene_ok": (
                cli_outputs["abstaining_unacquired_gene"]["result"].get("status") == "UNKNOWN"
                and cli_outputs["abstaining_unacquired_gene"]["result"].get("reason_code") == "INCOMPLETE_SNAPSHOT"
                and cli_outputs["abstaining_unacquired_gene"]["result"].get("witness") == []
                and cli_outputs["abstaining_unacquired_gene"]["verify_rc"] == 0
                and cli_outputs["abstaining_unacquired_gene"]["replay"].get("valid") is True
            ),
        }

        report = {
            "report_version": "1.0.0",
            "report_role": "developer_independent_recheck_and_package_builder",
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "network_mode": "disabled_in_process_and_subprocess",
            "git_head_commit": _git_head_commit(ROOT),
            "source_file_sha256": source_hashes,
            "preserved_prior_artifacts": preserved_records,
            "snapshot": {
                "path": "snapshots/real/gdc_tcga_luad_v3",
                "snapshot_id": snapshot_id,
                "cohort_id": cohort_id,
                "retrieved_at_utc": manifest["retrieved_at_utc"],
                "upstream_data_release": manifest["provenance_metadata"]["upstream_status"]["data_release"],
                "upstream_tag": manifest["provenance_metadata"]["upstream_status"]["tag"],
                "upstream_commit": manifest["provenance_metadata"]["upstream_status"]["commit"],
                "hashes": {
                    "raw_content_sha256": raw_sha,
                    "normalized_content_sha256": norm_sha,
                    "sqlite_sha256": sqlite_sha,
                    "manifest_sha256_canonical": computed_manifest_sha,
                    "manifest_file_sha256": _sha256_bytes(manifest_bytes),
                },
                "hash_verification": hash_checks,
                "page_consistency": page_consistency,
                "provenance_notes": [
                    "GDC /status response_sha256 and per-page response_sha256 digests were computed at fetch time over live HTTP wire bytes; raw.json stores re-serialized UTF-8 JSON (indent=2, sort_keys=True), so wire whitespace bytes are not stored separately.",
                    "In v3 manifest.provenance_metadata.base_snapshot_foundation.manifest_sha256, b64e2117e63534aa943f390c1aceb29dc1a14bcee279771e5effc1f7cfba8e09 is the raw disk-file SHA-256 of v2/manifest.json, whereas v2/manifest.json['manifest_sha256'] (82209334...) is the canonical JSON digest computed before inserting the manifest_sha256 key.",
                ],
            },
            "source_semantics_verification": semantic_checks,
            "independent_linkage_and_multiplicity_audit": {
                "cases_total": len(raw_cases),
                "cases_by_primary_tumor_sample_count": cases_by_pt_count,
                "primary_tumor_samples_total": len(primary_tumor_samples),
                "wxs_masked_ssm_maf_files_total": len(wxs_maf_files),
                "wxs_masked_ssm_primary_tumor_samples": len(wxs_pt_samples),
                "wxs_masked_ssm_primary_tumor_cases": len(wxs_pt_cases),
                "cases_with_multiple_wxs_primary_tumor_samples": multi_wxs_case_count,
                "wxs_cases_by_pt_sample_count": wxs_cases_by_pt_sample_count,
                "wxs_pt_samples_by_maf_file_count": wxs_pt_samples_by_file_count,
                "snv_unmatched_sample_links": snv_unmatched_samples,
                "snv_unmatched_aliquot_links": snv_unmatched_aliquots,
                "egfr_ssm_occurrences_total": len(raw_occ_all),
                "egfr_ssm_occurrences_unmatched": raw_unmatched_occ,
                "egfr_ssm_occurrences_multiply_matched": raw_multiply_matched_occ,
                "egfr_ssm_occurrences_non_unique_maf_file": raw_multi_file_occ,
                "egfr_any_variant_primary_tumor_samples": len(raw_any_egfr_samples),
                "egfr_samples_by_occurrence_count": egfr_samples_by_occ_count,
                "multi_maf_egfr_samples": multi_maf_egfr_samples,
                "compound_target_egfr_samples": compound_target_samples,
                "target_canonical_egfr_positive_samples": len(expected_samples),
                "target_canonical_egfr_positive_cases": len(raw_pos_cases),
                "target_variant_breakdown": dict(sorted(raw_pos_breakdown.items())),
                "target_variant_record_ids_count": len(expected_occs),
                "target_aliquot_ids_count": len(expected_alqs),
                "target_wxs_maf_file_ids_count": len(expected_files),
                "target_lineage_evidence_refs_count": len(expected_refs),
            },
            "cross_verification_checks": comparisons,
            "all_checks_passed": (
                all(hash_checks.values()) and all(semantic_checks.values()) and all(comparisons.values())
            ),
        }

        report_path = recheck_dir / "independent_recheck_report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        # 7. Populate Developer-Prepared External Reviewer Package
        for item in recheck_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, pkg_dir / item.name)
        shutil.copy2(Path(__file__), pkg_dir / "independent_recheck_gdc_v3.py")

        checklist_text = f"""# Developer-Prepared External Reviewer Package: GDC TCGA-LUAD v3

> **Important Notice**: This package was **prepared by the EGIS developer** to support an independent third-party audit. EGIS's own self-checks (`independent_recheck_gdc_v3.py`, `reconcile_and_verify_gdc_v3.py`, and internal regression suites) are **not** independent external validation.

## 1. Exact Scope & Non-Claims

- **Scoped claim**: In frozen snapshot `snapshots/real/gdc_tcga_luad_v3` (`{snapshot_id}`, GDC `{manifest['provenance_metadata']['upstream_status']['data_release']}`), **`40` of `571`** `Primary Tumor` samples linked to released open-access WXS `Masked Somatic Mutation` (`MAF`) files have an observed somatic `EGFR` `p.L858R` or `p.E746_A750del` call on `GRCh38` canonical transcript `ENST00000275493` (`7.005253940455342%`: `22` `p.L858R` + `18` `p.E746_A750del`).
- **Does NOT establish**: TCGA-LUAD cohort-wide prevalence (another `504` of `1,075` `Primary Tumor` samples lack open WXS MAF files in this subset; `571` WXS-linked samples span `559` distinct cases with `12` cases contributing `2` WXS-linked `Primary Tumor` samples), EGFR wild-type / negative status (unmutated locus depth is absent and germline calls are masked), clinical validity, or production readiness.

## 2. Cryptographic & Provenance Targets

- **Snapshot directory**: `snapshots/real/gdc_tcga_luad_v3`
- **`raw_content_sha256`**: `{raw_sha}`
- **`normalized_content_sha256`**: `{norm_sha}`
- **`sqlite_sha256`**: `{sqlite_sha}`
- **`manifest_sha256` (canonical)**: `{computed_manifest_sha}`
- **Public key (`Ed25519` demo keypair)**: `keys/public_key.pem` (SHA-256: `{_sha256_file(ROOT / 'keys' / 'public_key.pem')}`)
- **Git `HEAD` at package creation**: `{report['git_head_commit']}`

## 3. External Reviewer Verification Checklist

1. **Integrity & custody**:
   - Run `sha256sum -c SHA256SUMS.txt` inside `artifacts/external-reviewer-package-gdc-v3-v1/`.
   - Independently re-hash `snapshots/real/gdc_tcga_luad_v3/{{raw.json,normalized.json,data.sqlite,manifest.json}}` and verify canonical `manifest_sha256`.
   - Note that GDC HTTP wire response digests are recorded at acquisition time; `raw.json` preserves the parsed response payloads (`8d5ccf3b...`).
2. **Independent source acquisition check (recommended outside EGIS code)**:
   - Independently query `https://api.gdc.cancer.gov/status`, `https://api.gdc.cancer.gov/files` (TCGA-LUAD open SNV), and `https://api.gdc.cancer.gov/ssm_occurrences` (TCGA-LUAD EGFR), and inspect an open WXS MAF header to verify live GDC schema semantics (`GRCh38`, `ENST00000275493`, positive-only masked calls, `tumor_sample_uuid == aliquot_id`).
3. **Denominator & multiplicity check**:
   - Verify independently from `raw.json` (and without running `cancer_evidence`) that `571` distinct `Primary Tumor` samples (across `559` cases, `612` WXS MAF files; `21` samples have `>1` WXS MAF file) form `tcga_luad_gdc_r46_wxs_masked_ssm_primary_tumor_samples`.
4. **Witness & lineage check**:
   - Verify independently that `93` `EGFR` `/ssm_occurrences` map `93/93` 1-to-1 to tumor aliquot, sample, case, and WXS MAF file (`0` unmatched, `0` multiply matched), and that filtering to `GRCh38` + `ENST00000275493` + `hgvsp_short in {{'p.L858R', 'p.E746_A750del'}}` yields `40` samples, `40` `ssm_occurrence_id`s, `40` tumor aliquots, `40` WXS MAF files (`160` lineage UUIDs). Note `74bcf2d5-fd42-423e-bd96-b2de1b0cf778` has `2` WXS MAF aliquots with `p.L858R` on `92da1bb5-2e83-4f2f-88dd-afc0cb7d91d1`, and `4` `p.L858R` samples carry a second non-target `EGFR` variant.
5. **Abstention & independent replay**:
   - Inspect `abstaining_negative_query.json` (`UNKNOWN / INCOMPLETE_SNAPSHOT`) and `abstaining_unacquired_gene_query.json` (`KRAS` -> `UNKNOWN / INCOMPLETE_SNAPSHOT`).
   - Independently re-verify the Ed25519 signature on `supported_certificate.json` using `keys/public_key.pem`, read `src/cancer_evidence/replay.py` to audit independence from `engine.py`, and execute offline:
     ```bash
     EGIS_OFFLINE=1 .venv/bin/evidence verify \\
       --snapshot snapshots/real/gdc_tcga_luad_v3 \\
       --certificate artifacts/external-reviewer-package-gdc-v3-v1/supported_certificate.json
     ```
6. **Fresh independent holdout evaluation**:
   - Execute an external third-party evaluation against an untouched holdout suite (not the `48`-case development regression suite) and replace the `keys/` demo keypair with external custody before any deployment beyond internal research.
"""
        (pkg_dir / "REVIEWER_CHECKLIST.md").write_text(checklist_text, encoding="utf-8")

        pkg_files = sorted(
            p for p in pkg_dir.iterdir() if p.is_file() and p.name not in {"PACKAGE_MANIFEST.json", "SHA256SUMS.txt"}
        )
        pkg_manifest = {
            "package_version": "1.0.0",
            "package_role": "developer_prepared_external_reviewer_package",
            "independent_external_audit": False,
            "generated_at_utc": report["generated_at_utc"],
            "git_head_commit": report["git_head_commit"],
            "snapshot_dir": "snapshots/real/gdc_tcga_luad_v3",
            "snapshot_hashes": {
                f"snapshots/real/gdc_tcga_luad_v3/{name}": _sha256_file(snap_dir / name)
                for name in ("manifest.json", "raw.json", "normalized.json", "data.sqlite", "READY")
            },
            "public_key": {
                "path": "keys/public_key.pem",
                "sha256": _sha256_file(ROOT / "keys" / "public_key.pem"),
            },
            "package_files_sha256": {p.name: _sha256_file(p) for p in pkg_files},
        }
        (pkg_dir / "PACKAGE_MANIFEST.json").write_text(
            json.dumps(pkg_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        all_sum_targets = sorted(
            p for p in pkg_dir.iterdir() if p.is_file() and p.name != "SHA256SUMS.txt"
        )
        sums_lines = [f"{_sha256_file(p)}  {p.name}" for p in all_sum_targets]
        (pkg_dir / "SHA256SUMS.txt").write_text("\n".join(sums_lines) + "\n", encoding="utf-8")

        print(
            json.dumps(
                {
                    "recheck_report_path": str(report_path),
                    "reviewer_package_dir": str(pkg_dir),
                    "package_manifest_sha256": _sha256_file(pkg_dir / "PACKAGE_MANIFEST.json"),
                    "sha256sums_file_sha256": _sha256_file(pkg_dir / "SHA256SUMS.txt"),
                    "git_head_commit": report["git_head_commit"],
                    "hash_verification": hash_checks,
                    "source_semantics_verification": semantic_checks,
                    "cross_verification_checks": comparisons,
                    "all_checks_passed": report["all_checks_passed"],
                    "linkage_summary": {
                        "cases": len(raw_cases),
                        "wxs_pt_samples": len(wxs_pt_samples),
                        "wxs_pt_cases": len(wxs_pt_cases),
                        "target_samples": len(expected_samples),
                        "target_variant_breakdown": dict(sorted(raw_pos_breakdown.items())),
                        "multi_maf_egfr_samples_count": len(multi_maf_egfr_samples),
                        "compound_target_egfr_samples_count": len(compound_target_samples),
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if report["all_checks_passed"] else 2
    finally:
        socket.socket = orig_socket  # type: ignore[assignment]
        socket.create_connection = orig_create  # type: ignore[assignment]
        socket.getaddrinfo = orig_addr  # type: ignore[assignment]
        urllib.request.urlopen = orig_urlopen  # type: ignore[assignment]


if __name__ == "__main__":
    raise SystemExit(main())
