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
    "wrong_denominator_basis": ('denominator = len(left) if denominator_mode == "left" else len(population)', 'denominator = len(left)'),
    "set_vs_list_overlap": ("overlap = sorted(numerator_cases)", "overlap = sorted(left | right)"),
    "skipped_overlap_conflict_check": ("if _conflict_entities(connection, (left | right) | assay_entities):", "if False and _conflict_entities(connection, (left | right) | assay_entities):"),
    "missing_availability_conflict_check": ("if _conflict_entities(connection, candidate_assays):", "if False and _conflict_entities(connection, candidate_assays):"),
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
        write_snapshot(clean_bundle, clean_snapshot)
        write_snapshot(conflict_bundle, conflict_snapshot)
        eligible_query = {"operation": "assay_overlap", "left": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}, "right": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "protein_abundance"}, "cohort_filter": {"source": "gdc", "project_id": "TCGA-LUAD"}, "denominator_mode": "eligible"}
        overlap_conflict_query = {"operation": "assay_overlap", "left": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}, "right": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "protein_abundance"}}
        availability_query = {"operation": "assay_availability", "selector": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}}
        reports = {}
        with tempfile.TemporaryDirectory() as mutation_directory:
            for name, replacement in MUTATIONS.items():
                module = _load_mutant(name, replacement, Path(mutation_directory))
                snapshot = conflict_snapshot if "conflict" in name or "availability" in name else clean_snapshot
                query = availability_query if "availability" in name else (overlap_conflict_query if "conflict" in name else eligible_query)
                certificate_path = root / f"{name}.json"
                with patch("cancer_evidence.certificate.evaluate", module.evaluate):
                    issue_certificate(snapshot, query, certificate_path)
                replay = replay_certificate(snapshot, certificate_path)
                reports[name] = {"caught": not replay.get("valid", False), "replay": replay}
        killed = sum(1 for report in reports.values() if report["caught"])
        return {"mutations": reports, "killed": killed, "total": len(reports), "kill_rate": killed / len(reports)}


if __name__ == "__main__":
    print(json.dumps(run_mutations(), indent=2, sort_keys=True))
