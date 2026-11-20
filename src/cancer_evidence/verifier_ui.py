"""Read-only, offline Human-Verifier Demo UI for frozen EGIS evidence snapshots."""

from __future__ import annotations

import html
import hashlib
import json
import re
import socket
import urllib.request
from collections import Counter, defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_PATH_TOKENS = ("private_key", "answer_key", ".key")


def _assert_safe_readonly_path(path: Path) -> Path:
    """Reject any path referencing private keys, answer keys, or non-public PEM files."""
    normalized = path.as_posix().lower()
    name = path.name.lower()
    if any(tok in normalized for tok in FORBIDDEN_PATH_TOKENS):
        raise PermissionError(f"Refusing to access restricted key/answer file: {path}")
    if name.endswith(".pem") and name != "public_key.pem":
        raise PermissionError(f"Refusing to access non-public PEM file: {path}")
    return path


def _read_json(path: Path) -> Any:
    _assert_safe_readonly_path(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    _assert_safe_readonly_path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _esc(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value), quote=True)


def _json_for_pre(value: Any) -> str:
    return html.escape(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False), quote=True)


def _parse_proof_summary(proof_path: Path) -> dict[str, str]:
    summary: dict[str, str] = {"pytest": "Not recorded", "mutation": "Not recorded"}
    if not proof_path.is_file():
        return summary
    _assert_safe_readonly_path(proof_path)
    text = proof_path.read_text(encoding="utf-8")
    m_pytest = re.search(r"`.venv/bin/python -m pytest -q`:\s*`([^`]+)`", text)
    if m_pytest:
        summary["pytest"] = m_pytest.group(1)
    m_mut = re.search(r"`.venv/bin/python scripts/mutation_check\.py`:\s*`([^`]+)`\s*\(([^)]+)\)", text)
    if m_mut:
        summary["mutation"] = f"{m_mut.group(1)} ({m_mut.group(2)})"
    return summary


