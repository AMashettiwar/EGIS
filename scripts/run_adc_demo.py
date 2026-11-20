from __future__ import annotations

import json
from pathlib import Path

from cancer_evidence.multimodal import (
    AlignmentPolicy,
    CohortRequest,
    MultimodalSnapshot,
    evaluate_cohort,
)
from cancer_evidence.multimodal.adc import (
    DEFAULT_LUNG_ADC_RULES,
    evaluate_adc_target_cohort,
    evaluate_dual_target_adc,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = REPO_ROOT / "snapshots" / "multimodal" / "cptac3_lung_v1"
OUT_PATH = REPO_ROOT / "artifacts" / "multimodal-demo" / "cptac3_lung_v1" / "adc_stratification_report.json"


def main() -> None:
    with MultimodalSnapshot(SNAPSHOT_DIR) as snap:
        req = CohortRequest(
            cohort_id="cptac3-luad-4mod-adc-addon",
            required_modalities=("clinical", "rna", "somatic_mutations", "proteomics"),
            alignment_policy=AlignmentPolicy(
                required_sample_type="Primary Tumor",
                max_day_delta=30,
                require_same_episode=True,
                allow_missing_timestamps=False,
                allow_shared_specimen_fallback=True,
            ),
            disease_types=("Adenomas and Adenocarcinomas",),
        )
        cohort = evaluate_cohort(snap, req)

    rules_by_gene = {r.target_gene: r for r in DEFAULT_LUNG_ADC_RULES}
    single_target_reports = [evaluate_adc_target_cohort(cohort, r).to_dict() for r in DEFAULT_LUNG_ADC_RULES]
    dual_target_reports = [
        evaluate_dual_target_adc(cohort, rules_by_gene["EGFR"], rules_by_gene["MET"]),
        evaluate_dual_target_adc(cohort, rules_by_gene["EGFR"], rules_by_gene["ERBB3"]),
        evaluate_dual_target_adc(cohort, rules_by_gene["TACSTD2"], rules_by_gene["ERBB2"]),
        evaluate_dual_target_adc(cohort, rules_by_gene["TACSTD2"], rules_by_gene["CD276"]),
    ]

    report = {
        "snapshot_id": cohort.snapshot_id,
        "snapshot_manifest_sha256": cohort.snapshot_manifest_sha256,
        "denominator_ready_luad_patients": cohort.ready_count,
        "single_target_stratification": single_target_reports,
        "dual_target_coexpression_bulk_protein": dual_target_reports,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"=== ADC TARGET ADD-ON REPORT (Real CPTAC-3 LUAD, n={cohort.ready_count} READY patients) ===")
    for row in single_target_reports:
        r = row["rule"]
        print(
            f"  {r['common_name']:6s} ({r['target_gene']:7s}) | "
            f"Prot+: {row['protein_positive_count']:2d}/{row['evaluable_paired_patients']} ({row['protein_positive_pct']:4.1f}%) | "
            f"RNA-hi/Prot-lo: {row['discordant_rna_high_prot_low']:2d} | "
            f"RNA-lo/Prot-hi: {row['discordant_rna_low_prot_high']:2d}"
        )
    print("\n=== BISPECIFIC / DUAL-TARGET CO-EXPRESSION (Measured Protein >= +0.5 log2) ===")
    for d in dual_target_reports:
        print(
            f"  {d['pair']:14s} | Dual+: {d['dual_positive_count']:2d}/{d['evaluable_patients']} ({d['dual_positive_pct']:4.1f}%)"
        )
    print(f"\nSaved -> {OUT_PATH}")


if __name__ == "__main__":
    main()
