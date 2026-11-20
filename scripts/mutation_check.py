from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.fixture import synthetic_bundle
from cancer_evidence.replay import replay_certificate
from cancer_evidence.snapshot import write_snapshot


ENGINE_SOURCE = ROOT / "src" / "cancer_evidence" / "engine.py"

MUTATIONS = {
    "wrong_denominator_basis": (
        'denominator = len(left) if denominator_mode == "left" else len(population)',
        "denominator = len(left)",
    ),
    "set_vs_list_overlap": (
        "overlap = sorted(numerator_cases)",
        "overlap = sorted(left | right)",
    ),
    "skipped_overlap_conflict_check": (
        "if _conflict_entities(connection, (left | right) | assay_entities):",
        "if False and _conflict_entities(connection, (left | right) | assay_entities):",
    ),
    "missing_availability_conflict_check": (
        "if _conflict_entities(connection, candidate_assays):",
        "if False and _conflict_entities(connection, candidate_assays):",
    ),
    "cohort_wrong_numerator_plus_one": (
        '"numerator_count": len(positive_patient_ids),',
        '"numerator_count": len(positive_patient_ids) + 1,',
    ),
    "cohort_dropped_positive_witness": (
        "supporting_evidence = sorted(set(positive_patient_ids) | set(positive_assay_ids))",
        "supporting_evidence = sorted(positive_patient_ids[:1])",
    ),
    "claim_ignored_contradiction": (
        "if observed == expected_result:",
        "if observed == expected_result or True:",
    ),
    "cross_source_allowed_draft_mapping": (
        'approved = [m for m in mappings if m.get("approval_status") == "APPROVED"]',
        'approved = [m for m in mappings if m.get("approval_status") in {"APPROVED", "DRAFT_UNAPPROVED"}]',
    ),
    "molecular_dropped_variant_witness": (
        'pos_sample_ids = sorted(entities[s]["source_id"] for s in matching_samples)',
        'pos_sample_ids = sorted(entities[s]["source_id"] for s in matching_samples)[:1]',
    ),
}


def _rich_cohort_bundle() -> dict:
    entities = [
        {"entity_id": "SYN:patient:P1", "kind": "patient", "source": "SYN", "source_id": "P1", "attributes": {}},
        {"entity_id": "SYN:patient:P2", "kind": "patient", "source": "SYN", "source_id": "P2", "attributes": {}},
        {"entity_id": "SYN:case:C1", "kind": "case", "source": "SYN", "source_id": "C1", "attributes": {"histology": "LUAD", "stage": "STAGE_IV"}},
        {"entity_id": "SYN:case:C2", "kind": "case", "source": "SYN", "source_id": "C2", "attributes": {"histology": "LUAD", "stage": "STAGE_IV"}},
        {"entity_id": "SYN:assay:A1", "kind": "assay", "source": "SYN", "source_id": "A1", "attributes": {"patient_id": "P1", "biomarker": "EGFR", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "POSITIVE"}},
        {"entity_id": "SYN:assay:A2", "kind": "assay", "source": "SYN", "source_id": "A2", "attributes": {"patient_id": "P2", "biomarker": "EGFR", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "WT_NEGATIVE"}},
    ]
    relationships = [
        {"subject_id": "SYN:patient:P1", "predicate": "HAS_CASE", "object_id": "SYN:case:C1", "source": "SYN", "source_record_id": "P1-C1"},
        {"subject_id": "SYN:patient:P2", "predicate": "HAS_CASE", "object_id": "SYN:case:C2", "source": "SYN", "source_record_id": "P2-C2"},
        {"subject_id": "SYN:case:C1", "predicate": "HAS_ASSAY", "object_id": "SYN:assay:A1", "source": "SYN", "source_record_id": "C1-A1"},
        {"subject_id": "SYN:case:C2", "predicate": "HAS_ASSAY", "object_id": "SYN:assay:A2", "source": "SYN", "source_record_id": "C2-A2"},
    ]
    return {
        "source": "SYN",
        "coverage": {"SYN": {"scope": "mutation-check", "complete": True, "expected_total": 2, "retrieved_count": 2}},
        "entities": entities,
        "relationships": relationships,
        "evidence": [],
    }


