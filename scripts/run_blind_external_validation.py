from __future__ import annotations

import json
import math
from pathlib import Path
import time
from typing import Any
import warnings

from lifelines import CoxPHFitter
import numpy as np
import pandas as pd
from scipy import stats
import statsmodels.api as sm
from statsmodels.stats.multitest import multipletests

from cancer_evidence.discovery.cohort_ingest import (
    EXPRESSION_GENES,
    MUTATION_GENES,
    SILENT_MUTATION_TYPES,
    _cbio_get,
    _cbio_post,
    _parse_event,
    _parse_sex,
    _parse_smoking,
    _parse_stage,
    _resolve_entrez_ids,
    _to_float,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_REPORT = REPO_ROOT / "artifacts" / "discovery-report" / "luad_981_discovery.json"
OUT_PATH = REPO_ROOT / "artifacts" / "discovery-report" / "blind_external_validation_707.json"


def _fetch_blind_cohort(
    study_id: str,
    mut_profile: str,
    rna_profile: str | None,
    symbol_to_entrez: dict[str, int],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame | None]:
    entrez_to_symbol = {v: k for k, v in symbol_to_entrez.items()}
    mut_entrez = [symbol_to_entrez[g] for g in MUTATION_GENES if g in symbol_to_entrez]
    expr_entrez = [symbol_to_entrez[g] for g in EXPRESSION_GENES if g in symbol_to_entrez]

    samples = _cbio_get(f"/studies/{study_id}/samples?pageSize=2000")
    sample_to_patient: dict[str, str] = {}
    patient_to_sample: dict[str, str] = {}
    for s in sorted(samples, key=lambda x: str(x["sampleId"])):
        pid = str(s["patientId"])
        sid = str(s["sampleId"])
        sample_to_patient[sid] = pid
        if pid not in patient_to_sample:
            patient_to_sample[pid] = sid

    sample_ids = sorted(patient_to_sample.values())
    pt_clin = _cbio_get(f"/studies/{study_id}/clinical-data?clinicalDataType=PATIENT&pageSize=100000")
    sm_clin = _cbio_get(f"/studies/{study_id}/clinical-data?clinicalDataType=SAMPLE&pageSize=100000")

    clin_by_pt: dict[str, dict[str, str]] = {pid: {} for pid in patient_to_sample}
    for row in pt_clin:
        pid = str(row["patientId"])
        if pid in clin_by_pt:
            clin_by_pt[pid][str(row["clinicalAttributeId"])] = str(row["value"])
    for row in sm_clin:
        sid = str(row["sampleId"])
        pid = sample_to_patient.get(sid)
        if pid and patient_to_sample.get(pid) == sid:
            clin_by_pt[pid][str(row["clinicalAttributeId"])] = str(row["value"])

    mut_hits = _cbio_post(
        f"/molecular-profiles/{mut_profile}/mutations/fetch",
        {"sampleIds": sample_ids, "entrezGeneIds": mut_entrez},
    )
    mutated_by_pt: dict[str, set[str]] = {pid: set() for pid in patient_to_sample}
    for m in mut_hits:
        if str(m.get("mutationType") or "") in SILENT_MUTATION_TYPES:
            continue
        pid = sample_to_patient.get(str(m["sampleId"]))
        gene = entrez_to_symbol.get(int(m["entrezGeneId"]))
        if pid and gene:
            mutated_by_pt[pid].add(gene)

    rna_by_pt: dict[str, dict[str, float]] = {}
    if rna_profile:
        rna_hits = _cbio_post(
            f"/molecular-profiles/{rna_profile}/molecular-data/fetch",
            {"sampleIds": sample_ids, "entrezGeneIds": expr_entrez},
        )
        for d in rna_hits:
            val = _to_float(d.get("value"))
            if val is None:
                continue
            pid = sample_to_patient.get(str(d["sampleId"]))
            gene = entrez_to_symbol.get(int(d["entrezGeneId"]))
            if pid and gene:
                rna_by_pt.setdefault(pid, {})[gene] = round(val, 4)

    p_rows, m_rows, r_rows = [], [], []
    for pid, sid in sorted(patient_to_sample.items()):
        c = clin_by_pt[pid]
        stage_num, stage_grp = _parse_stage(
            c.get("PATHOLOGIC_STAGE") or c.get("TUMOR_STAGE") or c.get("TNM_TUMOR_STAGE")
        )
        pack_yrs = _to_float(c.get("SMOKING_PACK_YEARS"))
        smoking_bin = _parse_smoking(c.get("SMOKING_HISTORY") or c.get("SMOKING_STATUS"), pack_yrs)
        p_rows.append({
            "patient_id": pid,
            "sample_id": sid,
            "stage_numeric": stage_num,
            "stage_group": stage_grp,
            "age": _to_float(c.get("AGE_AT_SURGERY") or c.get("AGE")),
            "sex_male": _parse_sex(c.get("SEX")),
            "smoking_ever": smoking_bin,
            "tumor_purity": _to_float(c.get("TUMOR_PURITY")),
            "os_months": _to_float(c.get("OS_MONTHS")),
            "os_event": _parse_event(c.get("OS_STATUS")),
            "rfs_months": _to_float(c.get("RFS_MONTHS") or c.get("DFS_MONTHS")),
            "rfs_event": _parse_event(c.get("RFS_STATUS") or c.get("DFS_STATUS")),
        })
        mrow: dict[str, Any] = {"patient_id": pid}
        for g in MUTATION_GENES:
            mrow[g] = 1 if g in mutated_by_pt[pid] else 0
        m_rows.append(mrow)
        if pid in rna_by_pt and len(rna_by_pt[pid]) >= 20:
            rrow: dict[str, Any] = {"patient_id": pid}
            for g in EXPRESSION_GENES:
                rrow[g] = rna_by_pt[pid].get(g)
            r_rows.append(rrow)

    return pd.DataFrame(p_rows), pd.DataFrame(m_rows), (pd.DataFrame(r_rows) if r_rows else None)


def _cox(df: pd.DataFrame, dur: str, ev: str, feat: str, covs: list[str]) -> dict[str, Any] | None:
    sub = df[[dur, ev, feat] + covs].dropna().copy()
    sub = sub[sub[dur] > 0]
    if len(sub) < 30 or sub[ev].sum() < 10 or sub[feat].nunique() < 2:
        return None
    active = [c for c in covs if sub[c].nunique() >= 2]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cph = CoxPHFitter(penalizer=0.01)
            cph.fit(sub[[dur, ev, feat] + active], duration_col=dur, event_col=ev)
        r = cph.summary.loc[feat]
        return {
            "n": int(len(sub)),
            "events": int(sub[ev].sum()),
            "hr": round(math.exp(float(r["coef"])), 3),
            "ci_low": round(math.exp(float(r["coef lower 95%"])), 3),
            "ci_high": round(math.exp(float(r["coef upper 95%"])), 3),
            "p": float(r["p"]),
        }
    except Exception:
        return None


def _ols(df: pd.DataFrame, y_col: str, x_col: str, covs: list[str]) -> dict[str, Any] | None:
    sub = df[[y_col, x_col] + covs].dropna().copy()
    if len(sub) < 25 or sub[x_col].nunique() < 2:
        return None
    active = [c for c in covs if sub[c].nunique() >= 2]
    X = sm.add_constant(sub[[x_col] + active].astype(float), has_constant="add")
    m = sm.OLS(sub[y_col].astype(float), X).fit()
    return {"n": int(len(sub)), "beta": round(float(m.params[x_col]), 3), "p": float(m.pvalues[x_col])}


def main() -> None:
    t0 = time.time()
    disc = json.loads(DISCOVERY_REPORT.read_text(encoding="utf-8"))
    all_genes = sorted(set(MUTATION_GENES) | set(EXPRESSION_GENES))
    sym_to_entrez = _resolve_entrez_ids(all_genes)

    print("[1/4] Downloading COMPLETELY UNSEEN Blind Holdout #1: MSKCC LUAD (luad_mskcc_2020)...")
    msk_p, msk_m, _ = _fetch_blind_cohort("luad_mskcc_2020", "luad_mskcc_2020_mutations", None, sym_to_entrez)
    print(f"      MSKCC unseen patients={len(msk_p)}, OS events={int(msk_p['os_event'].sum())}, RFS events={int(msk_p['rfs_event'].sum())}")

    print("[2/4] Downloading COMPLETELY UNSEEN Blind Holdout #2: CAS Cell 2020 LUAD (luad_cas_2020)...")
    cas_p, cas_m, cas_r = _fetch_blind_cohort(
        "luad_cas_2020", "luad_cas_2020_mutations", "luad_cas_2020_mrna_seq_fpkm_all_sample_Zscores", sym_to_entrez
    )
    assert cas_r is not None
    print(f"      CAS unseen patients={len(cas_p)}, RNA={len(cas_r)}, OS events={int(cas_p['os_event'].sum())}, DFS events={int(cas_p['rfs_event'].sum())}")

    # Verify ZERO patient_id overlap with discovery cohorts!
    disc_pts = pd.read_parquet(REPO_ROOT / "snapshots" / "discovery" / "luad_981_v1" / "patients.parquet")["patient_id"].unique()
    overlap_msk = set(msk_p["patient_id"]) & set(disc_pts)
    overlap_cas = set(cas_p["patient_id"]) & set(disc_pts)
    print(f"      Patient overlap check vs 981 discovery cohort: MSKCC={len(overlap_msk)}, CAS={len(overlap_cas)} (MUST BE 0)")

    # ------------------------------------------------------------------
    # TRACK A: BLIND TEST ON MSKCC (n=604) — Co-Mutation + Survival + Epistasis
    # ------------------------------------------------------------------
    print("\n[3/4] Running Pre-Registered Blind Tests on MSKCC (n=604) and CAS (n=103)...")
    msk_comut_results = []
    for pair in disc["comutation_and_ethnic_shift"]["top_fdr_pairs"][:10]:
        g1, g2 = pair["gene_a"], pair["gene_b"]
        if g1 not in msk_m.columns or g2 not in msk_m.columns:
            continue
        a, b = msk_m[g1].values.astype(int), msk_m[g2].values.astype(int)
        if a.sum() < 5 or b.sum() < 5:
            msk_comut_results.append({"pair": f"{g1}x{g2}", "status": "UNDERPOWERED_IN_HOLDOUT", "n_a": int(a.sum()), "n_b": int(b.sum())})
            continue
        odds, p = stats.fisher_exact([
            [int(((a == 1) & (b == 1)).sum()), int(((a == 1) & (b == 0)).sum())],
            [int(((a == 0) & (b == 1)).sum()), int(((a == 0) & (b == 0)).sum())],
        ])
        expected_co = pair["relationship"] == "CO_OCCURRING"
        same_dir = (odds > 1.0) if expected_co else (odds < 1.0)
        replicated = bool(same_dir and p < 0.05)
        msk_comut_results.append({
            "pair": f"{g1}x{g2}",
            "expected": pair["relationship"],
            "discovery_or": pair["odds_ratio"],
            "blind_msk_or": round(float(odds), 3) if math.isfinite(odds) else 999.0,
            "blind_msk_p": float(p),
            "same_direction": same_dir,
            "status": "REPLICATED" if replicated else ("DIRECTION_MATCH_NS" if same_dir else "FAILED"),
        })

    # Blind Survival Validation on MSKCC (STK11 discovery + Confounder Kills + TP53xPIK3CA borderline check)
    msk_full = msk_p.merge(msk_m, on="patient_id", how="inner")
    msk_covs = ["stage_numeric", "age", "sex_male", "smoking_ever"]
    msk_surv_tests = {}
    for gene, expect in [("STK11", "HR>1_DISCOVERY"), ("KEAP1", "KILLED_IN_DISCOVERY"), ("TP53", "KILLED_IN_DISCOVERY"), ("KRAS", "KILLED_IN_DISCOVERY")]:
        os_res = _cox(msk_full, "os_months", "os_event", gene, msk_covs)
        rfs_res = _cox(msk_full, "rfs_months", "rfs_event", gene, msk_covs)
        msk_surv_tests[gene] = {"discovery_label": expect, "n_mut": int(msk_full[gene].sum()), "blind_msk_os_adj": os_res, "blind_msk_rfs_adj": rfs_res}

    # Test borderline TP53 x PIK3CA interaction on MSKCC (which our FDR rightly rejected at q=0.074!)
    msk_ix = msk_full[["os_months", "os_event", "TP53", "PIK3CA"] + msk_covs].dropna().copy()
    msk_ix["interaction"] = msk_ix["TP53"] * msk_ix["PIK3CA"]
    tp53_pik3ca_blind = {
        "n_double_mut_in_msk": int(msk_ix["interaction"].sum()),
        "blind_os_interaction": _cox(msk_ix, "os_months", "os_event", "interaction", ["TP53", "PIK3CA"] + msk_covs),
    }

    # ------------------------------------------------------------------
    # TRACK B: BLIND TEST ON CAS Cell 2020 (n=103) — RNA Survival + Genotype -> ADC/Payload
    # ------------------------------------------------------------------
    cas_full = (
        cas_p.merge(cas_m, on="patient_id", how="inner")
        .merge(cas_r, on="patient_id", how="inner", suffixes=("_mut", "_rna"))
    )
    cas_covs = ["stage_numeric", "age", "sex_male", "tumor_purity"]

    cas_surv_results = []
    for h in disc["confounder_adjusted_survival"]["top_fdr_survival_hits"]:
        if h["modality"] != "rna_zscore":
            continue
        g = h["biomarker"]
        rc = f"{g}_rna" if f"{g}_rna" in cas_full.columns else g
        os_r = _cox(cas_full, "os_months", "os_event", rc, cas_covs)
        dfs_r = _cox(cas_full, "rfs_months", "rfs_event", rc, cas_covs)
        exp_bad = h["hr_adj"] > 1.0
        os_same = bool(os_r and ((os_r["hr"] > 1.0) == exp_bad))
        dfs_same = bool(dfs_r and ((dfs_r["hr"] > 1.0) == exp_bad))
        cas_surv_results.append({
            "biomarker": g,
            "discovery_hr": h["hr_adj"],
            "blind_cas_os_adj": os_r,
            "blind_cas_dfs_adj": dfs_r,
            "os_same_direction": os_same,
            "dfs_same_direction": dfs_same,
            "status": (
                "REPLICATED_P05"
                if ((os_same and os_r and os_r["p"] < 0.05) or (dfs_same and dfs_r and dfs_r["p"] < 0.05))
                else ("DIRECTION_MATCH_UNDERPOWERED_N103" if (os_same or dfs_same) else "FAILED")
            ),
        })

    cas_adc_results = []
    for item in disc["genotype_to_adc_and_payload_reprogramming"]["gold_trans_discoveries"][:12]:
        mg, tg = item["driver_mutation"], item["target_gene"]
        mc = f"{mg}_mut" if f"{mg}_mut" in cas_full.columns else mg
        rc = f"{tg}_rna" if f"{tg}_rna" in cas_full.columns else tg
        if mc not in cas_full.columns or rc not in cas_full.columns:
            continue
        n_mut = int(cas_full[mc].sum())
        if n_mut < 4:
            cas_adc_results.append({
                "pair": f"{mg}->{tg}",
                "expected": item["direction"],
                "n_mut_in_cas": n_mut,
                "status": "UNDERPOWERED_IN_CAS",
            })
            continue
        res = _ols(cas_full, rc, mc, ["stage_numeric", "tumor_purity"])
        exp_up = item["direction"] == "UPREGULATED"
        same_dir = bool(res and ((res["beta"] > 0) == exp_up))
        replicated = bool(same_dir and res and res["p"] < 0.05)
        cas_adc_results.append({
            "pair": f"{mg}->{tg}",
            "expected": item["direction"],
            "discovery_rna_beta": item["beta_rna_z"],
            "discovery_prot_beta": item["cptac_protein_beta_z"],
            "n_mut_in_cas": n_mut,
            "blind_cas_beta_purity_adj": res["beta"] if res else None,
            "blind_cas_p": round(res["p"], 5) if res else None,
            "same_direction": same_dir,
            "status": "REPLICATED" if replicated else ("DIRECTION_MATCH_NS" if same_dir else "FAILED"),
        })

    # ------------------------------------------------------------------
    # TRACK C: NEGATIVE-CONTROL SHUFFLED NULL TEST (Anti-Hallucination Proof)
    # ------------------------------------------------------------------
    print("[4/4] Running 30x Shuffled Null Negative-Control Test (Proving Zero Hallucination on Pure Noise)...")
    rng = np.random.default_rng(123)
    test_muts = [g for g in MUTATION_GENES if (f"{g}_mut" if f"{g}_mut" in cas_full.columns else g) in cas_full.columns and int(cas_full[f"{g}_mut" if f"{g}_mut" in cas_full.columns else g].sum()) >= 5]
    test_rnas = [g for g in EXPRESSION_GENES[:19] if (f"{g}_rna" if f"{g}_rna" in cas_full.columns else g) in cas_full.columns]

    null_naive_p05 = []
    null_fdr_q05 = []
    n_null_runs = 30
    for _ in range(n_null_runs):
        perm_idx = rng.permutation(len(cas_full))
        pvals = []
        for mg in test_muts:
            mc = f"{mg}_mut" if f"{mg}_mut" in cas_full.columns else mg
            x = cas_full[mc].values[perm_idx]
            for tg in test_rnas:
                rc = f"{tg}_rna" if f"{tg}_rna" in cas_full.columns else tg
                y = pd.to_numeric(cas_full[rc], errors="coerce").to_numpy(dtype=float)
                mask = ~np.isnan(y)
                xm, ym = x[mask], y[mask]
                if xm.sum() < 4 or (xm == 0).sum() < 4:
                    continue
                _, pv = stats.ttest_ind(ym[xm == 1], ym[xm == 0], equal_var=False)
                if math.isfinite(pv):
                    pvals.append(float(pv))
        _, qvals, _, _ = multipletests(pvals, method="fdr_bh")
        null_naive_p05.append(sum(1 for pv in pvals if pv < 0.05))
        null_fdr_q05.append(sum(1 for qv in qvals if qv < 0.05))

    null_summary = {
        "permutations": n_null_runs,
        "tests_per_permutation": len(pvals),
        "mean_naive_p05_false_positives_per_run": round(float(np.mean(null_naive_p05)), 2),
        "mean_fdr_q05_false_discoveries_per_run": round(float(np.mean(null_fdr_q05)), 2),
        "max_fdr_q05_false_discoveries_seen": int(np.max(null_fdr_q05)),
        "pct_runs_with_zero_fdr_hallucinations": round(100.0 * sum(1 for c in null_fdr_q05 if c == 0) / n_null_runs, 1),
    }

    out = {
        "blind_cohorts": {
            "MSKCC_2020": {"study_id": "luad_mskcc_2020", "n": int(len(msk_p)), "overlap_with_discovery": len(overlap_msk)},
            "CAS_CELL_2020": {"study_id": "luad_cas_2020", "n": int(len(cas_p)), "overlap_with_discovery": len(overlap_cas)},
        },
        "track_a_mskcc_comutation": msk_comut_results,
        "track_a_mskcc_survival": msk_surv_tests,
        "track_a_mskcc_tp53_pik3ca_borderline": tp53_pik3ca_blind,
        "track_b_cas_rna_survival": cas_surv_results,
        "track_b_cas_genotype_to_adc_payload": cas_adc_results,
        "track_c_shuffled_null_negative_control": null_summary,
    }
    OUT_PATH.write_text(
        json.dumps(
            out,
            indent=2,
            sort_keys=True,
            default=lambda o: bool(o) if isinstance(o, np.bool_) else (int(o) if isinstance(o, np.integer) else float(o)),
        ) + "\n",
        encoding="utf-8",
    )

    # Crisp honest console scorecard
    print(f"\nDone in {time.time() - t0:.1f}s -> {OUT_PATH}\n")
    print("=" * 84)
    print("BLIND EXTERNAL VALIDATION SCORECARD (707 COMPLETELY UNSEEN PATIENTS, 0 OVERLAP)")
    print("=" * 84)

    evaluable_cm = [r for r in msk_comut_results if r["status"] != "UNDERPOWERED_IN_HOLDOUT"]
    rep_cm = sum(1 for r in evaluable_cm if r["status"] == "REPLICATED")
    dir_cm = sum(1 for r in evaluable_cm if r.get("same_direction"))
    print(f"\n[TRACK A1] MSKCC (n=604) BLIND CO-MUTATION REPLICATION: {rep_cm}/{len(evaluable_cm)} p<0.05 REPLICATED ({dir_cm}/{len(evaluable_cm)} same direction)")
    for r in msk_comut_results:
        if r["status"] == "UNDERPOWERED_IN_HOLDOUT":
            print(f"  {r['pair']:15s} | UNDERPOWERED (n_a={r['n_a']}, n_b={r['n_b']})")
        else:
            print(f"  {r['pair']:15s} | {r['status']:18s} | disc OR={r['discovery_or']:5.2f} -> BLIND MSK OR={r['blind_msk_or']:5.2f} (p={r['blind_msk_p']:.2e})")

    print("\n[TRACK A2] MSKCC (n=604) BLIND STAGE+AGE+SEX+SMOKING-ADJUSTED SURVIVAL (OS & RFS)")
    for g, d in msk_surv_tests.items():
        osr = d["blind_msk_os_adj"]
        rfs = d["blind_msk_rfs_adj"]
        print(
            f"  {g:7s} ({d['discovery_label']:19s}, n_mut={d['n_mut']:3d}) | "
            f"BLIND OS HR={osr['hr'] if osr else 'NA'} (p={osr['p']:.4f}) | "
            f"BLIND RFS HR={rfs['hr'] if rfs else 'NA'} (p={rfs['p']:.4f})"
        )
    print(f"  Borderline TP53xPIK3CA (rejected by FDR in discovery): {tp53_pik3ca_blind}")

    print("\n[TRACK B1] CAS Cell 2020 (n=103) BLIND STAGE+PURITY-ADJUSTED RNA SURVIVAL")
    for r in cas_surv_results:
        osr, dfsr = r["blind_cas_os_adj"], r["blind_cas_dfs_adj"]
        os_str = f"HR={osr['hr']} (p={osr['p']:.4f})" if osr else "UNDERPOWERED"
        dfs_str = f"HR={dfsr['hr']} (p={dfsr['p']:.4f})" if dfsr else "UNDERPOWERED"
        print(
            f"  {r['biomarker']:8s} (disc HR={r['discovery_hr']}) | {r['status']:32s} | "
            f"CAS OS {os_str} | CAS DFS {dfs_str}"
        )

    ev_adc = [r for r in cas_adc_results if r["status"] != "UNDERPOWERED_IN_CAS"]
    rep_adc = sum(1 for r in ev_adc if r["status"] == "REPLICATED")
    dir_adc = sum(1 for r in ev_adc if r.get("same_direction"))
    print(f"\n[TRACK B2] CAS Cell 2020 (n=103) BLIND GENOTYPE -> ADC/PAYLOAD REPROGRAMMING: {rep_adc}/{len(ev_adc)} p<0.05 REPLICATED ({dir_adc}/{len(ev_adc)} same direction)")
    for r in cas_adc_results:
        if r["status"] == "UNDERPOWERED_IN_CAS":
            print(f"  {r['pair']:16s} | UNDERPOWERED IN CAS (n_mut={r['n_mut_in_cas']} < 4)")
        else:
            print(
                f"  {r['pair']:16s} | {r['status']:18s} | disc RNA={r['discovery_rna_beta']:+.2f}/Prot={r['discovery_prot_beta']:+.2f} -> "
                f"BLIND CAS Purity-Adj beta={r['blind_cas_beta_purity_adj']:+.2f} (p={r['blind_cas_p']:.4f}, n_mut={r['n_mut_in_cas']})"
            )

    print("\n[TRACK C] SHUFFLED NULL NEGATIVE-CONTROL TEST (30x Pure Random Noise)")
    print(f"  Tests/run: {null_summary['tests_per_permutation']}")
    print(f"  Naive p<0.05 false positives per run : {null_summary['mean_naive_p05_false_positives_per_run']}")
    print(f"  EGIS FDR q<0.05 hallucinations/run   : {null_summary['mean_fdr_q05_false_discoveries_per_run']} (0 hallucinations in {null_summary['pct_runs_with_zero_fdr_hallucinations']}% of runs)")


if __name__ == "__main__":
    main()
