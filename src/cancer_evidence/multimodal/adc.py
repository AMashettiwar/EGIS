from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import json
from typing import Any

from .adapters import AdapterPayload
from .models import CohortResult, MappingStatus, PacketStatus, QCStatus, ReportStatus


@dataclass(frozen=True)
class AdcTargetRule:
    """Explicit, locked cutoff rule for evaluating an ADC target across RNA & Protein."""

    rule_id: str
    target_gene: str
    common_name: str
    protein_log2_cutoff: float
    rna_tpm_cutoff: float


DEFAULT_LUNG_ADC_RULES: tuple[AdcTargetRule, ...] = (
    AdcTargetRule("LUNG-ADC-TROP2-V1", "TACSTD2", "TROP2", protein_log2_cutoff=0.5, rna_tpm_cutoff=50.0),
    AdcTargetRule("LUNG-ADC-HER2-V1", "ERBB2", "HER2", protein_log2_cutoff=0.5, rna_tpm_cutoff=50.0),
    AdcTargetRule("LUNG-ADC-MET-V1", "MET", "c-MET", protein_log2_cutoff=0.5, rna_tpm_cutoff=50.0),
    AdcTargetRule("LUNG-ADC-HER3-V1", "ERBB3", "HER3", protein_log2_cutoff=0.5, rna_tpm_cutoff=30.0),
    AdcTargetRule("LUNG-ADC-EGFR-V1", "EGFR", "EGFR", protein_log2_cutoff=0.5, rna_tpm_cutoff=30.0),
    AdcTargetRule("LUNG-ADC-B7H3-V1", "CD276", "B7-H3", protein_log2_cutoff=0.5, rna_tpm_cutoff=25.0),
)


@dataclass(frozen=True)
class AdcTargetSummary:
    rule: AdcTargetRule
    denominator_ready_patients: int
    evaluable_paired_patients: int
    missing_protein_or_rna: int
    protein_positive_count: int
    rna_positive_count: int
    concordant_pos: int
    concordant_neg: int
    discordant_rna_high_prot_low: int
    discordant_rna_low_prot_high: int
    positive_patient_ids: tuple[str, ...]
    discordant_patient_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["protein_positive_pct"] = (
            round(100.0 * self.protein_positive_count / self.evaluable_paired_patients, 1)
            if self.evaluable_paired_patients > 0
            else None
        )
        d["total_discordant"] = self.discordant_rna_high_prot_low + self.discordant_rna_low_prot_high
        d["discordant_pct"] = (
            round(100.0 * d["total_discordant"] / self.evaluable_paired_patients, 1)
            if self.evaluable_paired_patients > 0
            else None
        )
        return d


def evaluate_adc_target_cohort(result: CohortResult, rule: AdcTargetRule) -> AdcTargetSummary:
    """Deterministically stratify READY multimodal patients under a locked ADC target rule."""
    ready = [p for p in result.packets if p.status == PacketStatus.READY]
    evaluable = 0
    missing = 0
    counts: Counter[str] = Counter()
    pos_ids: list[str] = []
    disc_ids: list[str] = []

    for pkt in ready:
        rna_mod = pkt.modalities.get("rna")
        prot_mod = pkt.modalities.get("proteomics")
        tpm = (rna_mod.features.get("tpm") or {}).get(rule.target_gene) if rna_mod else None
        prot = (prot_mod.features.get("protein_log2_ratio") or {}).get(rule.target_gene) if prot_mod else None

        if tpm is None or prot is None:
            missing += 1
            continue

        evaluable += 1
        prot_pos = float(prot) >= rule.protein_log2_cutoff
        rna_pos = float(tpm) >= rule.rna_tpm_cutoff

        if prot_pos:
            counts["prot_pos"] += 1
            pos_ids.append(pkt.canonical_patient_id)
        if rna_pos:
            counts["rna_pos"] += 1

        if prot_pos and rna_pos:
            counts["conc_pos"] += 1
        elif not prot_pos and not rna_pos:
            counts["conc_neg"] += 1
        elif rna_pos and not prot_pos:
            counts["disc_rna_high_prot_low"] += 1
            disc_ids.append(pkt.canonical_patient_id)
        else:
            counts["disc_rna_low_prot_high"] += 1
            disc_ids.append(pkt.canonical_patient_id)

    return AdcTargetSummary(
        rule=rule,
        denominator_ready_patients=len(ready),
        evaluable_paired_patients=evaluable,
        missing_protein_or_rna=missing,
        protein_positive_count=counts["prot_pos"],
        rna_positive_count=counts["rna_pos"],
        concordant_pos=counts["conc_pos"],
        concordant_neg=counts["conc_neg"],
        discordant_rna_high_prot_low=counts["disc_rna_high_prot_low"],
        discordant_rna_low_prot_high=counts["disc_rna_low_prot_high"],
        positive_patient_ids=tuple(sorted(pos_ids)),
        discordant_patient_ids=tuple(sorted(disc_ids)),
    )


