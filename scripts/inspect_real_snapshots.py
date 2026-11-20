#!/usr/bin/env python3
"""Offline real-data readiness verifier for frozen GDC and PDC snapshots."""

from __future__ import annotations

import json
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.engine import evaluate
from cancer_evidence.replay import replay_certificate
from cancer_evidence.snapshot import read_snapshot


def _blocked_socket(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("network disabled during offline real-snapshot audit")


def _summarize_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    manifest, connection = read_snapshot(snapshot_dir)
    try:
        kind_counts = {
            row[0]: row[1]
            for row in connection.execute("SELECT kind, COUNT(*) FROM entities GROUP BY kind ORDER BY kind").fetchall()
        }
        rel_count = connection.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        ev_count = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        sample_attrs = [
            json.loads(r[0])
            for r in connection.execute("SELECT attributes_json FROM entities LIMIT 25").fetchall()
        ]
        captured_fields = sorted({k for row in sample_attrs for k in row.keys()})
    finally:
        connection.close()

    return {
        "snapshot_dir": str(snapshot_dir.relative_to(ROOT)),
        "snapshot_id": manifest["snapshot_id"],
        "source": manifest["source"],
        "retrieved_at_utc": manifest["retrieved_at_utc"],
        "complete": manifest["complete"],
        "publication": manifest["publication"],
        "raw_content_sha256": manifest["raw_content_sha256"],
        "normalized_content_sha256": manifest["normalized_content_sha256"],
        "sqlite_sha256": manifest["sqlite_sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
        "coverage": manifest["coverage"],
        "entity_counts_by_kind": kind_counts,
        "relationship_count": rel_count,
        "evidence_row_count": ev_count,
        "captured_case_attributes": captured_fields,
        "missing_biospecimen_levels": [
            lvl for lvl in ("patient", "sample", "aliquot", "assay", "file") if kind_counts.get(lvl, 0) == 0
        ],
    }


def main() -> int:
    orig_socket = socket.socket
    socket.socket = _blocked_socket  # type: ignore[assignment]
    try:
        gdc_dir = ROOT / "snapshots" / "real" / "gdc_tcga_luad"
        gdc_v2_dir = ROOT / "snapshots" / "real" / "gdc_tcga_luad_v2"
        gdc_v3_dir = ROOT / "snapshots" / "real" / "gdc_tcga_luad_v3"
        pdc_dir = ROOT / "snapshots" / "real" / "pdc_pdc000153"

        gdc_summary = _summarize_snapshot(gdc_dir)
        gdc_v2_summary = _summarize_snapshot(gdc_v2_dir)
        gdc_v3_summary = _summarize_snapshot(gdc_v3_dir)
        pdc_summary = _summarize_snapshot(pdc_dir)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            gdc_query = {"operation": "count_entities", "selector": {"source": "gdc", "project_id": "TCGA-LUAD"}}
            gdc_cert = tmp_dir / "gdc_count.json"
            gdc_ans = evaluate(gdc_dir, gdc_query)
            issue_certificate(gdc_dir, gdc_query, gdc_cert)
            gdc_replay = replay_certificate(gdc_dir, gdc_cert)

            v2_manifest = json.loads((gdc_v2_dir / "manifest.json").read_text(encoding="utf-8"))
            gdc_v2_sup_query = {
                "operation": "cohort_count",
                "snapshot_id": v2_manifest["snapshot_id"],
                "cohort_id": v2_manifest["cohort_id"],
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
            gdc_v2_sup_cert = tmp_dir / "gdc_v2_supported.json"
            gdc_v2_sup_ans = evaluate(gdc_v2_dir, gdc_v2_sup_query)
            issue_certificate(gdc_v2_dir, gdc_v2_sup_query, gdc_v2_sup_cert)
            gdc_v2_sup_replay = replay_certificate(gdc_v2_dir, gdc_v2_sup_cert)

            gdc_v2_abs_query = {
                "operation": "cohort_count",
                "snapshot_id": v2_manifest["snapshot_id"],
                "cohort_id": v2_manifest["cohort_id"],
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
            gdc_v2_abs_cert = tmp_dir / "gdc_v2_abstaining.json"
            gdc_v2_abs_ans = evaluate(gdc_v2_dir, gdc_v2_abs_query)
            issue_certificate(gdc_v2_dir, gdc_v2_abs_query, gdc_v2_abs_cert)
            gdc_v2_abs_replay = replay_certificate(gdc_v2_dir, gdc_v2_abs_cert)

            pdc_query = {
                "operation": "count_entities",
                "selector": {"source": "pdc", "study_id": "f1c59a53-ab7c-11e9-9a07-0a80fada099c"},
            }
            pdc_cert = tmp_dir / "pdc_count.json"
            pdc_ans = evaluate(pdc_dir, pdc_query)
            issue_certificate(pdc_dir, pdc_query, pdc_cert)
            pdc_replay = replay_certificate(pdc_dir, pdc_cert)

            v3_manifest = json.loads((gdc_v3_dir / "manifest.json").read_text(encoding="utf-8"))
            gdc_v3_sup_query = {
                "operation": "cohort_count",
                "snapshot_id": v3_manifest["snapshot_id"],
                "cohort_id": v3_manifest["cohort_id"],
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
            gdc_v3_sup_cert = tmp_dir / "gdc_v3_supported.json"
            gdc_v3_sup_ans = evaluate(gdc_v3_dir, gdc_v3_sup_query)
            issue_certificate(gdc_v3_dir, gdc_v3_sup_query, gdc_v3_sup_cert)
            gdc_v3_sup_replay = replay_certificate(gdc_v3_dir, gdc_v3_sup_cert)

            gdc_v3_abs_query = {
                "operation": "cohort_count",
                "snapshot_id": v3_manifest["snapshot_id"],
                "cohort_id": v3_manifest["cohort_id"],
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
            gdc_v3_abs_cert = tmp_dir / "gdc_v3_abstaining.json"
            gdc_v3_abs_ans = evaluate(gdc_v3_dir, gdc_v3_abs_query)
            issue_certificate(gdc_v3_dir, gdc_v3_abs_query, gdc_v3_abs_cert)
            gdc_v3_abs_replay = replay_certificate(gdc_v3_dir, gdc_v3_abs_cert)

            # Confirm real snapshot rejects patient/biospecimen biomarker claims when only case-level summary exists
            gdc_overlap_attempt = {
                "operation": "cross_source_overlap_count",
                "entity_level": "patient",
                "denominator_basis": "unmapped_real_cohorts",
                "requested_sources": ["gdc", "pdc"],
            }
            xs_ans = evaluate(gdc_dir, gdc_overlap_attempt)
            xs_cert = tmp_dir / "xs.json"
            issue_certificate(gdc_dir, gdc_overlap_attempt, xs_cert)
            xs_replay = replay_certificate(gdc_dir, xs_cert)

        report = {
            "report_version": "1.2.0",
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "network_mode": "disabled_socket_guard",
            "gdc_tcga_luad": {
                "summary": gdc_summary,
                "case_count_eval": gdc_ans,
                "case_count_replay": gdc_replay,
            },
            "gdc_tcga_luad_v2": {
                "summary": gdc_v2_summary,
                "sample_file_availability": {
                    "query": gdc_v2_sup_query,
                    "status": gdc_v2_sup_ans["status"],
                    "result": gdc_v2_sup_ans["result"],
                    "witness_count": len(gdc_v2_sup_ans["witness"]),
                    "replay": gdc_v2_sup_replay,
                },
                "unacquired_assay_abstention": {
                    "query": gdc_v2_abs_query,
                    "eval": gdc_v2_abs_ans,
                    "replay": gdc_v2_abs_replay,
                },
            },
            "gdc_tcga_luad_v3": {
                "summary": gdc_v3_summary,
                "egfr_activating_somatic_variants": {
                    "query": gdc_v3_sup_query,
                    "status": gdc_v3_sup_ans["status"],
                    "numerator_count": (gdc_v3_sup_ans.get("result") or {}).get("numerator_count"),
                    "denominator_count": (gdc_v3_sup_ans.get("result") or {}).get("denominator_count"),
                    "percentage": (gdc_v3_sup_ans.get("result") or {}).get("percentage"),
                    "witness_count": len(gdc_v3_sup_ans["witness"]),
                    "replay": gdc_v3_sup_replay,
                },
                "negative_wildtype_abstention": {
                    "query": gdc_v3_abs_query,
                    "eval": gdc_v3_abs_ans,
                    "replay": gdc_v3_abs_replay,
                },
            },
            "pdc_pdc000153": {
                "summary": pdc_summary,
                "case_count_eval": pdc_ans,
                "case_count_replay": pdc_replay,
            },
            "cross_source_overlap_refusal": {
                "query": gdc_overlap_attempt,
                "eval": xs_ans,
                "replay": xs_replay,
            },
            "real_data_readiness_gaps": [
                "Checked-in GDC v1 (gdc_tcga_luad) and PDC (pdc_pdc000153) snapshots capture case-level summary attributes only.",
                "GDC v2 (gdc_tcga_luad_v2) adds linked case -> sample -> aliquot -> open RNA-Seq TSV file metadata but leaves assays unacquired (PARTIAL_INGEST).",
                "GDC v3 (gdc_tcga_luad_v3) adds open-access WXS Masked Somatic Mutation MAF files (618 files, 571 Primary Tumor samples) and all 93 EGFR /ssm_occurrences (GRCh38, ENST00000275493), but only records called positive masked somatic variants; locus-level callable reference depth and explicit negative wild-type calls are absent.",
                "PDC biospecimen/molecular integration, additional genes beyond EGFR, external cross-cohort identity mappings, production key custody, and independent external audit remain future/external work.",
            ],
        }
        out_path = ROOT / "artifacts" / "real-data-readiness" / "real_snapshots_report.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "out_path": str(out_path),
                    "gdc_replay": gdc_replay,
                    "gdc_v2_sup_status": gdc_v2_sup_ans["status"],
                    "gdc_v2_sup_replay": gdc_v2_sup_replay,
                    "gdc_v3_sup_status": gdc_v3_sup_ans["status"],
                    "gdc_v3_sup_counts": {
                        "numerator": (gdc_v3_sup_ans.get("result") or {}).get("numerator_count"),
                        "denominator": (gdc_v3_sup_ans.get("result") or {}).get("denominator_count"),
                    },
                    "gdc_v3_sup_replay": gdc_v3_sup_replay,
                    "gdc_v3_abs_status": gdc_v3_abs_ans["status"],
                    "gdc_v3_abs_replay": gdc_v3_abs_replay,
                    "pdc_replay": pdc_replay,
                    "xs_status": xs_ans["status"],
                },
                indent=2,
            )
        )
        return 0
    finally:
        socket.socket = orig_socket  # type: ignore[assignment]


if __name__ == "__main__":
    raise SystemExit(main())
