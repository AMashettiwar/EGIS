from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import time
from typing import Any
import urllib.parse
import urllib.request
import warnings

from lifelines import CoxPHFitter
from lifelines.statistics import multivariate_logrank_test
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.impute import SimpleImputer
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler
import statsmodels.api as sm
from statsmodels.stats.multitest import multipletests

from .cohort_ingest import EXPRESSION_GENES, MUTATION_GENES

ADC_AND_PAYLOAD_GENES: tuple[str, ...] = (
    "TACSTD2", "ERBB2", "MET", "ERBB3", "EGFR", "CD276",
    "CEACAM5", "NECTIN4", "FOLR1", "MUC1", "MSLN", "DLL3",
    "SLFN11", "TOP1", "ABCG2", "ABCB1", "TUBB3", "BRCA1", "BRCA2",
)

MIN_CARRIERS = 15
MIN_POWER_N = 15


def _safe_float(v: Any, digits: int = 4) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if not math.isfinite(f) else round(f, digits)
    except (TypeError, ValueError):
        return None


def _fit_cox(df: pd.DataFrame, duration_col: str, event_col: str, feature_col: str, covariates: list[str]) -> dict[str, Any] | None:
    cols = [duration_col, event_col, feature_col] + covariates
    sub = df[cols].dropna().copy()
    sub = sub[sub[duration_col] > 0]
    if len(sub) < 40 or sub[event_col].sum() < 12:
        return None
    if sub[feature_col].nunique() < 2:
        return None
    # Drop zero-variance covariates cleanly
    active_covs = [c for c in covariates if sub[c].nunique() >= 2]
    sub = sub[[duration_col, event_col, feature_col] + active_covs]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cph = CoxPHFitter(penalizer=0.01)
            cph.fit(sub, duration_col=duration_col, event_col=event_col)
        row = cph.summary.loc[feature_col]
        return {
            "n": int(len(sub)),
            "events": int(sub[event_col].sum()),
            "hr": _safe_float(math.exp(float(row["coef"])), 3),
            "log_hr": _safe_float(float(row["coef"]), 4),
            "ci_low": _safe_float(math.exp(float(row["coef lower 95%"])), 3),
            "ci_high": _safe_float(math.exp(float(row["coef upper 95%"])), 3),
            "p": float(row["p"]),
        }
    except Exception:
        return None


def _fit_ols(df: pd.DataFrame, y_col: str, x_col: str, covariates: list[str]) -> dict[str, Any] | None:
    cols = [y_col, x_col] + covariates
    sub = df[cols].dropna().copy()
    if len(sub) < 30 or sub[x_col].nunique() < 2:
        return None
    active_covs = [c for c in covariates if sub[c].nunique() >= 2]
    X = sm.add_constant(sub[[x_col] + active_covs].astype(float), has_constant="add")
    y = sub[y_col].astype(float)
    try:
        model = sm.OLS(y, X).fit()
        return {
            "n": int(len(sub)),
            "beta": _safe_float(model.params[x_col], 3),
            "se": _safe_float(model.bse[x_col], 3),
            "p": float(model.pvalues[x_col]),
            "r2": _safe_float(model.rsquared, 3),
        }
    except Exception:
        return None


def _pubmed_count(query: str) -> int | None:
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode({
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": 0,
    })
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "EGIS-discovery/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return int(data["esearchresult"]["count"])
    except Exception:
        return None


def _novelty_tier(count: int | None) -> str:
    if count is None:
        return "PUBMED_UNAVAILABLE"
    if count >= 50:
        return "ESTABLISHED_SANITY_CHECK"
    if count >= 5:
        return "EMERGING_SIGNAL"
    return "UNDER_EXPLORED_CANDIDATE"