def _draft_mapping_bundle() -> dict:
    return {
        "source": "MULTI",
        "coverage": {
            "SRC_A": {"scope": "a", "complete": True, "expected_total": 1, "retrieved_count": 1},
            "SRC_B": {"scope": "b", "complete": True, "expected_total": 1, "retrieved_count": 1},
        },
        "entities": [
            {"entity_id": "SRC_A:patient:PA1", "kind": "patient", "source": "SRC_A", "source_id": "PA1", "attributes": {}},
            {"entity_id": "SRC_A:case:CA1", "kind": "case", "source": "SRC_A", "source_id": "CA1", "attributes": {}},
            {"entity_id": "SRC_B:patient:PB1", "kind": "patient", "source": "SRC_B", "source_id": "PB1", "attributes": {}},
            {"entity_id": "SRC_B:case:CB1", "kind": "case", "source": "SRC_B", "source_id": "CB1", "attributes": {}},
        ],
        "relationships": [
            {"subject_id": "SRC_A:patient:PA1", "predicate": "HAS_CASE", "object_id": "SRC_A:case:CA1", "source": "SRC_A", "source_record_id": "PA1-CA1"},
            {"subject_id": "SRC_B:patient:PB1", "predicate": "HAS_CASE", "object_id": "SRC_B:case:CB1", "source": "SRC_B", "source_record_id": "PB1-CB1"},
        ],
        "evidence": [],
        "identity_mappings": [
            {
                "mapping_id": "M-DRAFT-1",
                "source_a": "SRC_A",
                "source_b": "SRC_B",
                "source_a_id": "PA1",
                "source_b_id": "PB1",
                "approval_status": "DRAFT_UNAPPROVED",
                "authority": "IRB",
                "cardinality": "ONE_TO_ONE_EXCLUSIVE",
            }
        ],
    }


def _molecular_bundle() -> dict:
    return {
        "source": "SYN",
        "cohort_id": "mol_mut_v1",
        "coverage": {"SYN": {"scope": "mutation-check", "complete": True, "expected_total": 2, "retrieved_count": 2}},
        "entities": [
            {"entity_id": "SYN:case:C1", "kind": "case", "source": "SYN", "source_id": "C1", "attributes": {"project_id": "TCGA-LUAD"}},
            {"entity_id": "SYN:case:C2", "kind": "case", "source": "SYN", "source_id": "C2", "attributes": {"project_id": "TCGA-LUAD"}},
            {"entity_id": "SYN:sample:S1", "kind": "sample", "source": "SYN", "source_id": "S1", "attributes": {"sample_type": "Primary Tumor"}},
            {"entity_id": "SYN:sample:S2", "kind": "sample", "source": "SYN", "source_id": "S2", "attributes": {"sample_type": "Primary Tumor"}},
            {"entity_id": "SYN:aliquot:ALQ1", "kind": "aliquot", "source": "SYN", "source_id": "ALQ1", "attributes": {}},
            {"entity_id": "SYN:aliquot:ALQ2", "kind": "aliquot", "source": "SYN", "source_id": "ALQ2", "attributes": {}},
            {"entity_id": "SYN:assay:OCC1", "kind": "assay", "source": "SYN", "source_id": "OCC1", "attributes": {"gene_symbol": "EGFR", "hgvsp_short": "p.L858R", "call_status": "POSITIVE", "linkage_status": "UNIQUE_MATCH"}},
            {"entity_id": "SYN:assay:OCC2", "kind": "assay", "source": "SYN", "source_id": "OCC2", "attributes": {"gene_symbol": "EGFR", "hgvsp_short": "p.L858R", "call_status": "POSITIVE", "linkage_status": "UNIQUE_MATCH"}},
        ],
        "relationships": [
            {"subject_id": "SYN:case:C1", "predicate": "HAS_SAMPLE", "object_id": "SYN:sample:S1", "source": "SYN", "source_record_id": "C1-S1"},
            {"subject_id": "SYN:case:C2", "predicate": "HAS_SAMPLE", "object_id": "SYN:sample:S2", "source": "SYN", "source_record_id": "C2-S2"},
            {"subject_id": "SYN:sample:S1", "predicate": "HAS_ALIQUOT", "object_id": "SYN:aliquot:ALQ1", "source": "SYN", "source_record_id": "S1-ALQ1"},
            {"subject_id": "SYN:sample:S2", "predicate": "HAS_ALIQUOT", "object_id": "SYN:aliquot:ALQ2", "source": "SYN", "source_record_id": "S2-ALQ2"},
            {"subject_id": "SYN:aliquot:ALQ1", "predicate": "HAS_ASSAY", "object_id": "SYN:assay:OCC1", "source": "SYN", "source_record_id": "ALQ1-OCC1"},
            {"subject_id": "SYN:aliquot:ALQ2", "predicate": "HAS_ASSAY", "object_id": "SYN:assay:OCC2", "source": "SYN", "source_record_id": "ALQ2-OCC2"},
        ],
        "evidence": [],
        "provenance_metadata": {
            "named_universes": {"mol_primary_samples": "Primary tumor samples"},
            "molecular_coverage": {"acquired_gene_symbols": ["EGFR"], "explicit_negative_calls_present": False, "locus_level_callable_coverage_present": False},
        },
    }