def evaluate_dual_target_adc(
    result: CohortResult,
    rule_a: AdcTargetRule,
    rule_b: AdcTargetRule,
) -> dict[str, Any]:
    """Evaluate bispecific / dual-target ADC protein co-positivity on READY patients."""
    ready = [p for p in result.packets if p.status == PacketStatus.READY]
    evaluable = 0
    both_pos: list[str] = []
    only_a: list[str] = []
    only_b: list[str] = []
    neither: list[str] = []

    for pkt in ready:
        prot_mod = pkt.modalities.get("proteomics")
        prots = (prot_mod.features.get("protein_log2_ratio") or {}) if prot_mod else {}
        va = prots.get(rule_a.target_gene)
        vb = prots.get(rule_b.target_gene)
        if va is None or vb is None:
            continue
        evaluable += 1
        pa = float(va) >= rule_a.protein_log2_cutoff
        pb = float(vb) >= rule_b.protein_log2_cutoff
        if pa and pb:
            both_pos.append(pkt.canonical_patient_id)
        elif pa:
            only_a.append(pkt.canonical_patient_id)
        elif pb:
            only_b.append(pkt.canonical_patient_id)
        else:
            neither.append(pkt.canonical_patient_id)

    return {
        "pair": f"{rule_a.common_name}+{rule_b.common_name}",
        "genes": [rule_a.target_gene, rule_b.target_gene],
        "cutoffs_log2": [rule_a.protein_log2_cutoff, rule_b.protein_log2_cutoff],
        "evaluable_patients": evaluable,
        "dual_positive_count": len(both_pos),
        "dual_positive_pct": round(100.0 * len(both_pos) / evaluable, 1) if evaluable else None,
        "only_a_count": len(only_a),
        "only_b_count": len(only_b),
        "neither_count": len(neither),
        "dual_positive_patient_ids": sorted(both_pos),
    }


@dataclass(frozen=True)
class SingleCellMeasurement:
    """One live CTC cell-level surface-antigen measurement (plugs into EGIS with zero engine edits)."""

    canonical_patient_id: str
    source_specimen_id: str
    cell_id: str
    qc_pass: bool
    surface_protein_intensity: dict[str, float]
    episode_id: str | None = None
    collection_day_offset: int | None = None
    sample_type: str = "Primary Tumor"