class LungDiscoveryEngine:
    """Zero-LLM, confounder-aware, FDR-controlled discovery & interrogation engine over 981 LUAD patients."""

    def __init__(self, snapshot_dir: str | Path) -> None:
        self.root = Path(snapshot_dir)
        self.manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.patients = pq.read_table(self.root / "patients.parquet").to_pandas()
        self.mutations = pq.read_table(self.root / "mutations.parquet").to_pandas()
        self.rna = pq.read_table(self.root / "rna_zscores.parquet").to_pandas()
        self.proteomics = pq.read_table(self.root / "proteomics_zscores.parquet").to_pandas()

        # Numeric cohort indicators for multivariable batch/cohort adjustment
        self.patients["is_oncosg"] = (self.patients["cohort"] == "ONCOSG_LUAD").astype(float)
        self.patients["is_cptac"] = (self.patients["cohort"] == "CPTAC_LUAD").astype(float)

    # ------------------------------------------------------------------
    # MODULE 1: Co-Mutation, Mutual Exclusivity & Western vs Asian Shift
    # ------------------------------------------------------------------
    def run_comutation_scan(self) -> dict[str, Any]:
        merged = self.patients[["patient_uid", "cohort", "region"]].merge(
            self.mutations, on=["patient_uid", "cohort"], how="inner"
        )
        eligible_genes = [g for g in MUTATION_GENES if g in merged.columns and int(merged[g].sum()) >= MIN_CARRIERS]

        raw_pairs: list[dict[str, Any]] = []
        for i, g1 in enumerate(eligible_genes):
            for g2 in eligible_genes[i + 1 :]:
                a = merged[g1].values.astype(int)
                b = merged[g2].values.astype(int)
                both = int(((a == 1) & (b == 1)).sum())
                only1 = int(((a == 1) & (b == 0)).sum())
                only2 = int(((a == 0) & (b == 1)).sum())
                neither = int(((a == 0) & (b == 0)).sum())
                odds, pval = stats.fisher_exact([[both, only1], [only2, neither]])

                # Independent cohort replication check
                rep_dirs: dict[str, dict[str, Any]] = {}
                same_dir_sig = 0
                pooled_dir = 1 if odds > 1.0 else -1
                for coh in ("TCGA_LUAD", "ONCOSG_LUAD", "CPTAC_LUAD"):
                    sub = merged[merged["cohort"] == coh]
                    sa = sub[g1].values.astype(int)
                    sb = sub[g2].values.astype(int)
                    if sa.sum() < 5 or sb.sum() < 5:
                        continue
                    c_or, c_p = stats.fisher_exact([
                        [int(((sa == 1) & (sb == 1)).sum()), int(((sa == 1) & (sb == 0)).sum())],
                        [int(((sa == 0) & (sb == 1)).sum()), int(((sa == 0) & (sb == 0)).sum())],
                    ])
                    c_dir = 1 if c_or > 1.0 else -1
                    if c_dir == pooled_dir and c_p < 0.05:
                        same_dir_sig += 1
                    rep_dirs[coh] = {"or": _safe_float(c_or, 2), "p": round(float(c_p), 5)}

                raw_pairs.append({
                    "gene_a": g1,
                    "gene_b": g2,
                    "relationship": "CO_OCCURRING" if odds > 1.0 else "MUTUALLY_EXCLUSIVE",
                    "n_total": int(len(merged)),
                    "n_both": both,
                    "n_a": int(a.sum()),
                    "n_b": int(b.sum()),
                    "odds_ratio": _safe_float(odds, 3) if math.isfinite(odds) else 999.0,
                    "p": float(pval),
                    "cohort_replication": rep_dirs,
                    "replicated_cohorts": same_dir_sig,
                })

        pvals = [r["p"] for r in raw_pairs]
        _, qvals, _, _ = multipletests(pvals, method="fdr_bh")
        for r, q in zip(raw_pairs, qvals):
            r["fdr_q"] = float(q)
            r["p_fmt"] = f"{r['p']:.2e}"
            r["q_fmt"] = f"{q:.2e}"

        naive_p05 = sum(1 for r in raw_pairs if r["p"] < 0.05)
        fdr_hits = sorted([r for r in raw_pairs if r["fdr_q"] < 0.05], key=lambda x: x["fdr_q"])

        # Western (TCGA) vs East Asian (OncoSG) driver prevalence shift
        eth_rows: list[dict[str, Any]] = []
        tcga = merged[merged["cohort"] == "TCGA_LUAD"]
        oncosg = merged[merged["cohort"] == "ONCOSG_LUAD"]
        for g in eligible_genes:
            t_pos, t_n = int(tcga[g].sum()), int(len(tcga))
            o_pos, o_n = int(oncosg[g].sum()), int(len(oncosg))
            odds, pval = stats.fisher_exact([[o_pos, o_n - o_pos], [t_pos, t_n - t_pos]])
            eth_rows.append({
                "gene": g,
                "western_tcga_pct": round(100.0 * t_pos / t_n, 1),
                "east_asian_oncosg_pct": round(100.0 * o_pos / o_n, 1),
                "odds_ratio_asian_vs_western": _safe_float(odds, 2),
                "p": float(pval),
            })
        _, eth_q, _, _ = multipletests([r["p"] for r in eth_rows], method="fdr_bh")
        for r, q in zip(eth_rows, eth_q):
            r["fdr_q"] = float(q)
        eth_sig = sorted([r for r in eth_rows if r["fdr_q"] < 0.05], key=lambda x: x["fdr_q"])

        return {
            "n_sequenced_patients": int(len(merged)),
            "pairs_tested": len(raw_pairs),
            "naive_p05_count": naive_p05,
            "fdr_q05_count": len(fdr_hits),
            "false_positives_filtered_by_fdr": naive_p05 - len(fdr_hits),
            "top_fdr_pairs": fdr_hits[:20],
            "western_vs_east_asian_shifts": eth_sig[:12],
        }

    # ------------------------------------------------------------------
    # MODULE 2: Confounder-Adjusted Survival Discovery + Confounder Kills
    # ------------------------------------------------------------------
    def run_survival_discovery(self, bootstrap_iters: int = 80) -> dict[str, Any]:
        base = self.patients.copy()
        surv_mut = base.merge(self.mutations, on=["patient_uid", "cohort"], how="inner")
        surv_rna = base.merge(self.rna, on=["patient_uid", "cohort"], how="inner")

        covs_pooled = ["stage_numeric", "age", "sex_male", "is_oncosg"]
        covs_single = ["stage_numeric", "age", "sex_male"]

        candidates: list[dict[str, Any]] = []

        # 1. Scan all 30 mutated genes
        for g in MUTATION_GENES:
            if g not in surv_mut.columns or int(surv_mut[g].sum()) < MIN_CARRIERS:
                continue
            unadj = _fit_cox(surv_mut, "os_months", "os_event", g, [])
            adj = _fit_cox(surv_mut, "os_months", "os_event", g, covs_pooled)
            if not unadj or not adj:
                continue
            tcga_r = _fit_cox(surv_mut[surv_mut["cohort"] == "TCGA_LUAD"], "os_months", "os_event", g, covs_single)
            onco_r = _fit_cox(surv_mut[surv_mut["cohort"] == "ONCOSG_LUAD"], "os_months", "os_event", g, covs_single)
            candidates.append({
                "biomarker": g,
                "modality": "mutation",
                "n_adj": adj["n"],
                "events": adj["events"],
                "hr_unadj": unadj["hr"],
                "p_unadj": unadj["p"],
                "hr_adj": adj["hr"],
                "ci_low": adj["ci_low"],
                "ci_high": adj["ci_high"],
                "p_adj": adj["p"],
                "confounder_killed": bool(unadj["p"] < 0.05 and adj["p"] > 0.10),
                "tcga_adj": tcga_r,
                "oncosg_adj": onco_r,
                "direction_replicated_both_cohorts": bool(
                    tcga_r and onco_r and ((tcga_r["hr"] > 1.0) == (onco_r["hr"] > 1.0))
                ),
            })

        # 2. Scan all 45 RNA biomarkers
        for g in EXPRESSION_GENES:
            if g not in surv_rna.columns:
                continue
            unadj = _fit_cox(surv_rna, "os_months", "os_event", g, [])
            adj = _fit_cox(surv_rna, "os_months", "os_event", g, covs_pooled)
            if not unadj or not adj:
                continue
            tcga_r = _fit_cox(surv_rna[surv_rna["cohort"] == "TCGA_LUAD"], "os_months", "os_event", g, covs_single)
            onco_r = _fit_cox(surv_rna[surv_rna["cohort"] == "ONCOSG_LUAD"], "os_months", "os_event", g, covs_single)
            candidates.append({
                "biomarker": g,
                "modality": "rna_zscore",
                "n_adj": adj["n"],
                "events": adj["events"],
                "hr_unadj": unadj["hr"],
                "p_unadj": unadj["p"],
                "hr_adj": adj["hr"],
                "ci_low": adj["ci_low"],
                "ci_high": adj["ci_high"],
                "p_adj": adj["p"],
                "confounder_killed": bool(unadj["p"] < 0.05 and adj["p"] > 0.10),
                "tcga_adj": tcga_r,
                "oncosg_adj": onco_r,
                "direction_replicated_both_cohorts": bool(
                    tcga_r and onco_r and ((tcga_r["hr"] > 1.0) == (onco_r["hr"] > 1.0))
                ),
            })

        _, q_adj, _, _ = multipletests([c["p_adj"] for c in candidates], method="fdr_bh")
        for c, q in zip(candidates, q_adj):
            c["fdr_q_adj"] = float(q)

        fdr_hits = sorted([c for c in candidates if c["fdr_q_adj"] < 0.05], key=lambda x: x["fdr_q_adj"])
        confounder_kills = sorted([c for c in candidates if c["confounder_killed"]], key=lambda x: x["p_unadj"])

        # Bootstrap stability on top 12 FDR-surviving survival hits
        rng = np.random.default_rng(42)
        for hit in fdr_hits[:12]:
            df_src = surv_mut if hit["modality"] == "mutation" else surv_rna
            g = hit["biomarker"]
            cols = ["os_months", "os_event", g] + covs_pooled
            sub = df_src[cols].dropna()
            sub = sub[sub["os_months"] > 0].reset_index(drop=True)
            same_dir = 0
            sig_05 = 0
            target_dir = 1 if hit["hr_adj"] > 1.0 else -1
            n_rows = len(sub)
            for _ in range(bootstrap_iters):
                idx = rng.integers(0, n_rows, size=n_rows)
                b_res = _fit_cox(sub.iloc[idx], "os_months", "os_event", g, covs_pooled)
                if b_res:
                    if (1 if b_res["hr"] > 1.0 else -1) == target_dir:
                        same_dir += 1
                    if b_res["p"] < 0.05:
                        sig_05 += 1
            hit["bootstrap_same_dir_pct"] = round(100.0 * same_dir / bootstrap_iters, 1)
            hit["bootstrap_p05_pct"] = round(100.0 * sig_05 / bootstrap_iters, 1)

        return {
            "total_biomarkers_tested": len(candidates),
            "naive_unadjusted_p05": sum(1 for c in candidates if c["p_unadj"] < 0.05),
            "adjusted_p05": sum(1 for c in candidates if c["p_adj"] < 0.05),
            "fdr_q05_adjusted_survivors": len(fdr_hits),
            "confounder_killed_count": len(confounder_kills),
            "top_confounder_kills": confounder_kills[:10],
            "top_fdr_survival_hits": fdr_hits[:15],
        }

    # ------------------------------------------------------------------
    # MODULE 3: Genotype -> ADC Target & Payload-Sensitivity Reprogramming
    #           (FDR + Stage/Cohort + Tumor Purity + CPTAC Mass-Spec Protein!)
    # ------------------------------------------------------------------
    def run_genotype_to_adc_protein_scan(self) -> dict[str, Any]:
        rna_df = (
            self.patients[["patient_uid", "cohort", "stage_numeric", "tumor_purity", "is_oncosg", "is_cptac"]]
            .merge(self.mutations, on=["patient_uid", "cohort"], how="inner")
            .merge(self.rna, on=["patient_uid", "cohort"], how="inner", suffixes=("_mut", "_rna"))
        )
        prot_df = (
            self.patients[["patient_uid", "cohort", "stage_numeric", "tumor_purity"]]
            .merge(self.mutations, on=["patient_uid", "cohort"], how="inner")
            .merge(self.proteomics, on=["patient_uid", "cohort"], how="inner", suffixes=("_mut", "_prot"))
        )

        # Resolve column names cleanly if a gene is in both mutations and expression (e.g. EGFR, ERBB2, MET)
        def mut_col(df: pd.DataFrame, g: str) -> str:
            return f"{g}_mut" if f"{g}_mut" in df.columns else g

        def rna_col(df: pd.DataFrame, g: str) -> str:
            return f"{g}_rna" if f"{g}_rna" in df.columns else g

        def prot_col(df: pd.DataFrame, g: str) -> str:
            return f"{g}_prot" if f"{g}_prot" in df.columns else g

        eligible_muts = [
            g for g in MUTATION_GENES
            if mut_col(rna_df, g) in rna_df.columns and int(rna_df[mut_col(rna_df, g)].sum()) >= 20
        ]

        results: list[dict[str, Any]] = []
        for mg in eligible_muts:
            mc_r = mut_col(rna_df, mg)
            mc_p = mut_col(prot_df, mg)
            for tg in ADC_AND_PAYLOAD_GENES:
                rc = rna_col(rna_df, tg)
                if rc not in rna_df.columns:
                    continue
                ols_rna = _fit_ols(rna_df, rc, mc_r, ["stage_numeric", "is_oncosg", "is_cptac"])
                if not ols_rna:
                    continue

                # Check Tumor-Purity-Adjusted replication (OncoSG + CPTAC have measured purity!)
                ols_purity = _fit_ols(rna_df, rc, mc_r, ["tumor_purity", "stage_numeric", "is_cptac"])

                # Check CPTAC Mass-Spec Protein replication!
                pc = prot_col(prot_df, tg)
                ols_prot = None
                if pc in prot_df.columns and mc_p in prot_df.columns and int(prot_df[mc_p].sum()) >= 6:
                    ols_prot = _fit_ols(prot_df, pc, mc_p, ["tumor_purity"])

                same_dir_prot = bool(
                    ols_prot
                    and ols_prot["beta"] is not None
                    and ols_rna["beta"] is not None
                    and ((ols_prot["beta"] > 0) == (ols_rna["beta"] > 0))
                    and ols_prot["p"] < 0.05
                )
                purity_survived = bool(
                    ols_purity
                    and ols_purity["beta"] is not None
                    and ols_rna["beta"] is not None
                    and ((ols_purity["beta"] > 0) == (ols_rna["beta"] > 0))
                    and ols_purity["p"] < 0.05
                )

                results.append({
                    "driver_mutation": mg,
                    "target_gene": tg,
                    "n_rna": ols_rna["n"],
                    "n_mutated": int(rna_df[mc_r].sum()),
                    "beta_rna_z": ols_rna["beta"],
                    "p_rna": ols_rna["p"],
                    "direction": "UPREGULATED" if (ols_rna["beta"] or 0) > 0 else "DOWNREGULATED",
                    "purity_adj_beta": ols_purity["beta"] if ols_purity else None,
                    "purity_adj_p": round(ols_purity["p"], 5) if ols_purity else None,
                    "survived_purity_control": purity_survived,
                    "cptac_protein_n": ols_prot["n"] if ols_prot else 0,
                    "cptac_protein_beta_z": ols_prot["beta"] if ols_prot else None,
                    "cptac_protein_p": round(ols_prot["p"], 5) if ols_prot else None,
                    "replicated_at_protein_level": same_dir_prot,
                })

        _, q_rna, _, _ = multipletests([r["p_rna"] for r in results], method="fdr_bh")
        for r, q in zip(results, q_rna):
            r["fdr_q_rna"] = float(q)

        fdr_hits = sorted([r for r in results if r["fdr_q_rna"] < 0.05], key=lambda x: x["fdr_q_rna"])
        gold_hits = [
            r for r in fdr_hits if r["replicated_at_protein_level"] and r["survived_purity_control"]
        ]
        # Exclude trivial self-cis regulation (e.g., EGFR mut -> EGFR expr) when ranking trans discoveries
        gold_trans = [r for r in gold_hits if r["driver_mutation"] != r["target_gene"]]

        return {
            "total_pairs_tested": len(results),
            "fdr_q05_rna_hits": len(fdr_hits),
            "gold_purity_and_protein_replicated_hits": len(gold_hits),
            "gold_trans_discoveries": gold_trans[:20],
            "top_all_fdr_hits": fdr_hits[:20],
        }

    # ------------------------------------------------------------------
    # MODULE 4: Hidden Two-Way Epistatic Survival Interactions (A x B)
    # ------------------------------------------------------------------
    def run_epistatic_interactions(self) -> dict[str, Any]:
        surv_mut = self.patients.merge(self.mutations, on=["patient_uid", "cohort"], how="inner")
        surv_mut = surv_mut.dropna(subset=["os_months", "os_event", "stage_numeric"])
        surv_mut = surv_mut[surv_mut["os_months"] > 0].copy()

        eligible = [g for g in MUTATION_GENES if g in surv_mut.columns and int(surv_mut[g].sum()) >= 30]
        interactions: list[dict[str, Any]] = []
        for i, g1 in enumerate(eligible):
            for g2 in eligible[i + 1 :]:
                both = int(((surv_mut[g1] == 1) & (surv_mut[g2] == 1)).sum())
                if both < MIN_POWER_N:
                    continue
                df = surv_mut[["os_months", "os_event", g1, g2, "stage_numeric", "age", "sex_male", "is_oncosg"]].dropna().copy()
                df["interaction"] = df[g1].astype(float) * df[g2].astype(float)
                res = _fit_cox(df, "os_months", "os_event", "interaction", [g1, g2, "stage_numeric", "age", "sex_male", "is_oncosg"])
                if not res:
                    continue
                interactions.append({
                    "gene_a": g1,
                    "gene_b": g2,
                    "n_double_mutant": both,
                    "n_total": res["n"],
                    "interaction_hr": res["hr"],
                    "ci_low": res["ci_low"],
                    "ci_high": res["ci_high"],
                    "p_interaction": res["p"],
                    "synergy_direction": "WORSE_SYNERGY" if (res["hr"] or 1.0) > 1.0 else "PROTECTIVE_INTERACTION",
                })

        if interactions:
            _, qvals, _, _ = multipletests([r["p_interaction"] for r in interactions], method="fdr_bh")
            for r, q in zip(interactions, qvals):
                r["fdr_q"] = float(q)
            interactions.sort(key=lambda x: x["p_interaction"])

        return {
            "pairs_tested_with_min_15_double_mutants": len(interactions),
            "nominal_p05_interactions": sum(1 for r in interactions if r["p_interaction"] < 0.05),
            "fdr_q05_interactions": sum(1 for r in interactions if r["fdr_q"] < 0.05),
            "top_interactions": interactions[:10],
        }

    # ------------------------------------------------------------------
    # MODULE 5: Unsupervised Multi-Cohort Subtype Discovery (Zero Labels)
    # ------------------------------------------------------------------
    def run_unsupervised_subtypes(self) -> dict[str, Any]:
        expr_cols = [g for g in EXPRESSION_GENES if g in self.rna.columns]
        merged = self.patients.merge(self.rna, on=["patient_uid", "cohort"], how="inner")
        X_raw = merged[expr_cols].values.astype(float)
        imp = SimpleImputer(strategy="median")
        scaler = StandardScaler()
        X_std = scaler.fit_transform(imp.fit_transform(X_raw))

        pca = PCA(n_components=min(12, X_std.shape[1]), random_state=42)
        X_pca = pca.fit_transform(X_std)

        best_k = 3
        best_sil = -1.0
        best_labels = None
        for k in (3, 4, 5):
            km = KMeans(n_clusters=k, n_init=30, random_state=42)
            labels = km.fit_predict(X_pca[:, :8])
            sil = float(silhouette_score(X_pca[:, :8], labels))
            if sil > best_sil:
                best_sil = sil
                best_k = k
                best_labels = labels

        merged = merged.copy()
        merged["cluster"] = best_labels

        # Blind survival test across discovered clusters (Log-Rank + Stage/Cohort-Adjusted Cox)
        surv_sub = merged.dropna(subset=["os_months", "os_event"])
        surv_sub = surv_sub[surv_sub["os_months"] > 0]
        lr = multivariate_logrank_test(surv_sub["os_months"], surv_sub["cluster"], surv_sub["os_event"])

        # Enrich cluster profiles with mutations + ADC targets + immune + survival
        mut_merged = merged[["patient_uid", "cohort", "cluster"]].merge(
            self.mutations, on=["patient_uid", "cohort"], how="left"
        )

        profiles: list[dict[str, Any]] = []
        for cid in range(best_k):
            c_df = merged[merged["cluster"] == cid]
            c_mut = mut_merged[mut_merged["cluster"] == cid]
            c_surv = surv_sub[surv_sub["cluster"] == cid]

            # Adjusted Cox HR for belonging to this cluster vs others
            surv_flag = surv_sub.copy()
            surv_flag["in_cluster"] = (surv_flag["cluster"] == cid).astype(float)
            cox_c = _fit_cox(surv_flag, "os_months", "os_event", "in_cluster", ["stage_numeric", "age", "sex_male", "is_oncosg"])

            # Top upregulated & downregulated genes in this cluster
            means = {g: round(float(c_df[g].mean()), 2) for g in expr_cols if c_df[g].notna().any()}
            top_up = sorted(means.items(), key=lambda kv: -kv[1])[:6]
            top_dn = sorted(means.items(), key=lambda kv: kv[1])[:4]

            # Top enriched mutations
            mut_rates = {}
            for g in ("EGFR", "KRAS", "TP53", "STK11", "KEAP1", "SMARCA4", "RBM10"):
                if g in c_mut.columns and c_mut[g].notna().any():
                    mut_rates[g] = round(100.0 * float(c_mut[g].mean()), 1)

            profiles.append({
                "cluster_id": f"LUAD_SUBTYPE_{cid + 1}",
                "n_patients": int(len(c_df)),
                "cohort_mix": {k: int(v) for k, v in c_df["cohort"].value_counts().items()},
                "stage_iii_iv_pct": round(100.0 * float((c_df["stage_group"] == "III_IV").mean()), 1),
                "median_os_months": _safe_float(c_surv["os_months"].median(), 1) if len(c_surv) else None,
                "stage_cohort_adj_hr": cox_c["hr"] if cox_c else None,
                "stage_cohort_adj_p": round(cox_c["p"], 5) if cox_c else None,
                "top_upregulated_genes_z": top_up,
                "top_downregulated_genes_z": top_dn,
                "key_mutation_rates_pct": mut_rates,
                "adc_profile_mean_z": {
                    g: means.get(g) for g in ("TACSTD2", "ERBB2", "MET", "ERBB3", "EGFR", "CD276", "CEACAM5", "SLFN11", "ABCG2")
                },
            })

        return {
            "n_rna_patients_clustered": int(len(merged)),
            "optimal_k": best_k,
            "silhouette_score": round(best_sil, 3),
            "pca_variance_explained_top8": round(float(pca.explained_variance_ratio_[:8].sum()), 3),
            "overall_logrank_p": float(lr.p_value),
            "subtypes": profiles,
        }

    # ------------------------------------------------------------------
    # MODULE 6: Live PubMed Novelty Grounding
    # ------------------------------------------------------------------
    def ground_top_discoveries_pubmed(
        self,
        gold_trans: list[dict[str, Any]],
        top_survival: list[dict[str, Any]],
        max_queries: int = 12,
    ) -> list[dict[str, Any]]:
        grounded: list[dict[str, Any]] = []
        seen: set[str] = set()

        for item in gold_trans[:8]:
            key = f"{item['driver_mutation']}::{item['target_gene']}"
            if key in seen:
                continue
            seen.add(key)
            q = f'("{item["driver_mutation"]}"[Title/Abstract]) AND ("{item["target_gene"]}"[Title/Abstract]) AND ("lung adenocarcinoma"[Title/Abstract] OR "NSCLC"[Title/Abstract])'
            cnt = _pubmed_count(q)
            time.sleep(0.35)
            grounded.append({
                "kind": "GENOTYPE_TO_ADC_PROTEIN",
                "finding": f"{item['driver_mutation']} mutation -> {item['target_gene']} {item['direction']} (RNA beta={item['beta_rna_z']}, q={item['fdr_q_rna']:.1e}; CPTAC Protein beta={item['cptac_protein_beta_z']}, p={item['cptac_protein_p']})",
                "pubmed_query": q,
                "pubmed_papers": cnt,
                "novelty_tier": _novelty_tier(cnt),
            })

        for item in top_survival[: max(0, max_queries - len(grounded))]:
            key = f"SURV::{item['biomarker']}"
            if key in seen:
                continue
            seen.add(key)
            q = f'("{item["biomarker"]}"[Title/Abstract]) AND ("survival"[Title/Abstract] OR "prognosis"[Title/Abstract]) AND ("lung adenocarcinoma"[Title/Abstract])'
            cnt = _pubmed_count(q)
            time.sleep(0.35)
            grounded.append({
                "kind": "STAGE_ADJUSTED_SURVIVAL",
                "finding": f"{item['biomarker']} ({item['modality']}) -> Stage+Cohort-adjusted HR={item['hr_adj']} (95% CI {item['ci_low']}-{item['ci_high']}, q={item['fdr_q_adj']:.1e}, bootstrap={item.get('bootstrap_same_dir_pct')}%)",
                "pubmed_query": q,
                "pubmed_papers": cnt,
                "novelty_tier": _novelty_tier(cnt),
            })

        return grounded

    # ------------------------------------------------------------------
    # MODULE 7: Head-of-Science Deterministic Interrogation Tool (Zero LLM)
    # ------------------------------------------------------------------
    def query_subgroup_survival(
        self,
        *,
        mutated_gene: str | None = None,
        wildtype_gene: str | None = None,
        stage_group: str | None = None,
        split_by_gene_expression: str | None = None,
    ) -> dict[str, Any]:
        """Strict zero-hallucination interrogation tool with power floor (n < 15 -> ABSTAIN)."""
        df = (
            self.patients
            .merge(self.mutations, on=["patient_uid", "cohort"], how="left")
            .merge(self.rna, on=["patient_uid", "cohort"], how="left", suffixes=("_mut", "_rna"))
        )
        filters_desc = []
        if stage_group:
            df = df[df["stage_group"] == stage_group]
            filters_desc.append(f"stage_group={stage_group}")
        if mutated_gene:
            col = f"{mutated_gene}_mut" if f"{mutated_gene}_mut" in df.columns else mutated_gene
            df = df[df[col] == 1]
            filters_desc.append(f"{mutated_gene}=MUT")
        if wildtype_gene:
            col = f"{wildtype_gene}_mut" if f"{wildtype_gene}_mut" in df.columns else wildtype_gene
            df = df[df[col] == 0]
            filters_desc.append(f"{wildtype_gene}=WT")

        surv = df.dropna(subset=["os_months", "os_event"])
        surv = surv[surv["os_months"] > 0]
        if len(surv) < MIN_POWER_N:
            return {
                "status": "INSUFFICIENT_POWER",
                "reason": f"Only {len(surv)} patients with survival match ({', '.join(filters_desc)}); minimum required is {MIN_POWER_N}. Refusing to guess.",
                "n_patients": int(len(surv)),
            }

        out: dict[str, Any] = {
            "status": "SUPPORTED",
            "filters": filters_desc,
            "n_patients_with_os": int(len(surv)),
            "os_events": int(surv["os_event"].sum()),
            "median_os_months": _safe_float(surv["os_months"].median(), 1),
            "cohort_breakdown": {k: int(v) for k, v in surv["cohort"].value_counts().items()},
        }
        if split_by_gene_expression:
            col = f"{split_by_gene_expression}_rna" if f"{split_by_gene_expression}_rna" in surv.columns else split_by_gene_expression
            if col not in surv.columns:
                return {"status": "UNKNOWN", "reason": f"Biomarker '{split_by_gene_expression}' not in snapshot."}
            cox = _fit_cox(surv, "os_months", "os_event", col, ["stage_numeric", "age", "sex_male", "is_oncosg"])
            if not cox:
                return {"status": "INSUFFICIENT_POWER", "reason": "Insufficient events for multivariable Cox model."}
            out["biomarker_cox_adjusted"] = {"gene": split_by_gene_expression, **cox}
        return out

    def run_full_discovery(self, *, query_pubmed: bool = True) -> dict[str, Any]:
        comut = self.run_comutation_scan()
        surv = self.run_survival_discovery()
        geno_adc = self.run_genotype_to_adc_protein_scan()
        epist = self.run_epistatic_interactions()
        subtypes = self.run_unsupervised_subtypes()
        pubmed = (
            self.ground_top_discoveries_pubmed(
                geno_adc["gold_trans_discoveries"],
                surv["top_fdr_survival_hits"],
            )
            if query_pubmed
            else []
        )
        return {
            "snapshot_id": self.manifest["snapshot_id"],
            "snapshot_manifest_sha256": self.manifest["manifest_sha256"],
            "total_patients": int(len(self.patients)),
            "cohorts": self.manifest["cohorts"],
            "comutation_and_ethnic_shift": comut,
            "confounder_adjusted_survival": surv,
            "genotype_to_adc_and_payload_reprogramming": geno_adc,
            "epistatic_survival_interactions": epist,
            "unsupervised_subtypes": subtypes,
            "pubmed_novelty_grounding": pubmed,
        }
