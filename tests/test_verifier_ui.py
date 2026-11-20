import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cancer_evidence.verifier_ui import (
    _assert_safe_readonly_path,
    build_verifier_site,
    create_readonly_server,
    load_verifier_view_model,
    render_verifier_html,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_synthetic_ui_workspace(
    root: Path,
    *,
    num_total_pt: int = 250,
    num_wxs: int = 123,
    num_l858r: int = 4,
    num_ex19del: int = 3,
    inject_xss: bool = False,
) -> tuple[Path, Path, Path]:
    snap_dir = root / "snap_v3"
    mile_dir = root / "mile"
    snap_dir.mkdir(parents=True, exist_ok=True)
    mile_dir.mkdir(parents=True, exist_ok=True)

    entities: list[dict] = []
    relationships: list[dict] = []

    case_id = "SYN-CASE-001"
    case_eid = f"gdc:case:{case_id}"
    entities.append(
        {
            "entity_id": case_eid,
            "kind": "case",
            "source": "gdc",
            "source_id": case_id,
            "attributes": {
                "project_id": "TCGA-LUAD",
                "submitter_id": '<script>alert("xss")</script>' if inject_xss else "TCGA-SYN-01",
                "disease_type": "Adenomas and Adenocarcinomas",
            },
        }
    )

    witness_ids: list[str] = []
    pos_total = num_l858r + num_ex19del
    for i in range(num_total_pt):
        sid = f"SMP-{i:04d}"
        s_eid = f"gdc:sample:{sid}"
        s_barcode = f'TCGA-SMP-{i}" onload="evil()' if (inject_xss and i == 0) else f"TCGA-SMP-{i:04d}"
        entities.append(
            {
                "entity_id": s_eid,
                "kind": "sample",
                "source": "gdc",
                "source_id": sid,
                "attributes": {"sample_type": "Primary Tumor", "submitter_id": s_barcode},
            }
        )
        relationships.append(
            {"subject_id": case_eid, "predicate": "HAS_SAMPLE", "object_id": s_eid, "source": "gdc", "source_record_id": sid}
        )

        if i < num_wxs:
            aid = f"ALQ-{i:04d}"
            a_eid = f"gdc:aliquot:{aid}"
            fid = f"MAF-{i:04d}"
            f_eid = f"gdc:file:{fid}"
            entities.append(
                {
                    "entity_id": a_eid,
                    "kind": "aliquot",
                    "source": "gdc",
                    "source_id": aid,
                    "attributes": {"submitter_id": f"TCGA-ALQ-{i:04d}"},
                }
            )
            entities.append(
                {
                    "entity_id": f_eid,
                    "kind": "file",
                    "source": "gdc",
                    "source_id": fid,
                    "attributes": {
                        "file_name": '<img src=x onerror="evil()">.maf.gz' if (inject_xss and i == 0) else f"{fid}.maf.gz",
                        "data_category": "Simple Nucleotide Variation",
                        "data_type": "Masked Somatic Mutation",
                        "experimental_strategy": "WXS",
                        "data_format": "MAF",
                        "access": "open",
                        "state": "released",
                        "matched_normal_aliquot_id": f"NALQ-{i:04d}",
                        "md5sum": "0" * 32,
                    },
                }
            )
            relationships.append({"subject_id": s_eid, "predicate": "HAS_ALIQUOT", "object_id": a_eid, "source": "gdc", "source_record_id": f"{sid}->{aid}"})
            relationships.append({"subject_id": a_eid, "predicate": "HAS_FILE", "object_id": f_eid, "source": "gdc", "source_record_id": f"{aid}->{fid}"})

            if i < pos_total:
                witness_ids.append(sid)
                var = "p.L858R" if i < num_l858r else "p.E746_A750del"
                hgvsc = "c.2573T>G" if i < num_l858r else "c.2235_2249del"
                occ_id = f"OCC-{i:04d}"
                occ_eid = f"gdc:assay:{occ_id}"
                entities.append(
                    {
                        "entity_id": occ_eid,
                        "kind": "assay",
                        "source": "gdc",
                        "source_id": occ_id,
                        "attributes": {
                            "gene_symbol": "EGFR",
                            "ncbi_build": "GRCh38",
                            "canonical_transcript_id": "ENST00000275493",
                            "hgvsp_short": var,
                            "hgvsc": hgvsc,
                            "call_status": "POSITIVE",
                            "coverage_completeness": "POSITIVE_ONLY_OPEN_ACCESS_MAF",
                            "linkage_status": "UNIQUE_MATCH",
                            "variant_callers": "muse",
                        },
                    }
                )
                relationships.append({"subject_id": a_eid, "predicate": "HAS_ASSAY", "object_id": occ_eid, "source": "gdc", "source_record_id": f"{aid}->{occ_id}"})
                relationships.append({"subject_id": occ_eid, "predicate": "HAS_FILE", "object_id": f_eid, "source": "gdc", "source_record_id": f"{occ_id}->{fid}"})

    manifest = {
        "snapshot_id": "syn-snap-v3",
        "cohort_id": "syn_cohort_v3",
        "retrieved_at_utc": "2026-11-19T00:00:00+00:00",
        "raw_content_sha256": "a" * 64,
        "normalized_content_sha256": "b" * 64,
        "sqlite_sha256": "c" * 64,
        "manifest_sha256": "d" * 64,
        "provenance_metadata": {
            "upstream_source": "Synthetic Test GDC",
            "upstream_status": {"data_release": "Data Release Test", "tag": "test", "commit": "deadbeef"},
            "named_universes": {"syn_wxs_universe": "Synthetic WXS MAF primary tumor samples"},
            "molecular_coverage": {
                "coverage_completeness": "POSITIVE_ONLY_OPEN_ACCESS_MAF",
                "explicit_negative_calls_present": False,
                "locus_level_callable_coverage_present": False,
            },
            "known_coverage_gaps": ["Synthetic gap note"],
        },
    }
    (snap_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (snap_dir / "normalized.json").write_text(json.dumps({"entities": entities, "relationships": relationships}), encoding="utf-8")

    sup_query = {
        "operation": "cohort_count",
        "snapshot_id": "syn-snap-v3",
        "cohort_id": "syn_cohort_v3",
        "entity_level": "sample",
        "denominator_basis": "syn_wxs_universe",
        "predicate": {
            "source": "gdc",
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
    sup_result = {
        "status": "SUPPORTED",
        "reason_code": "NONE",
        "result": {
            "numerator_count": pos_total,
            "denominator_count": num_wxs,
            "percentage": pos_total / num_wxs * 100.0,
            "entity_level": "sample",
            "denominator_basis": "syn_wxs_universe",
        },
        "witness": witness_ids,
        "evidence_refs": witness_ids,
    }
    sup_cert = {
        "certificate_sha256": "e" * 64,
        "signature": "sig==",
        "signature_algorithm": "Ed25519",
        "rule_version": "rules-2.0.0",
        "query": sup_query,
        "result": sup_result,
    }
    sup_replay = {"valid": True, "status": "SUPPORTED", "reason_code": "NONE"}
    abst_query = copy.deepcopy(sup_query)
    abst_query["predicate"]["call_status"] = "NEGATIVE"
    abst_result = {"status": "UNKNOWN", "reason_code": "INCOMPLETE_SNAPSHOT", "result": {}, "witness": [], "evidence_refs": []}
    abst_cert = {
        "certificate_sha256": "f" * 64,
        "signature": "sig2==",
        "signature_algorithm": "Ed25519",
        "rule_version": "rules-2.0.0",
        "query": abst_query,
        "result": abst_result,
    }
    abst_replay = {"valid": True, "status": "UNKNOWN", "reason_code": "NONE"}

    (mile_dir / "supported_query.json").write_text(json.dumps(sup_query), encoding="utf-8")
    (mile_dir / "supported_result.json").write_text(json.dumps(sup_result), encoding="utf-8")
    (mile_dir / "supported_certificate.json").write_text(json.dumps(sup_cert), encoding="utf-8")
    (mile_dir / "supported_replay.json").write_text(json.dumps(sup_replay), encoding="utf-8")
    (mile_dir / "abstaining_negative_query.json").write_text(json.dumps(abst_query), encoding="utf-8")
    (mile_dir / "abstaining_negative_result.json").write_text(json.dumps(abst_result), encoding="utf-8")
    (mile_dir / "abstaining_negative_certificate.json").write_text(json.dumps(abst_cert), encoding="utf-8")
    (mile_dir / "abstaining_negative_replay.json").write_text(json.dumps(abst_replay), encoding="utf-8")
    return root, snap_dir, mile_dir


class VerifierUITests(unittest.TestCase):
    def test_real_artifacts_load_and_render_with_required_labels_and_disclosure(self):
        vm = load_verifier_view_model(ROOT)
        self.assertEqual(vm["headline"]["numerator_count"], 40)
        self.assertEqual(vm["headline"]["denominator_count"], 571)
        self.assertEqual(vm["variant_counts"], {"p.E746_A750del": 18, "p.L858R": 22})
        self.assertEqual(vm["scope_and_denominator"]["total_primary_tumor_samples"], 1075)
        self.assertEqual(vm["scope_and_denominator"]["primary_tumor_samples_without_open_wxs_maf"], 504)
        self.assertEqual(len(vm["witness_lineage"]), 40)

        rendered = render_verifier_html(vm)
        self.assertIn("40 / 571", rendered)
        self.assertIn("observed selected EGFR calls among file-linked primary-tumor samples", rendered)
        self.assertIn("p.L858R", rendered)
        self.assertIn("22 samples", rendered)
        self.assertIn("p.E746_A750del", rendered)
        self.assertIn("18 samples", rendered)
        self.assertIn("1075", rendered)
        self.assertIn("INCOMPLETE_SNAPSHOT", rendered)
        self.assertIn("Research use only", rendered)
        self.assertIn("development regression", rendered.lower())
        self.assertIn("Project-authored script — not an external audit", rendered)
        self.assertEqual(rendered.count('data-initial-visible="true"'), 5)
        self.assertEqual(rendered.count('data-initial-visible="false"'), 35)
        self.assertNotIn("private_key.pem", rendered)
        self.assertNotIn("BEGIN PRIVATE KEY", rendered)

    def test_counts_and_labels_are_derived_from_artifacts_not_hardcoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, snap_dir, mile_dir = _make_synthetic_ui_workspace(
                Path(tmp), num_total_pt=250, num_wxs=123, num_l858r=4, num_ex19del=3
            )
            vm = load_verifier_view_model(base, snapshot_dir=snap_dir, milestone_dir=mile_dir)
            self.assertEqual(vm["headline"]["numerator_count"], 7)
            self.assertEqual(vm["headline"]["denominator_count"], 123)
            self.assertEqual(vm["variant_counts"], {"p.E746_A750del": 3, "p.L858R": 4})
            self.assertEqual(vm["scope_and_denominator"]["total_primary_tumor_samples"], 250)
            self.assertEqual(vm["scope_and_denominator"]["primary_tumor_samples_without_open_wxs_maf"], 127)

            rendered = render_verifier_html(vm)
            self.assertIn("7 / 123", rendered)
            self.assertIn("4 samples", rendered)
            self.assertIn("3 samples", rendered)
            self.assertIn(">250<", rendered)
            self.assertNotIn("40 / 571", rendered)

    def test_html_injection_is_escaped_and_restricted_paths_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, snap_dir, mile_dir = _make_synthetic_ui_workspace(
                Path(tmp), num_total_pt=10, num_wxs=5, num_l858r=2, num_ex19del=1, inject_xss=True
            )
            vm = load_verifier_view_model(base, snapshot_dir=snap_dir, milestone_dir=mile_dir)
            rendered = render_verifier_html(vm)
            self.assertNotIn('<script>alert("xss")</script>', rendered)
            self.assertNotIn('<img src=x onerror="evil()">', rendered)
            self.assertIn("&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;", rendered)
            self.assertIn("&lt;img src=x onerror=&quot;evil()&quot;&gt;.maf.gz", rendered)

        with self.assertRaises(PermissionError):
            _assert_safe_readonly_path(ROOT / "keys" / "private_key.pem")
        with self.assertRaises(PermissionError):
            _assert_safe_readonly_path(ROOT / "cancer-evidence-exam" / "answer_key.json")

    def test_readonly_http_server_serves_get_and_rejects_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "ui_site"
            server, paths = create_readonly_server(host="127.0.0.1", port=0, output_dir=out_dir, root=ROOT)
            self.assertTrue(paths["html_path"].is_file())
            self.assertTrue(paths["view_model_path"].is_file())
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    body = resp.read().decode("utf-8")
                    self.assertIn("40 / 571", body)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/view-model", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    vm = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(vm["headline"]["numerator_count"], 40)
                req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=b"{}", method="POST")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 405)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