class SingleCellCtcAdcAdapter:
    """Pluggable single-cell CTC surface-antigen adapter with strict 3-way evaluability.

    Enforces:
      - captured QC-passing CTCs < min_evaluable_ctcs -> QC_FAILED (UNEVALUABLE / UNKNOWN),
        NEVER falsely reported as antigen-negative.
      - Computes cell-level positive fraction AND true same-cell dual-target co-expression.
    """

    modality_name = "ctc_surface_protein"
    source_name = "ctc_single_cell"

    def ingest(
        self,
        *,
        cells: list[SingleCellMeasurement],
        target_cutoffs: dict[str, float],
        dual_pairs: tuple[tuple[str, str], ...] = (("TACSTD2", "ERBB2"), ("EGFR", "MET")),
        min_evaluable_ctcs: int = 5,
        patient_positive_cell_fraction_cutoff: float = 0.25,
    ) -> AdapterPayload:
        by_specimen: dict[tuple[str, str], list[SingleCellMeasurement]] = defaultdict(list)
        for cell in cells:
            by_specimen[(cell.canonical_patient_id, cell.source_specimen_id)].append(cell)

        crosswalk: list[dict[str, Any]] = []
        seen_pids: set[str] = set()
        runs: list[dict[str, Any]] = []
        cell_rows: list[dict[str, Any]] = []

        for (pid, spec_id), spec_cells in sorted(by_specimen.items()):
            if pid not in seen_pids:
                seen_pids.add(pid)
                crosswalk.append({
                    "canonical_patient_id": pid,
                    "source": self.source_name,
                    "source_patient_id": pid,
                    "mapping_status": MappingStatus.APPROVED.value,
                    "authority": "CTC-PANEL",
                    "evidence_note": f"Single-cell CTC surface-protein linkage for {pid}",
                })

            valid_cells = [c for c in spec_cells if c.qc_pass]
            evaluable = len(valid_cells) >= min_evaluable_ctcs
            first = spec_cells[0]

            target_calls: dict[str, dict[str, Any]] = {}
            dual_calls: dict[str, dict[str, Any]] = {}

            if evaluable:
                for gene, cutoff in sorted(target_cutoffs.items()):
                    vals = [
                        float(c.surface_protein_intensity[gene])
                        for c in valid_cells
                        if gene in c.surface_protein_intensity
                    ]
                    if len(vals) < min_evaluable_ctcs:
                        target_calls[gene] = {"status": "UNEVALUABLE", "evaluable_cells": len(vals)}
                        continue
                    pos_cells = sum(1 for v in vals if v >= cutoff)
                    pos_frac = round(pos_cells / len(vals), 4)
                    sorted_vals = sorted(vals)
                    med = sorted_vals[len(sorted_vals) // 2]
                    target_calls[gene] = {
                        "status": (
                            "POSITIVE"
                            if pos_frac >= patient_positive_cell_fraction_cutoff
                            else "CONFIRMED_NEGATIVE"
                        ),
                        "evaluable_cells": len(vals),
                        "positive_cells": pos_cells,
                        "positive_cell_fraction": pos_frac,
                        "median_intensity": round(med, 3),
                        "cell_cutoff": cutoff,
                    }

                for g_a, g_b in dual_pairs:
                    if g_a not in target_cutoffs or g_b not in target_cutoffs:
                        continue
                    paired_cells = [
                        c
                        for c in valid_cells
                        if g_a in c.surface_protein_intensity and g_b in c.surface_protein_intensity
                    ]
                    if len(paired_cells) < min_evaluable_ctcs:
                        continue
                    co_pos = sum(
                        1
                        for c in paired_cells
                        if float(c.surface_protein_intensity[g_a]) >= target_cutoffs[g_a]
                        and float(c.surface_protein_intensity[g_b]) >= target_cutoffs[g_b]
                    )
                    dual_calls[f"{g_a}+{g_b}"] = {
                        "same_cell_coexpressing_cells": co_pos,
                        "evaluable_cells": len(paired_cells),
                        "same_cell_coexpression_fraction": round(co_pos / len(paired_cells), 4),
                    }

            runs.append({
                "run_id": f"ctc:surface:{pid}:{spec_id}",
                "modality": self.modality_name,
                "canonical_patient_id": pid,
                "specimen_id": f"ctc:{spec_id}",
                "source_specimen_id": spec_id,
                "episode_id": first.episode_id,
                "source": self.source_name,
                "source_run_id": f"{pid}:{spec_id}",
                "qc_status": QCStatus.PASS.value if evaluable else QCStatus.FAIL.value,
                "report_status": ReportStatus.ACTIVE_FINAL.value,
                "sample_type": first.sample_type,
                "collection_day_offset": first.collection_day_offset,
                "pipeline_version": "ctc-adc-evaluability-v1",
                "artifact_uri": f"ctc://specimen/{spec_id}",
                "artifact_sha256": None,
                "features_json": json.dumps(
                    {
                        "total_captured_ctcs": len(spec_cells),
                        "qc_passing_ctcs": len(valid_cells),
                        "min_evaluable_ctcs": min_evaluable_ctcs,
                        "evaluability_status": "EVALUABLE" if evaluable else "UNEVALUABLE_LOW_CTC_COUNT",
                        "target_calls": target_calls,
                        "same_cell_dual_coexpression": dual_calls,
                    },
                    sort_keys=True,
                ),
            })

            for c in spec_cells:
                cell_rows.append({
                    "canonical_patient_id": pid,
                    "source_specimen_id": spec_id,
                    "cell_id": c.cell_id,
                    "qc_pass": c.qc_pass,
                    "surface_protein_json": json.dumps(c.surface_protein_intensity, sort_keys=True),
                })

        return AdapterPayload(
            modality_name=self.modality_name,
            source_name=self.source_name,
            identity_crosswalk=crosswalk,
            modality_runs=runs,
            feature_tables={"ctc_surface_cells": cell_rows},
            source_provenance={
                "source": "Single-Cell CTC Surface Protein Adapter",
                "min_evaluable_ctcs": min_evaluable_ctcs,
                "specimens_ingested": len(by_specimen),
            },
        )