def _load_mutant(name: str, replacement: tuple[str, str], directory: Path):
    source = ENGINE_SOURCE.read_text(encoding="utf-8")
    old, new = replacement
    if old not in source:
        raise AssertionError(f"mutation target not found: {name}")
    mutant_path = directory / f"{name}.py"
    mutant_path.write_text(source.replace(old, new, 1), encoding="utf-8")
    module_name = f"cancer_evidence._mutant_{name}"
    spec = importlib.util.spec_from_file_location(module_name, mutant_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def run_mutations() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        clean_bundle = synthetic_bundle()
        conflict_bundle = copy.deepcopy(clean_bundle)
        conflict_bundle["evidence"].append({"evidence_id": "ev:mutation-conflict", "entity_id": "gdc:assay:A001", "field_name": "eligible", "raw_value": False, "canonical_value": False, "source_record_id": "mutation-conflict"})
        clean_snapshot = root / "clean"
        conflict_snapshot = root / "conflict"
        cohort_snapshot = root / "cohort"
        draft_snapshot = root / "draft"
        molecular_snapshot = root / "molecular"
        write_snapshot(clean_bundle, clean_snapshot)
        write_snapshot(conflict_bundle, conflict_snapshot)
        write_snapshot(_rich_cohort_bundle(), cohort_snapshot)
        write_snapshot(_draft_mapping_bundle(), draft_snapshot)
        write_snapshot(_molecular_bundle(), molecular_snapshot)

        eligible_query = {"operation": "assay_overlap", "left": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}, "right": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "protein_abundance"}, "cohort_filter": {"source": "gdc", "project_id": "TCGA-LUAD"}, "denominator_mode": "eligible"}
        overlap_conflict_query = {"operation": "assay_overlap", "left": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}, "right": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "protein_abundance"}}
        availability_query = {"operation": "assay_availability", "selector": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}}
        cohort_query = {"operation": "cohort_count", "entity_level": "patient", "denominator_basis": "eligible_luad", "predicate": {"histology": "LUAD", "biomarker": "EGFR", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "POSITIVE"}}
        claim_query = {"operation": "claim_verification", "subject_ref": {"patient_id": "P2"}, "predicate": {"biomarker": "EGFR", "expected_result": "POSITIVE"}}
        cross_query = {"operation": "cross_source_overlap_count", "entity_level": "patient", "denominator_basis": "union", "requested_sources": ["SRC_A", "SRC_B"]}
        mol_query = {"operation": "cohort_count", "entity_level": "sample", "denominator_basis": "mol_primary_samples", "predicate": {"source": "SYN", "gene_symbol": "EGFR", "hgvsp_short": "p.L858R", "call_status": "POSITIVE"}}

        reports = {}
        with tempfile.TemporaryDirectory() as mutation_directory:
            for name, replacement in MUTATIONS.items():
                module = _load_mutant(name, replacement, Path(mutation_directory))
                if name.startswith("cohort_"):
                    snapshot, query = cohort_snapshot, cohort_query
                elif name.startswith("claim_"):
                    snapshot, query = cohort_snapshot, claim_query
                elif name.startswith("cross_source_"):
                    snapshot, query = draft_snapshot, cross_query
                elif name.startswith("molecular_"):
                    snapshot, query = molecular_snapshot, mol_query
                elif "availability" in name:
                    snapshot, query = conflict_snapshot, availability_query
                elif "conflict" in name:
                    snapshot, query = conflict_snapshot, overlap_conflict_query
                else:
                    snapshot, query = clean_snapshot, eligible_query
                certificate_path = root / f"{name}.json"
                with patch("cancer_evidence.certificate.evaluate", module.evaluate):
                    issue_certificate(snapshot, query, certificate_path)
                replay = replay_certificate(snapshot, certificate_path)
                reports[name] = {"caught": not replay.get("valid", False), "replay": replay}
        killed = sum(1 for report in reports.values() if report["caught"])
        return {"mutations": reports, "killed": killed, "total": len(reports), "kill_rate": killed / len(reports)}


if __name__ == "__main__":
    print(json.dumps(run_mutations(), indent=2, sort_keys=True))