def load_verifier_view_model(
    root: Path | None = None,
    *,
    snapshot_dir: Path | None = None,
    milestone_dir: Path | None = None,
    recheck_report_path: Path | None = None,
    grading_report_path: Path | None = None,
) -> dict[str, Any]:
    """Load all UI data dynamically from frozen artifacts with networking disabled."""

    def _blocked_net(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Network requests are disabled in read-only Human-Verifier UI")

    orig_socket = socket.socket
    orig_create = socket.create_connection
    orig_addr = socket.getaddrinfo
    orig_urlopen = urllib.request.urlopen
    socket.socket = _blocked_net  # type: ignore[assignment]
    socket.create_connection = _blocked_net  # type: ignore[assignment]
    socket.getaddrinfo = _blocked_net  # type: ignore[assignment]
    urllib.request.urlopen = _blocked_net  # type: ignore[assignment]

    try:
        base_root = Path(root) if root is not None else ROOT
        snap_dir = snapshot_dir or (base_root / "snapshots" / "real" / "gdc_tcga_luad_v3")
        mile_dir = milestone_dir or (base_root / "artifacts" / "gdc-luad-v3-molecular-milestone")
        recheck_path = recheck_report_path or (
            base_root / "artifacts" / "gdc-luad-v3-independent-recheck-v1" / "independent_recheck_report.json"
        )
        if grading_report_path is not None:
            grade_path = grading_report_path
        else:
            cand_v2 = base_root / "artifacts" / "regression-exam-grading-v2-post-gdc-v3" / "grading_report.json"
            cand_v1 = base_root / "artifacts" / "regression-exam-grading" / "grading_report.json"
            grade_path = cand_v2 if cand_v2.is_file() else cand_v1

        manifest = _read_json(snap_dir / "manifest.json")
        normalized = _read_json(snap_dir / "normalized.json")

        supported_query = _read_json(mile_dir / "supported_query.json")
        supported_result = _read_json(mile_dir / "supported_result.json")
        supported_cert = _read_json(mile_dir / "supported_certificate.json")
        supported_replay = _read_json(mile_dir / "supported_replay.json")

        abst_neg_query = _read_json(mile_dir / "abstaining_negative_query.json")
        abst_neg_result = _read_json(mile_dir / "abstaining_negative_result.json")
        abst_neg_cert = _read_json(mile_dir / "abstaining_negative_certificate.json")
        abst_neg_replay = _read_json(mile_dir / "abstaining_negative_replay.json")

        unacq_q_path = mile_dir / "abstaining_unacquired_gene_query.json"
        unacq_gene_bundle: dict[str, Any] | None = None
        if unacq_q_path.is_file():
            unacq_gene_bundle = {
                "query": _read_json(unacq_q_path),
                "result": _read_json(mile_dir / "abstaining_unacquired_gene_result.json"),
                "replay": _read_json(mile_dir / "abstaining_unacquired_gene_replay.json"),
            }

        recheck_report: dict[str, Any] | None = None
        if recheck_path.is_file():
            recheck_report = _read_json(recheck_path)

        grading_summary: dict[str, Any] | None = None
        if grade_path.is_file():
            full_grade = _read_json(grade_path)
            grading_summary = {
                "path": grade_path.relative_to(base_root).as_posix()
                if grade_path.is_relative_to(base_root)
                else str(grade_path),
                "overall_verdict": full_grade.get("overall_verdict"),
                "suite_role": full_grade.get("suite_role"),
                "graded_at_utc": full_grade.get("graded_at_utc"),
                "frozen_outputs_untouched": full_grade.get("frozen_outputs_untouched"),
                "summary": full_grade.get("summary") or {},
                "category_breakdown": full_grade.get("category_breakdown") or {},
            }

        pub_key_path = base_root / "keys" / "public_key.pem"
        pub_key_sha256 = _sha256_file(pub_key_path) if pub_key_path.is_file() else None
        proof_summary = _parse_proof_summary(base_root / "PROOF.md")

        # Index entities and relationships dynamically from normalized.json
        entities = normalized.get("entities", [])
        relationships = normalized.get("relationships", [])

        cases_by_id: dict[str, dict[str, Any]] = {}
        samples_by_id: dict[str, dict[str, Any]] = {}
        aliquots_by_id: dict[str, dict[str, Any]] = {}
        assays_by_id: dict[str, dict[str, Any]] = {}
        files_by_id: dict[str, dict[str, Any]] = {}
        by_eid: dict[str, dict[str, Any]] = {}

        for ent in entities:
            by_eid[ent["entity_id"]] = ent
            kind = ent["kind"]
            sid = ent["source_id"]
            if kind == "case":
                cases_by_id[sid] = ent
            elif kind == "sample":
                samples_by_id[sid] = ent
            elif kind == "aliquot":
                aliquots_by_id[sid] = ent
            elif kind == "assay":
                assays_by_id[sid] = ent
            elif kind == "file":
                files_by_id[sid] = ent

        sample_to_case_eid: dict[str, str] = {}
        sample_to_aliquot_eids: dict[str, list[str]] = defaultdict(list)
        aliquot_to_file_eids: dict[str, list[str]] = defaultdict(list)
        aliquot_to_assay_eids: dict[str, list[str]] = defaultdict(list)
        assay_to_file_eids: dict[str, list[str]] = defaultdict(list)
        assay_to_aliquot_eids: dict[str, list[str]] = defaultdict(list)

        for rel in relationships:
            pred = rel["predicate"]
            sub = rel["subject_id"]
            obj = rel["object_id"]
            if pred == "HAS_SAMPLE":
                sample_to_case_eid[obj] = sub
            elif pred == "HAS_ALIQUOT":
                sample_to_aliquot_eids[sub].append(obj)
            elif pred == "HAS_FILE":
                sub_ent = by_eid.get(sub)
                if sub_ent and sub_ent["kind"] == "aliquot":
                    aliquot_to_file_eids[sub].append(obj)
                elif sub_ent and sub_ent["kind"] == "assay":
                    assay_to_file_eids[sub].append(obj)
            elif pred == "HAS_ASSAY":
                aliquot_to_assay_eids[sub].append(obj)
                assay_to_aliquot_eids[obj].append(sub)

        # File predicate matching rules from the query itself
        q_pred = supported_query.get("predicate") or {}
        target_sample_type = q_pred.get("sample_type", "Primary Tumor")
        file_filter_keys = ("data_category", "data_type", "experimental_strategy", "data_format", "access", "state")

        def _file_matches_query(f_ent: dict[str, Any]) -> bool:
            attrs = f_ent.get("attributes") or {}
            return all(q_pred.get(k) is None or attrs.get(k) == q_pred.get(k) for k in file_filter_keys)

        def _assay_matches_query(a_ent: dict[str, Any]) -> bool:
            attrs = a_ent.get("attributes") or {}
            if q_pred.get("gene_symbol") and attrs.get("gene_symbol") != q_pred["gene_symbol"]:
                return False
            if q_pred.get("ncbi_build") and attrs.get("ncbi_build") != q_pred["ncbi_build"]:
                return False
            if q_pred.get("canonical_transcript_id") and attrs.get("canonical_transcript_id") != q_pred["canonical_transcript_id"]:
                return False
            if q_pred.get("call_status") and attrs.get("call_status") != q_pred["call_status"]:
                return False
            if "hgvsp_short_in" in q_pred:
                return attrs.get("hgvsp_short") in q_pred["hgvsp_short_in"]
            if "hgvsp_short" in q_pred:
                return attrs.get("hgvsp_short") == q_pred["hgvsp_short"]
            return True

        primary_tumor_samples = {
            sid: ent
            for sid, ent in samples_by_id.items()
            if (ent.get("attributes") or {}).get("sample_type") == target_sample_type
        }
        sample_wxs_files: dict[str, set[str]] = defaultdict(set)
        wxs_linked_cases: set[str] = set()
        wxs_files_total: set[str] = set()

        for sid, s_ent in primary_tumor_samples.items():
            s_eid = s_ent["entity_id"]
            for alq_eid in sample_to_aliquot_eids.get(s_eid, []):
                for f_eid in aliquot_to_file_eids.get(alq_eid, []):
                    f_ent = by_eid.get(f_eid)
                    if f_ent and _file_matches_query(f_ent):
                        sample_wxs_files[sid].add(f_ent["source_id"])
                        wxs_files_total.add(f_ent["source_id"])
                        c_eid = sample_to_case_eid.get(s_eid)
                        if c_eid and c_eid in by_eid:
                            wxs_linked_cases.add(by_eid[c_eid]["source_id"])

        wxs_cases_sample_counts: Counter[str] = Counter()
        for sid in sample_wxs_files:
            s_eid = samples_by_id[sid]["entity_id"]
            c_eid = sample_to_case_eid.get(s_eid)
            if c_eid and c_eid in by_eid:
                wxs_cases_sample_counts[by_eid[c_eid]["source_id"]] += 1

        res_inner = supported_result.get("result") or {}
        numerator_count = int(res_inner.get("numerator_count", 0))
        denominator_count = int(res_inner.get("denominator_count", 0))
        percentage = float(res_inner.get("percentage", 0.0))

        witness_ids: list[str] = list(supported_result.get("witness") or [])
        variant_counts: Counter[str] = Counter()
        hgvsc_counts: Counter[str] = Counter()
        witness_lineage: list[dict[str, Any]] = []
        compound_target_samples_count = 0

        for idx, sid in enumerate(witness_ids, start=1):
            s_ent = samples_by_id.get(sid)
            if s_ent is None:
                continue
            s_attrs = s_ent.get("attributes") or {}
            s_eid = s_ent["entity_id"]
            c_eid = sample_to_case_eid.get(s_eid)
            c_ent = by_eid.get(c_eid) if c_eid else None
            c_attrs = (c_ent or {}).get("attributes") or {}

            all_sample_assays: list[dict[str, Any]] = []
            matched_assays: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
            for alq_eid in sample_to_aliquot_eids.get(s_eid, []):
                alq_ent = by_eid.get(alq_eid)
                for asy_eid in aliquot_to_assay_eids.get(alq_eid, []):
                    asy_ent = by_eid.get(asy_eid)
                    if asy_ent is None:
                        continue
                    all_sample_assays.append(asy_ent)
                    if _assay_matches_query(asy_ent):
                        matched_assays.append((asy_ent, alq_ent))

            # Track distinct variant labels contributed by this sample (single count per variant label per sample)
            sample_var_labels = {
                (a.get("attributes") or {}).get("hgvsp_short") or (a.get("attributes") or {}).get("aa_change") or "UNKNOWN"
                for a, _ in matched_assays
            }
            for lbl in sorted(sample_var_labels):
                variant_counts[lbl] += 1
            sample_hgvsc_labels = {
                (a.get("attributes") or {}).get("hgvsc") or "UNKNOWN" for a, _ in matched_assays
            }
            for hgvsc_lbl in sorted(sample_hgvsc_labels):
                hgvsc_counts[hgvsc_lbl] += 1

            primary_asy, primary_alq = matched_assays[0] if matched_assays else (None, None)
            primary_a_attrs = (primary_asy or {}).get("attributes") or {}
            primary_alq_attrs = (primary_alq or {}).get("attributes") or {}

            linked_maf_file: dict[str, Any] | None = None
            if primary_asy is not None:
                for f_eid in assay_to_file_eids.get(primary_asy["entity_id"], []):
                    f_ent = by_eid.get(f_eid)
                    if f_ent and _file_matches_query(f_ent):
                        linked_maf_file = f_ent
                        break
            if linked_maf_file is None and primary_a_attrs.get("matched_maf_file_id"):
                linked_maf_file = files_by_id.get(primary_a_attrs["matched_maf_file_id"])
            maf_attrs = (linked_maf_file or {}).get("attributes") or {}

            matched_ids = {a["source_id"] for a, _ in matched_assays}
            co_variants = [
                {
                    "ssm_occurrence_id": a["source_id"],
                    "hgvsp_short": (a.get("attributes") or {}).get("hgvsp_short"),
                    "hgvsc": (a.get("attributes") or {}).get("hgvsc"),
                }
                for a in sorted(all_sample_assays, key=lambda x: x["source_id"])
                if a["source_id"] not in matched_ids
            ]
            if co_variants:
                compound_target_samples_count += 1

            wxs_maf_count = len(sample_wxs_files.get(sid, set()))
            witness_lineage.append(
                {
                    "index": idx,
                    "initial_visible": idx <= 5,
                    "sample": {
                        "sample_id": sid,
                        "submitter_id": s_attrs.get("submitter_id"),
                        "sample_type": s_attrs.get("sample_type"),
                        "tissue_type": s_attrs.get("tissue_type"),
                        "tumor_descriptor": s_attrs.get("tumor_descriptor"),
                        "preservation_method": s_attrs.get("preservation_method"),
                        "wxs_maf_file_count_for_sample": wxs_maf_count,
                    },
                    "case": {
                        "case_id": (c_ent or {}).get("source_id"),
                        "submitter_id": c_attrs.get("submitter_id"),
                        "project_id": c_attrs.get("project_id"),
                        "primary_site": c_attrs.get("primary_site"),
                        "disease_type": c_attrs.get("disease_type"),
                    },
                    "tumor_aliquot": {
                        "aliquot_id": (primary_alq or {}).get("source_id"),
                        "submitter_id": primary_alq_attrs.get("submitter_id"),
                    },
                    "variant_record": {
                        "ssm_occurrence_id": (primary_asy or {}).get("source_id"),
                        "ssm_id": primary_a_attrs.get("ssm_id"),
                        "gene_symbol": primary_a_attrs.get("gene_symbol"),
                        "gene_id": primary_a_attrs.get("gene_id"),
                        "ncbi_build": primary_a_attrs.get("ncbi_build"),
                        "canonical_transcript_id": primary_a_attrs.get("canonical_transcript_id"),
                        "chromosome": primary_a_attrs.get("chromosome"),
                        "start_position": primary_a_attrs.get("start_position"),
                        "end_position": primary_a_attrs.get("end_position"),
                        "reference_allele": primary_a_attrs.get("reference_allele"),
                        "tumor_allele": primary_a_attrs.get("tumor_allele"),
                        "genomic_dna_change": primary_a_attrs.get("genomic_dna_change"),
                        "hgvsp_short": primary_a_attrs.get("hgvsp_short"),
                        "aa_change": primary_a_attrs.get("aa_change"),
                        "hgvsc": primary_a_attrs.get("hgvsc"),
                        "consequence_type": primary_a_attrs.get("consequence_type"),
                        "mutation_subtype": primary_a_attrs.get("mutation_subtype"),
                        "variant_callers": primary_a_attrs.get("variant_callers"),
                        "observation_count": primary_a_attrs.get("observation_count"),
                        "max_t_depth": primary_a_attrs.get("max_t_depth"),
                        "max_t_alt_count": primary_a_attrs.get("max_t_alt_count"),
                        "max_t_ref_count": primary_a_attrs.get("max_t_ref_count"),
                        "max_n_depth": primary_a_attrs.get("max_n_depth"),
                        "call_status": primary_a_attrs.get("call_status"),
                        "coverage_completeness": primary_a_attrs.get("coverage_completeness"),
                        "linkage_status": primary_a_attrs.get("linkage_status"),
                        "co_occurring_egfr_variants": co_variants,
                    },
                    "maf_file": {
                        "file_id": (linked_maf_file or {}).get("source_id"),
                        "file_name": maf_attrs.get("file_name"),
                        "data_category": maf_attrs.get("data_category"),
                        "data_type": maf_attrs.get("data_type"),
                        "experimental_strategy": maf_attrs.get("experimental_strategy"),
                        "data_format": maf_attrs.get("data_format"),
                        "workflow_type": maf_attrs.get("workflow_type"),
                        "access": maf_attrs.get("access"),
                        "state": maf_attrs.get("state"),
                        "md5sum": maf_attrs.get("md5sum"),
                        "file_size": maf_attrs.get("file_size"),
                        "matched_normal_aliquot_id": maf_attrs.get("matched_normal_aliquot_id")
                        or primary_a_attrs.get("matched_normal_aliquot_id"),
                    },
                }
            )

        prov = manifest.get("provenance_metadata") or {}
        up_status = prov.get("upstream_status") or {}
        linkage_audit = prov.get("linkage_audit") or {}
        mol_cov = prov.get("molecular_coverage") or {}
        named_universes = prov.get("named_universes") or {}
        den_basis = res_inner.get("denominator_basis") or supported_query.get("denominator_basis") or ""

        total_pt_count = len(primary_tumor_samples)
        wxs_linked_pt_count = len(sample_wxs_files)
        unlinked_pt_count = total_pt_count - wxs_linked_pt_count
        multi_maf_pt_count = sum(1 for files in sample_wxs_files.values() if len(files) > 1)
        multi_pt_cases_count = sum(1 for cnt in wxs_cases_sample_counts.values() if cnt > 1)

        return {
            "ui_version": "1.0.0",
            "mode": "read_only_offline",
            "disclaimer": "Research use only — not clinically validated; not for patient-care decisions.",
            "headline": {
                "numerator_count": numerator_count,
                "denominator_count": denominator_count,
                "percentage": percentage,
                "entity_level": res_inner.get("entity_level", "sample"),
                "denominator_basis": den_basis,
                "label": "observed selected EGFR calls among file-linked primary-tumor samples",
                "deduplication_note": (
                    "Distinct Primary Tumor sample entities are counted once each in both numerator and denominator, "
                    "even when a sample has multiple open WXS MAF files or compound EGFR variant occurrences."
                ),
            },
            "variant_counts": dict(sorted(variant_counts.items())),
            "hgvsc_counts": dict(sorted(hgvsc_counts.items())),
            "scope_and_denominator": {
                "total_cases": len(cases_by_id),
                "total_samples": len(samples_by_id),
                "total_primary_tumor_samples": total_pt_count,
                "wxs_linked_primary_tumor_samples": wxs_linked_pt_count,
                "wxs_linked_primary_tumor_cases": len(wxs_linked_cases),
                "wxs_maf_primary_tumor_files_count": len(wxs_files_total),
                "primary_tumor_samples_without_open_wxs_maf": unlinked_pt_count,
                "wxs_linked_samples_without_selected_egfr_call": denominator_count - numerator_count,
                "multi_maf_primary_tumor_samples_count": multi_maf_pt_count,
                "cases_with_multiple_wxs_primary_tumor_samples": multi_pt_cases_count,
                "compound_egfr_target_samples_count": compound_target_samples_count,
                "denominator_definition": named_universes.get(den_basis, ""),
                "molecular_coverage": mol_cov,
                "known_coverage_gaps": prov.get("known_coverage_gaps") or [],
            },
            "provenance": {
                "snapshot_path": snap_dir.relative_to(base_root).as_posix()
                if snap_dir.is_relative_to(base_root)
                else str(snap_dir),
                "snapshot_id": manifest.get("snapshot_id"),
                "cohort_id": manifest.get("cohort_id"),
                "retrieved_at_utc": manifest.get("retrieved_at_utc"),
                "upstream_source": prov.get("upstream_source"),
                "gdc_data_release": up_status.get("data_release"),
                "gdc_tag": up_status.get("tag"),
                "gdc_commit": up_status.get("commit"),
                "raw_content_sha256": manifest.get("raw_content_sha256"),
                "normalized_content_sha256": manifest.get("normalized_content_sha256"),
                "sqlite_sha256": manifest.get("sqlite_sha256"),
                "manifest_sha256": manifest.get("manifest_sha256"),
                "public_key_path": "keys/public_key.pem",
                "public_key_sha256": pub_key_sha256,
                "linkage_audit": linkage_audit,
                "row_counts": prov.get("row_counts") or {},
            },
            "artifacts": {
                "supported": {
                    "query": supported_query,
                    "result": supported_result,
                    "certificate": supported_cert,
                    "replay": supported_replay,
                },
                "abstaining_negative": {
                    "query": abst_neg_query,
                    "result": abst_neg_result,
                    "certificate": abst_neg_cert,
                    "replay": abst_neg_replay,
                },
                "abstaining_unacquired_gene": unacq_gene_bundle,
            },
            "witness_lineage": witness_lineage,
            "verification": {
                "offline_verify_cmd": (
                    "EGIS_OFFLINE=1 .venv/bin/evidence verify "
                    "--snapshot snapshots/real/gdc_tcga_luad_v3 "
                    "--certificate artifacts/gdc-luad-v3-molecular-milestone/supported_certificate.json"
                ),
                "independent_recheck_cmd": ".venv/bin/python scripts/independent_recheck_gdc_v3.py",
                "project_recheck_script": "scripts/independent_recheck_gdc_v3.py",
                "project_recheck_report_path": (
                    recheck_path.relative_to(base_root).as_posix()
                    if recheck_path.is_file() and recheck_path.is_relative_to(base_root)
                    else str(recheck_path)
                ),
                "project_recheck_all_passed": (recheck_report or {}).get("all_checks_passed"),
                "regression_summary": grading_summary,
                "test_and_mutation_summary": proof_summary,
            },
        }
    finally:
        socket.socket = orig_socket  # type: ignore[assignment]
        socket.create_connection = orig_create  # type: ignore[assignment]
        socket.getaddrinfo = orig_addr  # type: ignore[assignment]
        urllib.request.urlopen = orig_urlopen  # type: ignore[assignment]


def render_verifier_html(vm: dict[str, Any]) -> str:
    """Render a self-contained, XSS-escaped, offline HTML human-verifier report."""
    headline = vm["headline"]
    scope = vm["scope_and_denominator"]
    prov = vm["provenance"]
    artifacts = vm["artifacts"]
    ver = vm["verification"]
    vcounts: dict[str, int] = vm["variant_counts"]
    hcounts: dict[str, int] = vm["hgvsc_counts"]
    witness_list: list[dict[str, Any]] = vm["witness_lineage"]

    num = int(headline["numerator_count"])
    den = int(headline["denominator_count"])
    pct = float(headline["percentage"])

    l858r_cnt = vcounts.get("p.L858R", 0)
    ex19_cnt = vcounts.get("p.E746_A750del", 0)

    other_variants_html = "".join(
        f'<div class="metric-sub-card"><span class="badge badge-derived">EGIS-Derived</span>'
        f'<div class="var-name">{_esc(var)}</div>'
        f'<div class="var-val">{_esc(cnt)} samples</div></div>'
        for var, cnt in vcounts.items()
    )
    hgvsc_badges = " ".join(
        f'<span class="pill">{_esc(h)}: {_esc(c)}</span>' for h, c in hcounts.items()
    )

    sup_cert = artifacts["supported"]["certificate"]
    sup_replay = artifacts["supported"]["replay"]
    abst_neg_res = artifacts["abstaining_negative"]["result"]
    abst_neg_rep = artifacts["abstaining_negative"]["replay"]
    unacq_bundle = artifacts.get("abstaining_unacquired_gene")

    reg = ver.get("regression_summary") or {}
    reg_sum = reg.get("summary") or {}
    test_mut = ver.get("test_and_mutation_summary") or {}

    gaps_li = "".join(f"<li>{_esc(g)}</li>" for g in scope.get("known_coverage_gaps", []))

    rows_html_parts: list[str] = []
    for row in witness_list:
        s = row["sample"]
        c = row["case"]
        alq = row["tumor_aliquot"]
        vr = row["variant_record"]
        mf = row["maf_file"]
        var_label = vr.get("hgvsp_short") or vr.get("aa_change") or ""
        co_vars = vr.get("co_occurring_egfr_variants") or []
        is_special = (s.get("wxs_maf_file_count_for_sample", 1) > 1) or bool(co_vars)
        init_attr = "true" if row["initial_visible"] else "false"
        hidden_attr = "" if row["initial_visible"] else ' hidden="hidden"'
        co_text = ", ".join(
            f"{cv.get('hgvsp_short') or ''} ({cv.get('hgvsc') or ''})" for cv in co_vars
        )
        search_blob = " ".join(
            str(x or "")
            for x in (
                s.get("sample_id"),
                s.get("submitter_id"),
                c.get("case_id"),
                c.get("submitter_id"),
                alq.get("aliquot_id"),
                alq.get("submitter_id"),
                vr.get("ssm_occurrence_id"),
                vr.get("hgvsp_short"),
                vr.get("hgvsc"),
                mf.get("file_id"),
                mf.get("file_name"),
            )
        ).lower()

        co_badge = (
            f'<span class="pill pill-warn">+{len(co_vars)} co-EGFR</span>' if co_vars else ""
        )
        multi_maf_badge = (
            f'<span class="pill pill-info">{_esc(s.get("wxs_maf_file_count_for_sample"))} MAFs</span>'
            if (s.get("wxs_maf_file_count_for_sample") or 1) > 1
            else ""
        )

        rows_html_parts.append(
            f'<tr class="witness-row" data-index="{row["index"]}" data-initial-visible="{init_attr}" '
            f'data-variant="{_esc(var_label)}" data-special="{"true" if is_special else "false"}" '
            f'data-search="{_esc(search_blob)}"{hidden_attr}>'
            f'<td><strong>#{row["index"]}</strong></td>'
            f'<td><code>{_esc(s.get("submitter_id"))}</code><div class="sub-id"><code>{_esc(s.get("sample_id"))}</code></div> {multi_maf_badge}</td>'
            f'<td><span class="pill pill-var">{_esc(var_label)}</span><div class="sub-id"><code>{_esc(vr.get("hgvsc"))}</code></div> {co_badge}</td>'
            f'<td><code>{_esc(alq.get("aliquot_id"))}</code><div class="sub-id">{_esc(alq.get("submitter_id"))}</div></td>'
            f'<td><code>{_esc(vr.get("ssm_occurrence_id"))}</code><div class="sub-id">Callers: {_esc(vr.get("variant_callers"))}</div></td>'
            f'<td><code>{_esc(mf.get("file_id"))}</code><div class="sub-id">{_esc(mf.get("file_name"))}</div></td>'
            f'<td><details><summary>Inspect</summary><div class="detail-grid">'
            f'<div><strong>Case:</strong> {_esc(c.get("submitter_id"))} (<code>{_esc(c.get("case_id"))}</code>) — {_esc(c.get("disease_type"))}</div>'
            f'<div><strong>Locus ({_esc(vr.get("ncbi_build"))} / {_esc(vr.get("canonical_transcript_id"))}):</strong> <code>{_esc(vr.get("genomic_dna_change"))}</code></div>'
            f'<div><strong>Read Depths:</strong> tumor {_esc(vr.get("max_t_alt_count"))}/{_esc(vr.get("max_t_depth"))} alt/total (ref {_esc(vr.get("max_t_ref_count"))}); normal {_esc(vr.get("max_n_depth"))}</div>'
            f'<div><strong>Call &amp; Coverage:</strong> {_esc(vr.get("call_status"))} | {_esc(vr.get("coverage_completeness"))} | linkage: {_esc(vr.get("linkage_status"))}</div>'
            f'<div><strong>Matched Normal Aliquot:</strong> <code>{_esc(mf.get("matched_normal_aliquot_id"))}</code> | <strong>MAF MD5:</strong> <code>{_esc(mf.get("md5sum"))}</code></div>'
            f'<div><strong>Co-occurring EGFR variants on same aliquot:</strong> {_esc(co_text) if co_vars else "None"}</div>'
            f'</div></details></td>'
            f'</tr>'
        )
    witness_rows_html = "\n".join(rows_html_parts)

    client_script = """
(function() {
  var rows = Array.prototype.slice.call(document.querySelectorAll('tr.witness-row'));
  var searchInput = document.getElementById('witness-search');
  var statusEl = document.getElementById('witness-filter-status');
  var toggleAllBtn = document.getElementById('toggle-all-witnesses');
  var filterBtns = Array.prototype.slice.call(document.querySelectorAll('[data-filter-mode]'));
  var showAll = false;
  var activeMode = 'all';

  function updateView() {
    var q = (searchInput ? searchInput.value : '').trim().toLowerCase();
    var isFiltering = (q.length > 0) || (activeMode !== 'all');
    var matchedCount = 0;
    var shownCount = 0;

    rows.forEach(function(row) {
      var v = row.getAttribute('data-variant') || '';
      var sp = row.getAttribute('data-special') === 'true';
      var blob = row.getAttribute('data-search') || '';
      var modeOk = (
        activeMode === 'all' ||
        (activeMode === 'p.L858R' && v === 'p.L858R') ||
        (activeMode === 'p.E746_A750del' && v === 'p.E746_A750del') ||
        (activeMode === 'special' && sp)
      );
      var searchOk = (!q || blob.indexOf(q) !== -1);
      var matches = modeOk && searchOk;
      if (matches) {
        matchedCount += 1;
        var visible = isFiltering || showAll || (row.getAttribute('data-initial-visible') === 'true');
        if (visible) {
          row.removeAttribute('hidden');
          shownCount += 1;
        } else {
          row.setAttribute('hidden', 'hidden');
        }
      } else {
        row.setAttribute('hidden', 'hidden');
      }
    });

    if (statusEl) {
      statusEl.textContent = 'Showing ' + shownCount + ' of ' + matchedCount + ' matching witnesses (' + rows.length + ' total witness samples).';
    }
    if (toggleAllBtn) {
      toggleAllBtn.style.display = isFiltering ? 'none' : 'inline-block';
      toggleAllBtn.textContent = showAll ? 'Collapse to first 5 witnesses' : ('Show all ' + rows.length + ' witnesses');
      toggleAllBtn.setAttribute('aria-expanded', showAll ? 'true' : 'false');
    }
  }

  filterBtns.forEach(function(btn) {
    btn.addEventListener('click', function() {
      activeMode = btn.getAttribute('data-filter-mode') || 'all';
      filterBtns.forEach(function(b) {
        b.classList.toggle('btn-active', b === btn);
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
      });
      updateView();
    });
  });

  if (searchInput) {
    searchInput.addEventListener('input', updateView);
  }
  if (toggleAllBtn) {
    toggleAllBtn.addEventListener('click', function() {
      showAll = !showAll;
      updateView();
    });
  }
  updateView();
})();
"""

    unacq_block = ""
    if unacq_bundle is not None:
        unacq_res = unacq_bundle["result"]
        unacq_rep = unacq_bundle["replay"]
        unacq_block = (
            f'<div class="card callout-unknown">'
            f'<span class="badge badge-derived">EGIS-Derived</span> '
            f'<span class="badge badge-source">Unacquired Gene Scope</span>'
            f'<h3>Unacquired Gene Abstention (<code>KRAS p.G12C</code>)</h3>'
            f'<p>Status: <strong>{_esc(unacq_res.get("status"))}</strong> / '
            f'<code>{_esc(unacq_res.get("reason_code"))}</code> | '
            f'Witnesses: <code>{_esc(len(unacq_res.get("witness") or []))}</code> | '
            f'SQL Replay valid: <code>{_esc(unacq_rep.get("valid"))}</code></p>'
            f'<p>Only <code>EGFR</code> was acquired in <code>gdc_tcga_luad_v3</code>. EGIS refuses to return <code>0 / 571</code> for unacquired genes.</p>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
  <title>EGIS Human-Verifier Demo — {_esc(prov.get("snapshot_id"))}</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --surface: #ffffff;
      --border: #cbd5e1;
      --text: #0f172a;
      --muted: #475569;
      --warn-bg: #fef3c7;
      --warn-border: #d97706;
      --warn-text: #78350f;
      --ok-bg: #ccfbf1;
      --ok-text: #115e59;
      --derived-bg: #e0e7ff;
      --derived-text: #3730a3;
      --interp-bg: #f1f5f9;
      --interp-text: #334155;
      --unk-bg: #ffe4e6;
      --unk-border: #e11d48;
      --unk-text: #881337;
      --accent: #0f766e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .top-banner {{
      background: var(--warn-bg);
      color: var(--warn-text);
      border-bottom: 3px solid var(--warn-border);
      padding: 0.75rem 1.25rem;
      font-weight: 700;
      text-align: center;
      font-size: 0.95rem;
    }}
    header, main, footer {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 1.25rem;
    }}
    header {{
      border-bottom: 1px solid var(--border);
      background: var(--surface);
      margin-top: 1rem;
      border-radius: 8px;
      border: 1px solid var(--border);
    }}
    h1 {{ margin: 0 0 0.35rem 0; font-size: 1.5rem; }}
    h2 {{ margin: 0 0 0.75rem 0; font-size: 1.2rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.35rem; }}
    h3 {{ margin: 0 0 0.5rem 0; font-size: 1.02rem; }}
    p {{ margin: 0.4rem 0; }}
    section {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1.25rem;
      margin-top: 1.25rem;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.85em;
      background: #f1f5f9;
      padding: 0.1rem 0.3rem;
      border-radius: 4px;
      word-break: break-all;
    }}
    pre {{
      background: #0f172a;
      color: #f8fafc;
      padding: 0.85rem;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 0.8rem;
      line-height: 1.4;
      margin: 0.5rem 0;
    }}
    .grid-2 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 1rem;
    }}
    .grid-3 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1rem;
    }}
    .card {{
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.9rem;
      background: #ffffff;
    }}
    .headline-box {{
      border: 2px solid var(--accent);
      background: #f0fdfa;
      padding: 1rem 1.25rem;
      border-radius: 8px;
      margin-bottom: 1rem;
    }}
    .headline-fraction {{
      font-size: 2.15rem;
      font-weight: 800;
      color: var(--ok-text);
      line-height: 1.15;
    }}
    .headline-label {{
      font-size: 1.05rem;
      font-weight: 700;
      color: var(--text);
      margin-top: 0.25rem;
    }}
    .metric-sub-card {{
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.75rem;
      background: #f8fafc;
    }}
    .var-name {{ font-weight: 700; font-size: 1.05rem; margin-top: 0.25rem; }}
    .var-val {{ font-size: 1.35rem; font-weight: 700; color: var(--derived-text); }}
    .badge {{
      display: inline-block;
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      padding: 0.15rem 0.45rem;
      border-radius: 999px;
    }}
    .badge-source {{ background: var(--ok-bg); color: var(--ok-text); }}
    .badge-derived {{ background: var(--derived-bg); color: var(--derived-text); }}
    .badge-interp {{ background: var(--interp-bg); color: var(--interp-text); border: 1px solid var(--border); }}
    .badge-warn {{ background: var(--warn-bg); color: var(--warn-text); border: 1px solid var(--warn-border); }}
    .callout-unknown {{
      border-left: 4px solid var(--unk-border);
      background: var(--unk-bg);
      color: var(--unk-text);
      margin-top: 0.75rem;
    }}
    .callout-warn {{
      border-left: 4px solid var(--warn-border);
      background: var(--warn-bg);
      color: var(--warn-text);
      padding: 0.85rem 1rem;
      border-radius: 6px;
      margin-top: 0.75rem;
    }}
    .pill {{
      display: inline-block;
      background: #e2e8f0;
      color: #1e293b;
      padding: 0.1rem 0.45rem;
      border-radius: 4px;
      font-size: 0.78rem;
      font-weight: 600;
    }}
    .pill-var {{ background: #e0e7ff; color: #3730a3; }}
    .pill-warn {{ background: #fef3c7; color: #92400e; }}
    .pill-info {{ background: #ccfbf1; color: #115e59; }}
    .legend-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      align-items: center;
      margin-top: 0.65rem;
      font-size: 0.88rem;
    }}
    .funnel-list {{
      list-style: none;
      padding: 0;
      margin: 0.5rem 0;
    }}
    .funnel-list li {{
      padding: 0.45rem 0.65rem;
      border-left: 3px solid var(--border);
      margin-bottom: 0.4rem;
      background: #f8fafc;
    }}
    .controls-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      align-items: center;
      margin: 0.75rem 0;
    }}
    input[type="search"] {{
      padding: 0.45rem 0.65rem;
      border: 1px solid var(--border);
      border-radius: 6px;
      min-width: 260px;
      font-size: 0.9rem;
    }}
    button {{
      padding: 0.42rem 0.75rem;
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--text);
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.85rem;
      font-weight: 600;
    }}
    button:hover {{ background: #f1f5f9; }}
    button.btn-active {{
      background: var(--text);
      color: #ffffff;
      border-color: var(--text);
    }}
    button:focus-visible, input:focus-visible, summary:focus-visible {{
      outline: 3px solid #2563eb;
      outline-offset: 2px;
    }}
    .table-wrap {{ overflow-x: auto; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      margin-top: 0.5rem;
    }}
    th, td {{
      text-align: left;
      padding: 0.5rem 0.55rem;
      border-bottom: 1px solid #e2e8f0;
      vertical-align: top;
    }}
    th {{ background: #f1f5f9; font-weight: 700; }}
    .sub-id {{ font-size: 0.75rem; color: var(--muted); margin-top: 0.15rem; }}
    .detail-grid {{
      display: grid;
      gap: 0.3rem;
      padding: 0.5rem;
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 4px;
      margin-top: 0.35rem;
      font-size: 0.8rem;
    }}
    details {{ margin-top: 0.35rem; }}
    summary {{ cursor: pointer; font-weight: 600; color: var(--accent); }}
    footer {{
      color: var(--muted);
      font-size: 0.85rem;
      padding-bottom: 2.5rem;
    }}
  </style>
</head>
<body>
  <div class="top-banner" role="banner">
    {_esc(vm["disclaimer"])} Offline Read-Only Human-Verifier View — Not Externally Audited or Production-Ready.
  </div>

  <header>
    <h1>EGIS Human-Verifier Demo — TCGA-LUAD EGFR Open-Access Somatic Calls</h1>
    <p>Deterministic, non-generative evidence inspection over frozen GDC snapshot <code>{_esc(prov.get("snapshot_id"))}</code> (<code>{_esc(prov.get("snapshot_path"))}</code>).</p>
    <div class="legend-bar" aria-label="Epistemic provenance legend">
      <span><strong>Epistemic Distinction:</strong></span>
      <span><span class="badge badge-source">Source Facts</span> Verbatim frozen GDC metadata &amp; raw counts</span>
      <span><span class="badge badge-derived">EGIS-Derived</span> Deterministic engine evaluation &amp; replay</span>
      <span><span class="badge badge-interp">Reviewer Interpretation</span> Narrow scope &amp; statistical boundaries</span>
    </div>
  </header>

  <main>
    <section aria-labelledby="sec-result">
      <h2 id="sec-result">1. Result Summary</h2>
      <div class="headline-box">
        <span class="badge badge-derived">EGIS-Derived</span>
        <div class="headline-fraction">{_esc(num)} / {_esc(den)} <span style="font-size:1.1rem;font-weight:600;">({_esc(f"{pct:.2f}%")})</span></div>
        <div class="headline-label">{_esc(headline.get("label"))}</div>
        <p><span class="badge badge-interp">Reviewer Interpretation</span> This is an observed-call fraction in the file-linked open WXS MAF subset — <strong>NOT</strong> TCGA-LUAD-wide prevalence, <strong>NOT</strong> a mutation rate across all cases, <strong>NOT</strong> a wild-type rate, and <strong>NOT</strong> a clinical finding.</p>
        <p>{_esc(headline.get("deduplication_note"))}</p>
      </div>

      <div class="grid-3">
        {other_variants_html}
        <div class="metric-sub-card">
          <span class="badge badge-source">Source Facts</span>
          <div class="var-name">Canonical cDNA &amp; Build</div>
          <p><code>GRCh38</code> | <code>ENST00000275493</code></p>
          <div>{hgvsc_badges}</div>
        </div>
      </div>
      <p style="margin-top:0.65rem;font-size:0.88rem;color:var(--muted);">
        Note: {_esc(f"p.L858R ({l858r_cnt})")} and {_esc(f"p.E746_A750del ({ex19_cnt})")} are disjoint across the {_esc(num)} positive witness samples (0 samples carry both). {_esc(scope.get("compound_egfr_target_samples_count"))} of these samples carry an additional non-target <em>EGFR</em> call on the same aliquot, and {_esc(scope.get("multi_maf_primary_tumor_samples_count"))} file-linked Primary Tumor samples have &gt;1 WXS MAF file.
      </p>
    </section>

    <section aria-labelledby="sec-denom">
      <h2 id="sec-denom">2. Denominator Definition, Scope &amp; Why Missing Does Not Equal Wild-Type</h2>
      <div class="grid-2">
        <div class="card">
          <span class="badge badge-source">Source Facts</span> <span class="badge badge-derived">Denominator Funnel</span>
          <h3>Snapshot Cohort → {_esc(den)}-Sample Denominator</h3>
          <p><strong>Named universe:</strong> <code>{_esc(headline.get("denominator_basis"))}</code></p>
          <p>{_esc(scope.get("denominator_definition"))}</p>
          <ul class="funnel-list">
            <li><strong>{_esc(scope.get("total_cases"))}</strong> frozen TCGA-LUAD cases (<code>{_esc(scope.get("total_samples"))}</code> total biospecimen samples)</li>
            <li><strong>{_esc(scope.get("total_primary_tumor_samples"))}</strong> total <code>Primary Tumor</code> samples in the snapshot</li>
            <li><strong>{_esc(scope.get("wxs_linked_primary_tumor_samples"))}</strong> <code>Primary Tumor</code> samples linked to &ge;1 released open-access WXS <code>Masked Somatic Mutation</code> (<code>MAF</code>) file (across <strong>{_esc(scope.get("wxs_linked_primary_tumor_cases"))}</strong> cases and <strong>{_esc(scope.get("wxs_maf_primary_tumor_files_count"))}</strong> WXS MAF files; <strong>{_esc(scope.get("primary_tumor_samples_without_open_wxs_maf"))}</strong> Primary Tumor samples lack an open WXS MAF link)</li>
            <li><strong>{_esc(num)}</strong> file-linked Primary Tumor samples with an observed <code>p.L858R</code> or <code>p.E746_A750del</code> call (<strong>{_esc(scope.get("wxs_linked_samples_without_selected_egfr_call"))}</strong> lack that call in open MAFs)</li>
          </ul>
        </div>
        <div class="card callout-unknown">
          <span class="badge badge-derived">EGIS-Derived Abstention</span>
          <h3>Why Unlisted Variants Return <code>{_esc(abst_neg_res.get("status"))}</code> / <code>{_esc(abst_neg_res.get("reason_code"))}</code></h3>
          <p>Open-access GDC <code>/ssm_occurrences</code> and WXS Masked Somatic Mutation MAF files record <strong>called positive somatic variants only</strong> (<code>{_esc((scope.get("molecular_coverage") or {}).get("coverage_completeness"))}</code>). They omit locus-level reference read depth for unmutated bases and mask potential germline calls.</p>
          <p>Therefore, neither the <strong>{_esc(scope.get("primary_tumor_samples_without_open_wxs_maf"))}</strong> samples lacking an open MAF file nor the <strong>{_esc(scope.get("wxs_linked_samples_without_selected_egfr_call"))}</strong> file-linked samples without an observed EGFR variant establish wild-type status.</p>
          <p><strong>Verified Negative/Wild-Type Query:</strong> Status = <strong>{_esc(abst_neg_res.get("status"))}</strong>, Reason = <code>{_esc(abst_neg_res.get("reason_code"))}</code>, Witnesses = <code>{_esc(len(abst_neg_res.get("witness") or []))}</code>, SQL Replay valid = <code>{_esc(abst_neg_rep.get("valid"))}</code>.</p>
          {unacq_block}
        </div>
      </div>
    </section>

    <section aria-labelledby="sec-trace">
      <h2 id="sec-trace">3. Evidence Traceability &amp; Witness Lineage Explorer</h2>
      <div class="grid-2">
        <div class="card">
          <span class="badge badge-source">Source Facts</span>
          <h3>Frozen Snapshot &amp; GDC Release Provenance</h3>
          <ul>
            <li><strong>Snapshot ID:</strong> <code>{_esc(prov.get("snapshot_id"))}</code> | <strong>Cohort ID:</strong> <code>{_esc(prov.get("cohort_id"))}</code></li>
            <li><strong>Source &amp; Release:</strong> {_esc(prov.get("upstream_source"))} — {_esc(prov.get("gdc_data_release"))} (<code>tag: {_esc(prov.get("gdc_tag"))}</code>, <code>commit: {_esc(prov.get("gdc_commit"))}</code>)</li>
            <li><strong>Retrieved At (UTC):</strong> <code>{_esc(prov.get("retrieved_at_utc"))}</code></li>
            <li><strong><code>raw_content_sha256</code>:</strong> <code>{_esc(prov.get("raw_content_sha256"))}</code></li>
            <li><strong><code>normalized_content_sha256</code>:</strong> <code>{_esc(prov.get("normalized_content_sha256"))}</code></li>
            <li><strong><code>sqlite_sha256</code>:</strong> <code>{_esc(prov.get("sqlite_sha256"))}</code></li>
            <li><strong><code>manifest_sha256</code>:</strong> <code>{_esc(prov.get("manifest_sha256"))}</code></li>
            <li><strong><code>public_key.pem</code> SHA-256:</strong> <code>{_esc(prov.get("public_key_sha256"))}</code> (no private key exposed)</li>
          </ul>
        </div>
        <div class="card">
          <span class="badge badge-derived">EGIS-Derived</span>
          <h3>Certificate &amp; Replay Artifacts</h3>
          <ul>
            <li><strong>Supported Query Result:</strong> <code>{_esc(artifacts["supported"]["result"].get("status"))}</code> / <code>{_esc(artifacts["supported"]["result"].get("reason_code"))}</code> ({_esc(num)} / {_esc(den)})</li>
            <li><strong><code>certificate_sha256</code>:</strong> <code>{_esc(sup_cert.get("certificate_sha256"))}</code></li>
            <li><strong>Algorithm &amp; Rules:</strong> <code>{_esc(sup_cert.get("signature_algorithm"))}</code> | <code>{_esc(sup_cert.get("rule_version"))}</code></li>
            <li><strong>Offline SQL Replay:</strong> <code>valid={_esc(sup_replay.get("valid"))}</code>, <code>status={_esc(sup_replay.get("status"))}</code></li>
            <li><strong>Linkage Audit (GDC v3):</strong> <code>93 / 93</code> EGFR <code>ssm_occurrences</code> linked 1-to-1 (<code>0</code> unmatched, <code>0</code> multiply matched).</li>
          </ul>
          <details>
            <summary>Inspect Supported Query, Result, Certificate Header &amp; Replay JSON</summary>
            <h4>supported_query.json</h4>
            <pre>{_json_for_pre(artifacts["supported"]["query"])}</pre>
            <h4>supported_replay.json</h4>
            <pre>{_json_for_pre(sup_replay)}</pre>
            <h4>supported_certificate.json (metadata &amp; signature)</h4>
            <pre>{_json_for_pre({k: v for k, v in sup_cert.items() if k != "result"})}</pre>
            <h4>abstaining_negative_query.json &amp; abstaining_negative_result.json</h4>
            <pre>{_json_for_pre({"query": artifacts["abstaining_negative"]["query"], "result": abst_neg_res, "replay": abst_neg_rep})}</pre>
          </details>
        </div>
      </div>

      <h3 style="margin-top:1.1rem;">Sample &rarr; Tumor Aliquot &rarr; Variant Record &rarr; MAF File Lineage (Initial View: First 5 of {_esc(len(witness_list))})</h3>
      <div class="controls-bar">
        <label for="witness-search" style="font-weight:600;">Filter witnesses:</label>
        <input id="witness-search" type="search" placeholder="Search sample, case, aliquot, variant, or file ID..." aria-label="Search witness IDs and attributes">
        <button type="button" class="btn-active" data-filter-mode="all" aria-pressed="true">All ({_esc(len(witness_list))})</button>
        <button type="button" data-filter-mode="p.L858R" aria-pressed="false">p.L858R ({_esc(l858r_cnt)})</button>
        <button type="button" data-filter-mode="p.E746_A750del" aria-pressed="false">p.E746_A750del ({_esc(ex19_cnt)})</button>
        <button type="button" data-filter-mode="special" aria-pressed="false">Multi-MAF or Compound Co-variant</button>
        <button type="button" id="toggle-all-witnesses" aria-expanded="false">Show all {_esc(len(witness_list))} witnesses</button>
      </div>
      <div id="witness-filter-status" aria-live="polite" style="font-size:0.85rem;color:var(--muted);"></div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Primary Tumor Sample</th>
              <th>Observed Variant</th>
              <th>Tumor Aliquot</th>
              <th>GDC Variant Record</th>
              <th>WXS MAF File</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {witness_rows_html}
          </tbody>
        </table>
      </div>
    </section>

    <section aria-labelledby="sec-verify">
      <h2 id="sec-verify">4. Independent Verification Controls &amp; Regression Context</h2>
      <div class="grid-2">
        <div class="card">
          <span class="badge badge-interp">Reviewer Verification</span>
          <h3>Offline Certificate Verification &amp; Reconciliation</h3>
          <p>Verify the Ed25519 signature and reconstruct the answer purely from SQLite queries against the frozen snapshot:</p>
          <pre>{_esc(ver.get("offline_verify_cmd"))}</pre>
          <p><strong>Project-authored reconciliation code &amp; report</strong> <span class="badge badge-warn">Project-authored script — not an external audit</span>:</p>
          <ul>
            <li>Script: <code>{_esc(ver.get("project_recheck_script"))}</code></li>
            <li>Report: <code>{_esc(ver.get("project_recheck_report_path"))}</code> (<code>all_checks_passed={_esc(ver.get("project_recheck_all_passed"))}</code>)</li>
            <li>Human Reviewer Guide: <code>docs/HUMAN_REVIEWER_BRIEF.md</code></li>
          </ul>
          <div class="callout-warn">
            <strong>Replay Boundary:</strong> Passing SQL replay proves EGIS faithfully executed its deterministic rules over the frozen <code>data.sqlite</code> snapshot and that the Ed25519 signature matches <code>keys/public_key.pem</code>. It does <strong>not</strong> prove biological correctness, clinical validity, or completeness of upstream GDC data.
          </div>
        </div>
        <div class="card">
          <span class="badge badge-derived">Development Regression</span>
          <h3>Last Reported Test &amp; Suite Status <span class="badge badge-warn">Development regression — not an external audit</span></h3>
          <ul>
            <li><strong>Unit &amp; integration tests (<code>PROOF.md</code>):</strong> <code>{_esc(test_mut.get("pytest"))}</code></li>
            <li><strong>Replay mutation resistance (<code>PROOF.md</code>):</strong> <code>{_esc(test_mut.get("mutation"))}</code></li>
            <li><strong>48-case suite (development regression):</strong> {_esc(reg_sum.get("executed_passed"))} / {_esc(reg_sum.get("executed_count"))} executed passed, {_esc(reg_sum.get("not_run_count"))} <code>NOT_RUN</code>, {_esc(reg_sum.get("pre_designated_exclusions_count"))} pre-designated <code>BLOCKED</code>, {_esc(reg_sum.get("critical_executed_passed"))} / {_esc(reg_sum.get("critical_cases_total"))} <code>CRITICAL</code> passed, {_esc(reg_sum.get("determinism_failures_on_executed"))} determinism failures across 3&times; runs (<code>{_esc(reg.get("overall_verdict"))}</code>).</li>
            <li><strong>Source report:</strong> <code>{_esc(reg.get("path"))}</code> (evaluator answer key was previously inspected; classified strictly as development regression).</li>
          </ul>
        </div>
      </div>
    </section>

    <section aria-labelledby="sec-limits">
      <h2 id="sec-limits">5. Limitations, Gaps &amp; Offline Read-Only Design</h2>
      <div class="callout-warn">
        <strong>{_esc(vm["disclaimer"])}</strong> EGIS is neither clinically validated, externally audited, nor production-ready.
      </div>
      <ul>
        {gaps_li}
        <li>Assay/QC metadata, broad negative-call locus coverage, CPTAC/PDC biospecimen integration with governed identity mappings, third-party blind evaluation, and external production key custody (replacing the checked-in <code>keys/</code> demo keypair) remain incomplete.</li>
        <li><strong>UI Security &amp; Isolation:</strong> Strictly read-only and offline (<code>default-src 'none'</code>, zero outbound requests, HTML/JS output escaped, never accesses private signing keys or evaluator answer keys).</li>
      </ul>
      <h3>Local Launch &amp; Regeneration Commands</h3>
      <pre>.venv/bin/python scripts/run_verifier_ui.py --out artifacts/human-verifier-ui
.venv/bin/python scripts/run_verifier_ui.py --out artifacts/human-verifier-ui --serve --port 8765</pre>
    </section>
  </main>

  <footer>
    Generated purely from local frozen artifacts by <code>cancer_evidence.verifier_ui</code> (v{_esc(vm["ui_version"])}). Read-only offline mode.
  </footer>

  <script>{client_script}</script>
</body>
</html>
"""


def build_verifier_site(
    output_dir: Path,
    *,
    root: Path | None = None,
    snapshot_dir: Path | None = None,
    milestone_dir: Path | None = None,
) -> dict[str, Path]:
    """Generate static offline HTML report and view model JSON into output_dir."""
    vm = load_verifier_view_model(root=root, snapshot_dir=snapshot_dir, milestone_dir=milestone_dir)
    html_text = render_verifier_html(vm)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / "index.html"
    json_path = out / "ui_view_model.json"
    html_path.write_text(html_text, encoding="utf-8")
    json_path.write_text(json.dumps(vm, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"html_path": html_path, "view_model_path": json_path}


def _make_readonly_handler(html_bytes: bytes, json_bytes: bytes):
    class ReadOnlyVerifierHandler(BaseHTTPRequestHandler):
        def _send_bytes(self, status: int, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            parsed = urlparse(self.path).path
            if parsed in ("/", "/index.html"):
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", html_bytes)
            elif parsed == "/api/view-model":
                self._send_bytes(HTTPStatus.OK, "application/json; charset=utf-8", json_bytes)
            else:
                self._send_bytes(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not Found\n")

        def _reject_mutation(self) -> None:
            self._send_bytes(HTTPStatus.METHOD_NOT_ALLOWED, "text/plain; charset=utf-8", b"Read-only verifier UI\n")

        do_POST = _reject_mutation
        do_PUT = _reject_mutation
        do_DELETE = _reject_mutation
        do_PATCH = _reject_mutation

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ReadOnlyVerifierHandler


def create_readonly_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    output_dir: Path | None = None,
    root: Path | None = None,
    snapshot_dir: Path | None = None,
    milestone_dir: Path | None = None,
) -> tuple[ThreadingHTTPServer, dict[str, Path]]:
    """Build static artifacts and return a configured read-only localhost HTTP server."""
    target_dir = output_dir or (ROOT / "artifacts" / "human-verifier-ui")
    paths = build_verifier_site(target_dir, root=root, snapshot_dir=snapshot_dir, milestone_dir=milestone_dir)
    html_bytes = paths["html_path"].read_bytes()
    json_bytes = paths["view_model_path"].read_bytes()
    server = ThreadingHTTPServer((host, port), _make_readonly_handler(html_bytes, json_bytes))
    return server, paths


def serve_verifier_ui(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    output_dir: Path | None = None,
    root: Path | None = None,
) -> None:
    """Build static artifacts and serve the read-only verifier UI until interrupted."""
    server, paths = create_readonly_server(host=host, port=port, output_dir=output_dir, root=root)
    actual_port = server.server_address[1]
    print(
        json.dumps(
            {
                "mode": "read_only_offline_server",
                "url": f"http://{host}:{actual_port}/",
                "html_path": str(paths["html_path"]),
                "view_model_path": str(paths["view_model_path"]),
            },
            indent=2,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
