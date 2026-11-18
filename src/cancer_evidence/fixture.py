from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from .snapshot import write_snapshot


def synthetic_bundle() -> dict[str, Any]:
    entities: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    def add(entity_id: str, kind: str, source: str, source_id: str, **attributes: Any) -> None:
        entities.append({"entity_id": entity_id, "kind": kind, "source": source, "source_id": source_id, "attributes": attributes})
        for field, value in attributes.items():
            evidence.append({
                "evidence_id": f"ev:{entity_id}:{field}",
                "entity_id": entity_id,
                "field_name": field,
                "raw_value": value,
                "canonical_value": value,
                "source_record_id": source_id,
            })

    def link(subject: str, predicate: str, object_id: str, source: str, record: str) -> None:
        relationships.append({"subject_id": subject, "predicate": predicate, "object_id": object_id, "source": source, "source_record_id": record})

    add("gdc:case:C001", "case", "gdc", "C001", project_id="TCGA-LUAD", disease="Lung Adenocarcinoma")
    add("gdc:case:C002", "case", "gdc", "C002", project_id="TCGA-LUAD", disease="Lung Adenocarcinoma")
    add("gdc:sample:S001", "sample", "gdc", "S001", sample_type="Primary Tumor")
    add("gdc:sample:S002", "sample", "gdc", "S002", sample_type="Primary Tumor")
    add("gdc:assay:A001", "assay", "gdc", "A001", assay_type="rna_expression", eligible=True)
    add("gdc:assay:A002", "assay", "gdc", "A002", assay_type="protein_abundance", eligible=True)
    add("gdc:assay:A003", "assay", "gdc", "A003", assay_type="protein_abundance", eligible=True)
    add("gdc:file:F001", "file", "gdc", "F001", data_type="Gene Expression Quantification")
    add("gdc:file:F002", "file", "gdc", "F002", data_type="Protein Quantification")
    add("gdc:file:F003", "file", "gdc", "F003", data_type="Protein Quantification")
    link("gdc:case:C001", "HAS_SAMPLE", "gdc:sample:S001", "gdc", "C001-S001")
    link("gdc:case:C002", "HAS_SAMPLE", "gdc:sample:S002", "gdc", "C002-S002")
    link("gdc:case:C001", "HAS_ASSAY", "gdc:assay:A001", "gdc", "C001-A001")
    link("gdc:case:C001", "HAS_ASSAY", "gdc:assay:A002", "gdc", "C001-A002")
    link("gdc:case:C002", "HAS_ASSAY", "gdc:assay:A003", "gdc", "C002-A003")
    link("gdc:assay:A001", "HAS_FILE", "gdc:file:F001", "gdc", "A001-F001")
    link("gdc:assay:A002", "HAS_FILE", "gdc:file:F002", "gdc", "A002-F002")
    link("gdc:assay:A003", "HAS_FILE", "gdc:file:F003", "gdc", "A003-F003")

    add("pdc:case:P001", "case", "pdc", "P001", study_id="PDC000153", disease="Lung Adenocarcinoma")
    add("pdc:aliquot:AL001", "aliquot", "pdc", "AL001", study_id="PDC000153")
    add("pdc:assay:PA001", "assay", "pdc", "PA001", assay_type="protein_abundance", eligible=True)
    link("pdc:case:P001", "HAS_ALIQUOT", "pdc:aliquot:AL001", "pdc", "P001-AL001")
    link("pdc:case:P001", "HAS_ASSAY", "pdc:assay:PA001", "pdc", "P001-PA001")

    return {
        "source": "synthetic",
        "coverage": {"gdc": {"scope": "TCGA-LUAD fixture", "complete": True}, "pdc": {"scope": "PDC000153 fixture", "complete": True}},
        "entities": entities,
        "relationships": relationships,
        "evidence": evidence,
    }


def create_synthetic_snapshot(output_dir: Path) -> dict[str, Any]:
    if Path(output_dir).exists():
        from .snapshot import read_snapshot
        manifest, connection = read_snapshot(output_dir)
        connection.close()
        expected_raw = (json.dumps(synthetic_bundle(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        from .snapshot import sha256_bytes
        if manifest.get("raw_content_sha256") != sha256_bytes(expected_raw):
            raise ValueError("existing output is not the requested synthetic fixture")
        return manifest
    return write_snapshot(synthetic_bundle(), output_dir, source="synthetic", request={"fixture": "synthetic-v1"}, retrieved_at="2026-01-01T00:00:00+00:00")

