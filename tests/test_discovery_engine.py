from __future__ import annotations

from pathlib import Path

import pytest

from cancer_evidence.discovery.engine import LungDiscoveryEngine

SNAP_DIR = Path(__file__).resolve().parents[1] / "snapshots" / "discovery" / "luad_981_v1"


@pytest.mark.skipif(
    not (SNAP_DIR / "patients.parquet").exists(),
    reason="Local 981-patient LUAD Parquet snapshot not present (run scripts/run_luad_981_discovery.py)",
)
def test_real_luad_981_discovery_recovers_ground_truth_and_abstains_underpowered() -> None:
    eng = LungDiscoveryEngine(SNAP_DIR)
    assert len(eng.patients) >= 900

    # Module 1 must recover canonical EGFR <-> KRAS mutual exclusivity across cohorts
    cm = eng.run_comutation_scan()
    egfr_kras = [
        p for p in cm["top_fdr_pairs"]
        if {p["gene_a"], p["gene_b"]} == {"EGFR", "KRAS"}
    ]
    assert egfr_kras, "Expected EGFR x KRAS mutual exclusivity at FDR < 0.05"
    assert egfr_kras[0]["relationship"] == "MUTUALLY_EXCLUSIVE"
    assert egfr_kras[0]["odds_ratio"] < 0.25

    # Module 2 must run multivariable Cox PH and catch confounder-killed signals
    sv = eng.run_survival_discovery(bootstrap_iters=20)
    assert sv["fdr_q05_adjusted_survivors"] >= 1

    # Module 3 must find FDR-surviving genotype -> ADC/payload effects replicated at CPTAC protein level
    ga = eng.run_genotype_to_adc_protein_scan()
    assert ga["gold_purity_and_protein_replicated_hits"] >= 1

    # Module 7 interrogation tool must abstain when subgroup has < 15 patients
    abstain = eng.query_subgroup_survival(mutated_gene="RET", stage_group="III_IV")
    assert abstain["status"] == "INSUFFICIENT_POWER"
