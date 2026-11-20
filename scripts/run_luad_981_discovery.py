from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from cancer_evidence.discovery.cohort_ingest import ingest_luad_981_cohort
from cancer_evidence.discovery.engine import LungDiscoveryEngine
from cancer_evidence.discovery.report_html import write_discovery_html

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAP_DIR = REPO_ROOT / "snapshots" / "discovery" / "luad_981_v1"
REPORT_PATH = REPO_ROOT / "artifacts" / "discovery-report" / "luad_981_discovery.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 981-patient LUAD confounder-aware discovery engine.")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch public cohorts from cBioPortal")
    parser.add_argument("--no-pubmed", action="store_true", help="Skip live PubMed novelty lookup")
    args = parser.parse_args()

    t0 = time.time()
    if args.refresh or not (SNAP_DIR / "manifest.json").exists():
        print("[1/3] Acquiring real 981-patient Lung Adenocarcinoma cohorts (TCGA + OncoSG + CPTAC)...")
        manifest = ingest_luad_981_cohort(SNAP_DIR)
        print(f"      Frozen {manifest['total_patients']} patients -> {SNAP_DIR} (sha={manifest['manifest_sha256'][:16]}...)")
    else:
        print(f"[1/3] Using existing frozen snapshot at {SNAP_DIR}")

    print("[2/3] Running confounder-aware statistical discovery (Cox PH, FDR, Purity, CPTAC Protein, Subtypes, PubMed)...")
    engine = LungDiscoveryEngine(SNAP_DIR)
    report = engine.run_full_discovery(query_pubmed=not args.no_pubmed)

    # Also demonstrate strict zero-hallucination Head-of-Science interrogation + power-floor abstention
    report["sample_interrogations"] = {
        "egfr_wt_stage_iii_iv_cd276": engine.query_subgroup_survival(
            wildtype_gene="EGFR", stage_group="III_IV", split_by_gene_expression="CD276"
        ),
        "underpowered_rare_subgroup_abstention": engine.query_subgroup_survival(
            mutated_gene="RET", stage_group="III_IV", split_by_gene_expression="TACSTD2"
        ),
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path = write_discovery_html(report, REPORT_PATH.parent / "index.html")

    print(f"[3/3] Done in {time.time() - t0:.1f}s -> {REPORT_PATH} | HTML: {html_path}\n")
    print("=" * 84)
    print(f"COHORTS UNIFIED: {report['total_patients']} REAL LUNG ADENOCARCINOMA PATIENTS")
    for k, v in report["cohorts"].items():
        print(f"  {k:12s}: n={v['patients']:3d} | WXS={v['with_wxs']:3d} | RNA={v['with_rna']:3d} | Mass-Spec Protein={v['with_proteomics']:3d}")

    cm = report["comutation_and_ethnic_shift"]
    print("\n[MODULE 1] CO-MUTATION & MUTUAL EXCLUSIVITY (n=" + str(cm["n_sequenced_patients"]) + ")")
    print(f"  Pairs tested: {cm['pairs_tested']} | Naive p<0.05: {cm['naive_p05_count']} | FDR q<0.05: {cm['fdr_q05_count']} (Filtered {cm['false_positives_filtered_by_fdr']} likely false positives)")
    for p in cm["top_fdr_pairs"][:8]:
        print(f"  {p['gene_a']:7s} x {p['gene_b']:7s} | {p['relationship']:18s} | OR={p['odds_ratio']:6.2f} | q={p['q_fmt']} | rep_cohorts={p['replicated_cohorts']}")
    print("  Western (TCGA) vs East Asian (OncoSG) Driver Shifts (FDR q<0.05):")
    for e in cm["western_vs_east_asian_shifts"][:6]:
        print(f"    {e['gene']:7s}: Western={e['western_tcga_pct']:4.1f}% vs East Asian={e['east_asian_oncosg_pct']:4.1f}% (OR={e['odds_ratio_asian_vs_western']}, q={e['fdr_q']:.2e})")

    sv = report["confounder_adjusted_survival"]
    print("\n[MODULE 2] CONFOUNDER-ADJUSTED SURVIVAL (Stage + Age + Sex + Cohort Adjusted Cox PH)")
    print(f"  Tested: {sv['total_biomarkers_tested']} | Naive p<0.05: {sv['naive_unadjusted_p05']} | Adjusted p<0.05: {sv['adjusted_p05']} | FDR q<0.05: {sv['fdr_q05_adjusted_survivors']} | CONFOUNDER KILLS: {sv['confounder_killed_count']}")
    print("  Top Stage-Independent FDR-Surviving Prognostic Signals:")
    for h in sv["top_fdr_survival_hits"][:8]:
        print(
            f"    {h['biomarker']:8s} ({h['modality']:10s}) | adj HR={h['hr_adj']:.2f} ({h['ci_low']:.2f}-{h['ci_high']:.2f}) | "
            f"q={h['fdr_q_adj']:.2e} | both_cohorts_same_dir={h['direction_replicated_both_cohorts']} | boot={h.get('bootstrap_same_dir_pct')}%"
        )
    if sv["top_confounder_kills"]:
        print("  Confounder Kills (Looked Significant Unadjusted, DIED After Stage/Age/Sex/Cohort Control!):")
        for k in sv["top_confounder_kills"][:5]:
            print(f"    KILLED: {k['biomarker']:8s} ({k['modality']:10s}) | unadj p={k['p_unadj']:.4f} (HR={k['hr_unadj']}) -> adj p={k['p_adj']:.4f} (HR={k['hr_adj']})")

    ga = report["genotype_to_adc_and_payload_reprogramming"]
    print("\n[MODULE 3] GENOTYPE -> ADC SURFACE TARGET & PAYLOAD-SENSITIVITY REPROGRAMMING")
    print(f"  Tested: {ga['total_pairs_tested']} | RNA FDR q<0.05: {ga['fdr_q05_rna_hits']} | GOLD (FDR + Purity-Adjusted + CPTAC Mass-Spec Protein Replicated): {ga['gold_purity_and_protein_replicated_hits']}")
    for g in ga["gold_trans_discoveries"][:10]:
        print(
            f"    {g['driver_mutation']:7s} mut -> {g['target_gene']:8s} {g['direction']:13s} | "
            f"RNA beta={g['beta_rna_z']:+.2f} (q={g['fdr_q_rna']:.1e}) | Purity-adj p={g['purity_adj_p']:.4f} | "
            f"CPTAC PROTEIN beta={g['cptac_protein_beta_z']:+.2f} (p={g['cptac_protein_p']:.4f})"
        )

    ep = report["epistatic_survival_interactions"]
    print("\n[MODULE 4] TWO-WAY EPISTATIC SURVIVAL INTERACTIONS (A x B Synergy)")
    print(f"  Well-powered pairs tested (>=15 double mutants): {ep['pairs_tested_with_min_15_double_mutants']} | Nominal p<0.05: {ep['nominal_p05_interactions']} | FDR q<0.05: {ep['fdr_q05_interactions']}")
    for ix in ep["top_interactions"][:5]:
        print(f"    {ix['gene_a']:7s} x {ix['gene_b']:7s} (n_both={ix['n_double_mutant']:2d}) | int HR={ix['interaction_hr']} ({ix['ci_low']}-{ix['ci_high']}) | p={ix['p_interaction']:.4f} | q={ix['fdr_q']:.3f}")

    st = report["unsupervised_subtypes"]
    print(f"\n[MODULE 5] UNSUPERVISED MULTI-COHORT SUBTYPE DISCOVERY (n={st['n_rna_patients_clustered']}, k={st['optimal_k']}, sil={st['silhouette_score']}, logrank p={st['overall_logrank_p']:.2e})")
    for s in st["subtypes"]:
        print(
            f"    {s['cluster_id']}: n={s['n_patients']:3d} | cohorts={s['cohort_mix']} | adj HR={s['stage_cohort_adj_hr']} (p={s['stage_cohort_adj_p']}) | "
            f"top_up={[x[0] for x in s['top_upregulated_genes_z'][:4]]} | muts={s['key_mutation_rates_pct']}"
        )

    if report["pubmed_novelty_grounding"]:
        print("\n[MODULE 6] LIVE PUBMED NOVELTY GROUNDING (Known Sanity Checks vs Under-Explored Signals)")
        for pm in report["pubmed_novelty_grounding"]:
            print(f"    [{pm['novelty_tier']:24s} | papers={str(pm['pubmed_papers']):>5s}] {pm['finding']}")

    print("\n[MODULE 7] ZERO-HALLUCINATION INTERROGATION CHECK")
    for name, ans in report["sample_interrogations"].items():
        print(f"    {name}: {json.dumps(ans)}")


if __name__ == "__main__":
    main()
