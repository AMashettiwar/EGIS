#!/usr/bin/env python3
"""Offline independent source reconciliation, evaluation, certification, and replay for GDC TCGA-LUAD v3 molecular evidence."""

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
    raise RuntimeError("network disabled during offline gdc-v3 molecular verification")

socket.socket = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
urllib.request.urlopen = _blocked
"""


def _blocked_net(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("network disabled during offline gdc-v3 source reconciliation")


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
        snapshot_dir = ROOT / "snapshots" / "real" / "gdc_tcga_luad_v3"
        out_dir = ROOT / "artifacts" / "gdc-luad-v3-molecular-milestone"
        out_dir.mkdir(parents=True, exist_ok=True)

        manifest_bytes = (snapshot_dir / "manifest.json").read_bytes()
        raw_bytes = (snapshot_dir / "raw.json").read_bytes()
        norm_bytes = (snapshot_dir / "normalized.json").read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        raw = json.loads(raw_bytes.decode("utf-8"))
        normalized = json.loads(norm_bytes.decode("utf-8"))

        raw_hash_matches = hashlib.sha256(raw_bytes).hexdigest() == manifest.get("raw_content_sha256")
        norm_hash_matches = hashlib.sha256(norm_bytes).hexdigest() == manifest.get("normalized_content_sha256")

        # Independent derivation directly from GDC raw_capture (zero EGIS engine imports)
        raw_capture = raw["raw_capture"]
        raw_cases: dict[str, dict[str, Any]] = {}
        raw_samples: dict[str, tuple[str, str | None]] = {}
        raw_aliquots: dict[str, str] = {}
        for page in raw_capture["case_pages"]:
            for c in page["response"]["data"]["hits"]:
                cid = c["case_id"]
                raw_cases[cid] = c
                for s in c.get("samples", []) or []:
                    sid = s["sample_id"]
                    if sid not in raw_samples:
                        raw_samples[sid] = (cid, s.get("sample_type"))
                    for p in s.get("portions", []) or []:
                        for an in p.get("analytes", []) or []:
                            for alq in an.get("aliquots", []) or []:
                                raw_aliquots[alq["aliquot_id"]] = sid

        primary_tumor_samples = {sid for sid, (_, st) in raw_samples.items() if st == "Primary Tumor"}
        normal_types = {"Blood Derived Normal", "Solid Tissue Normal"}

        wxs_maf_files: dict[str, dict[str, Any]] = {}
        wxs_primary_tumor_samples: set[str] = set()
        wxs_primary_tumor_aliquots: set[str] = set()
        pair_to_files: dict[frozenset[str], list[str]] = {}
        snv_orphan_sample_links = 0
        snv_orphan_aliquot_links = 0

        for page in raw_capture["snv_file_pages"]:
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
                        sid = s.get("sample_id")
                        st = s.get("sample_type")
                        if sid not in raw_samples:
                            snv_orphan_sample_links += 1
                            continue
                        for p in s.get("portions", []) or []:
                            for an in p.get("analytes", []) or []:
                                for alq in an.get("aliquots", []) or []:
                                    alqid = alq.get("aliquot_id")
                                    if alqid not in raw_aliquots:
                                        snv_orphan_aliquot_links += 1
                                    else:
                                        alq_set.add(alqid)
                                        if qualifies and st == "Primary Tumor" and sid in primary_tumor_samples:
                                            wxs_primary_tumor_samples.add(sid)
                                            wxs_primary_tumor_aliquots.add(alqid)
                if qualifies and alq_set:
                    pair_to_files.setdefault(frozenset(alq_set), []).append(fid)

        target_hgvsp_short = {"p.L858R", "p.E746_A750del"}
        ssm_total = 0
        ssm_unmatched = 0
        ssm_multiply_matched = 0
        ssm_all_egfr_samples: set[str] = set()
        expected_samples: set[str] = set()
        expected_occs: set[str] = set()
        expected_aliquots: set[str] = set()
        expected_files: set[str] = set()
        variant_breakdown: dict[str, int] = {}

        for page in raw_capture["egfr_ssm_pages"]:
            for occ in page["response"]["data"]["hits"]:
                ssm_total += 1
                occ_id = occ["ssm_occurrence_id"]
                ssm = occ.get("ssm") or {}
                obs = (occ.get("case") or {}).get("observation") or []
                t_alqs = {
                    (o.get("sample") or {}).get("tumor_sample_uuid")
                    for o in obs
                    if (o.get("sample") or {}).get("tumor_sample_uuid") in raw_aliquots
                }
                smps = {raw_aliquots[a] for a in t_alqs}
                if len(smps) == 0:
                    ssm_unmatched += 1
                    continue
                if len(smps) > 1 or len(t_alqs) != 1:
                    ssm_multiply_matched += 1
                    continue
                sid = next(iter(smps))
                t_alq = next(iter(t_alqs))
                ssm_all_egfr_samples.add(sid)

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

                canon_tr = None
                for cons in ssm.get("consequence", []) or []:
                    tr = cons.get("transcript") or {}
                    if (tr.get("gene") or {}).get("symbol") == "EGFR" and tr.get("is_canonical") is True:
                        canon_tr = tr
                        break
                if canon_tr is None:
                    continue
                ann = canon_tr.get("annotation") or {}
                hgvsp = ann.get("hgvsp_short")
                if (
                    ssm.get("ncbi_build") == "GRCh38"
                    and canon_tr.get("transcript_id") == "ENST00000275493"
                    and sid in wxs_primary_tumor_samples
                    and hgvsp in target_hgvsp_short
                ):
                    expected_samples.add(sid)
                    expected_occs.add(occ_id)
                    expected_aliquots.add(t_alq)
                    expected_files.update(matched_files)
                    variant_breakdown[hgvsp] = variant_breakdown.get(hgvsp, 0) + 1

        # Cross-check independent derivation directly against normalized.json entity/relationship records
        norm_assay_matches = {
            e["source_id"]
            for e in normalized["entities"]
            if e["kind"] == "assay"
            and e["attributes"].get("gene_symbol") == "EGFR"
            and e["attributes"].get("ncbi_build") == "GRCh38"
            and e["attributes"].get("canonical_transcript_id") == "ENST00000275493"
            and e["attributes"].get("hgvsp_short") in target_hgvsp_short
            and e["attributes"].get("call_status") == "POSITIVE"
        }

        expected_witness = sorted(expected_samples)
        expected_occ_ids = sorted(expected_occs)
        expected_aliquot_ids = sorted(expected_aliquots)
        expected_file_ids = sorted(expected_files)
        expected_lineage_refs = sorted(
            expected_samples | expected_occs | expected_aliquots | expected_files
        )
        expected_num = len(expected_witness)
        expected_den = len(wxs_primary_tumor_samples)

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

        sup_q_path = out_dir / "supported_query.json"
        sup_res_path = out_dir / "supported_result.json"
        sup_cert_path = out_dir / "supported_certificate.json"
        sup_rep_path = out_dir / "supported_replay.json"

        neg_q_path = out_dir / "abstaining_negative_query.json"
        neg_res_path = out_dir / "abstaining_negative_result.json"
        neg_cert_path = out_dir / "abstaining_negative_certificate.json"
        neg_rep_path = out_dir / "abstaining_negative_replay.json"

        gene_q_path = out_dir / "abstaining_unacquired_gene_query.json"
        gene_res_path = out_dir / "abstaining_unacquired_gene_result.json"
        gene_cert_path = out_dir / "abstaining_unacquired_gene_certificate.json"
        gene_rep_path = out_dir / "abstaining_unacquired_gene_replay.json"

        sup_q_path.write_text(json.dumps(supported_query, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        neg_q_path.write_text(json.dumps(abstaining_negative_query, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        gene_q_path.write_text(json.dumps(abstaining_unacquired_gene_query, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        cli_prefix = _find_evidence_cli(ROOT)
        with tempfile.TemporaryDirectory(prefix="egis_gdc_v3_offline_") as tmp:
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

            neg_ans = _run_offline_cli(
                cli_prefix,
                ["answer", "--snapshot", str(snapshot_dir), "--query", str(neg_q_path), "--certificate", str(neg_cert_path)],
                scratch,
            )
            if neg_ans["returncode"] != 0 or not isinstance(neg_ans["stdout_json"], dict):
                raise RuntimeError(f"abstaining negative answer failed: {neg_ans}")
            neg_rep = _run_offline_cli(
                cli_prefix,
                ["verify", "--snapshot", str(snapshot_dir), "--certificate", str(neg_cert_path)],
                scratch,
            )

            gene_ans = _run_offline_cli(
                cli_prefix,
                ["answer", "--snapshot", str(snapshot_dir), "--query", str(gene_q_path), "--certificate", str(gene_cert_path)],
                scratch,
            )
            if gene_ans["returncode"] != 0 or not isinstance(gene_ans["stdout_json"], dict):
                raise RuntimeError(f"abstaining unacquired gene answer failed: {gene_ans}")
            gene_rep = _run_offline_cli(
                cli_prefix,
                ["verify", "--snapshot", str(snapshot_dir), "--certificate", str(gene_cert_path)],
                scratch,
            )

        sup_result_obj = sup_ans["stdout_json"]
        sup_replay_obj = sup_rep["stdout_json"] or {}
        neg_result_obj = neg_ans["stdout_json"]
        neg_replay_obj = neg_rep["stdout_json"] or {}
        gene_result_obj = gene_ans["stdout_json"]
        gene_replay_obj = gene_rep["stdout_json"] or {}

        sup_res_path.write_text(json.dumps(sup_result_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sup_rep_path.write_text(json.dumps(sup_replay_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        neg_res_path.write_text(json.dumps(neg_result_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        neg_rep_path.write_text(json.dumps(neg_replay_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        gene_res_path.write_text(json.dumps(gene_result_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        gene_rep_path.write_text(json.dumps(gene_replay_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        egis_res = sup_result_obj.get("result") or {}
        egis_witness = sorted(sup_result_obj.get("witness") or [])
        egis_refs = sorted(sup_result_obj.get("evidence_refs") or [])
        egis_var_ids = sorted(egis_res.get("supporting_variant_record_ids") or [])
        egis_alq_ids = sorted(egis_res.get("supporting_aliquot_ids") or [])
        egis_file_ids = sorted(egis_res.get("supporting_file_ids") or [])

        mismatches: list[str] = []
        if not raw_hash_matches or not norm_hash_matches:
            mismatches.append("raw_content_sha256 or normalized_content_sha256 mismatch with manifest.json")
        if sorted(norm_assay_matches) != expected_occ_ids:
            mismatches.append("normalized.json assay records differ from independent raw_capture calculation")
        if sup_result_obj.get("status") != "SUPPORTED":
            mismatches.append(f"supported status={sup_result_obj.get('status')!r} != 'SUPPORTED'")
        if egis_res.get("numerator_count") != expected_num:
            mismatches.append(f"numerator_count={egis_res.get('numerator_count')} != raw={expected_num}")
        if egis_res.get("denominator_count") != expected_den:
            mismatches.append(f"denominator_count={egis_res.get('denominator_count')} != raw={expected_den}")
        if egis_witness != expected_witness:
            mismatches.append("witness sample set mismatch")
        if egis_var_ids != expected_occ_ids:
            mismatches.append("supporting_variant_record_ids mismatch")
        if egis_alq_ids != expected_aliquot_ids:
            mismatches.append("supporting_aliquot_ids mismatch")
        if egis_file_ids != expected_file_ids:
            mismatches.append("supporting_file_ids mismatch")
        if egis_refs != expected_lineage_refs:
            mismatches.append("evidence_refs union mismatch")
        if sup_rep["returncode"] != 0 or sup_replay_obj.get("valid") is not True:
            mismatches.append(f"supported replay failed: rc={sup_rep['returncode']} obj={sup_replay_obj}")

        if neg_result_obj.get("status") != "UNKNOWN" or neg_result_obj.get("reason_code") != "INCOMPLETE_SNAPSHOT":
            mismatches.append(f"negative coverage query did not abstain: {neg_result_obj}")
        if neg_result_obj.get("witness") != [] or neg_result_obj.get("evidence_refs") != []:
            mismatches.append("negative coverage query emitted non-empty witness/refs")
        if neg_rep["returncode"] != 0 or neg_replay_obj.get("valid") is not True:
            mismatches.append(f"abstaining negative replay failed: {neg_replay_obj}")

        if gene_result_obj.get("status") != "UNKNOWN" or gene_result_obj.get("reason_code") != "INCOMPLETE_SNAPSHOT":
            mismatches.append(f"unacquired gene query did not abstain: {gene_result_obj}")
        if gene_rep["returncode"] != 0 or gene_replay_obj.get("valid") is not True:
            mismatches.append(f"abstaining unacquired gene replay failed: {gene_replay_obj}")

        report = {
            "report_version": "1.0.0",
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "network_mode": "disabled_socket_and_subprocess_guard",
            "snapshot_dir": "snapshots/real/gdc_tcga_luad_v3",
            "snapshot_id": snapshot_id,
            "cohort_id": cohort_id,
            "upstream_data_release": manifest.get("provenance_metadata", {}).get("upstream_status", {}).get("data_release"),
            "raw_content_sha256_verified": raw_hash_matches,
            "normalized_content_sha256_verified": norm_hash_matches,
            "independent_raw_counts": {
                "cases": len(raw_cases),
                "samples_total": len(raw_samples),
                "primary_tumor_samples": len(primary_tumor_samples),
                "aliquots_total": len(raw_aliquots),
                "wxs_masked_ssm_maf_files": len(wxs_maf_files),
                "wxs_masked_ssm_primary_tumor_samples": expected_den,
                "wxs_masked_ssm_primary_tumor_aliquots": len(wxs_primary_tumor_aliquots),
                "snv_orphan_sample_links": snv_orphan_sample_links,
                "snv_orphan_aliquot_links": snv_orphan_aliquot_links,
                "egfr_ssm_occurrences_total": ssm_total,
                "egfr_ssm_occurrences_unmatched": ssm_unmatched,
                "egfr_ssm_occurrences_multiply_matched": ssm_multiply_matched,
                "egfr_any_variant_primary_tumor_samples": len(ssm_all_egfr_samples),
                "target_canonical_egfr_samples": expected_num,
                "target_canonical_egfr_variant_breakdown": variant_breakdown,
                "target_ssm_occurrence_ids_count": len(expected_occ_ids),
                "target_tumor_aliquot_ids_count": len(expected_aliquot_ids),
                "target_wxs_maf_file_ids_count": len(expected_file_ids),
                "target_lineage_evidence_refs_count": len(expected_lineage_refs),
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
                "exact_witness_set_match": egis_witness == expected_witness,
                "exact_variant_record_id_match": egis_var_ids == expected_occ_ids,
                "exact_aliquot_id_match": egis_alq_ids == expected_aliquot_ids,
                "exact_file_id_match": egis_file_ids == expected_file_ids,
                "exact_evidence_refs_match": egis_refs == expected_lineage_refs,
                "replay_valid": sup_replay_obj.get("valid"),
            },
            "egis_abstaining_negative_comparison": {
                "status": neg_result_obj.get("status"),
                "reason_code": neg_result_obj.get("reason_code"),
                "witness_count": len(neg_result_obj.get("witness") or []),
                "replay_valid": neg_replay_obj.get("valid"),
            },
            "egis_abstaining_unacquired_gene_comparison": {
                "status": gene_result_obj.get("status"),
                "reason_code": gene_result_obj.get("reason_code"),
                "witness_count": len(gene_result_obj.get("witness") or []),
                "replay_valid": gene_replay_obj.get("valid"),
            },
            "mismatches": mismatches,
            "reconciled": len(mismatches) == 0,
        }
        report_path = out_dir / "source_reconciliation_report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "report_path": str(report_path),
                    "reconciled": report["reconciled"],
                    "mismatches": mismatches,
                    "independent_raw_counts": report["independent_raw_counts"],
                    "egis_supported_comparison": report["egis_supported_comparison"],
                    "egis_abstaining_negative_comparison": report["egis_abstaining_negative_comparison"],
                    "egis_abstaining_unacquired_gene_comparison": report["egis_abstaining_unacquired_gene_comparison"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if report["reconciled"] else 2
    finally:
        socket.socket = orig_socket  # type: ignore[assignment]
        socket.create_connection = orig_create  # type: ignore[assignment]
        socket.getaddrinfo = orig_addr  # type: ignore[assignment]
        urllib.request.urlopen = orig_urlopen  # type: ignore[assignment]


if __name__ == "__main__":
    raise SystemExit(main())
