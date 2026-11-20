from __future__ import annotations

import html
from pathlib import Path
from typing import Any


def render_discovery_html(report: dict[str, Any]) -> str:
    e = html.escape
    cm = report["comutation_and_ethnic_shift"]
    sv = report["confounder_adjusted_survival"]
    ga = report["genotype_to_adc_and_payload_reprogramming"]
    ep = report["epistatic_survival_interactions"]
    st = report["unsupervised_subtypes"]
    pm = report.get("pubmed_novelty_grounding") or []

    cohort_rows = "".join(
        f"<tr><td><strong>{e(k)}</strong></td><td><code>{e(v['study_id'])}</code></td>"
        f"<td class='num'><strong>{v['patients']}</strong></td><td class='num'>{v['with_wxs']}</td>"
        f"<td class='num'>{v['with_rna']}</td><td class='num'>{v['with_proteomics']}</td></tr>"
        for k, v in report["cohorts"].items()
    )

    comut_rows = "".join(
        f"<tr><td><strong>{e(r['gene_a'])} &times; {e(r['gene_b'])}</strong></td>"
        f"<td><span class='badge {'ok' if r['relationship']=='CO_OCCURRING' else 'warn'}'>{e(r['relationship'])}</span></td>"
        f"<td class='num'>{r['n_both']} / {r['n_total']}</td>"
        f"<td class='num'><strong>{r['odds_ratio']}</strong></td>"
        f"<td class='num'><code>{e(r['q_fmt'])}</code></td>"
        f"<td class='num'>{r['replicated_cohorts']} / 3</td></tr>"
        for r in cm["top_fdr_pairs"][:12]
    )

    surv_rows = "".join(
        f"<tr><td><strong>{e(h['biomarker'])}</strong></td><td><code>{e(h['modality'])}</code></td>"
        f"<td class='num'>{h['n_adj']} ({h['events']})</td>"
        f"<td class='num'><strong>{h['hr_adj']}</strong> ({h['ci_low']}&ndash;{h['ci_high']})</td>"
        f"<td class='num'><code>{h['fdr_q_adj']:.2e}</code></td>"
        f"<td class='num'>{'YES' if h['direction_replicated_both_cohorts'] else 'NO'}</td>"
        f"<td class='num'>{h.get('bootstrap_same_dir_pct', '—')}%</td></tr>"
        for h in sv["top_fdr_survival_hits"][:12]
    )

    kill_rows = "".join(
        f"<tr><td><strong>{e(k['biomarker'])}</strong></td><td><code>{e(k['modality'])}</code></td>"
        f"<td class='num'>HR={k['hr_unadj']} (p={k['p_unadj']:.4f})</td>"
        f"<td class='num'>HR={k['hr_adj']} (p={k['p_adj']:.4f})</td>"
        f"<td><span class='badge err'>CONFOUNDED BY STAGE/COHORT</span></td></tr>"
        for k in sv["top_confounder_kills"][:8]
    )

    gold_rows = "".join(
        f"<tr><td><strong>{e(g['driver_mutation'])}</strong></td><td><strong>{e(g['target_gene'])}</strong></td>"
        f"<td><span class='badge {'ok' if g['direction']=='UPREGULATED' else 'warn'}'>{e(g['direction'])}</span></td>"
        f"<td class='num'>{g['beta_rna_z']:+.2f} (q={g['fdr_q_rna']:.1e})</td>"
        f"<td class='num'>{g['purity_adj_beta']:+.2f} (p={g['purity_adj_p']:.4f})</td>"
        f"<td class='num'><strong>{g['cptac_protein_beta_z']:+.2f}</strong> (p={g['cptac_protein_p']:.4f})</td></tr>"
        for g in ga["gold_trans_discoveries"][:15]
    )

    subtype_rows = "".join(
        f"<tr><td><strong>{e(s['cluster_id'])}</strong></td><td class='num'>{s['n_patients']}</td>"
        f"<td><code>{e(str(s['cohort_mix']))}</code></td>"
        f"<td class='num'><strong>{s['stage_cohort_adj_hr']}</strong> (p={s['stage_cohort_adj_p']})</td>"
        f"<td><code>{e(', '.join(x[0] for x in s['top_upregulated_genes_z'][:4]))}</code></td>"
        f"<td><code>{e(', '.join(f'{k}:{v}%' for k,v in list(s['key_mutation_rates_pct'].items())[:4]))}</code></td></tr>"
        for s in st["subtypes"]
    )

    pubmed_rows = "".join(
        f"<tr><td><span class='badge {'ok' if p['novelty_tier']=='UNDER_EXPLORED_CANDIDATE' else ('warn' if p['novelty_tier']=='EMERGING_SIGNAL' else 'muted')}'>{e(p['novelty_tier'])}</span></td>"
        f"<td class='num'><strong>{e(str(p['pubmed_papers']))}</strong></td>"
        f"<td>{e(p['finding'])}</td></tr>"
        for p in pm
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';" />
  <title>EGIS Lung Adenocarcinoma Discovery Report (n={report['total_patients']})</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; background: #0b1120; color: #e2e8f0; line-height: 1.5; }}
    .wrap {{ max-width: 1220px; margin: 0 auto; padding: 28px 24px 64px; }}
    h1 {{ margin: 0 0 6px; font-size: 26px; color: #f8fafc; }}
    h2 {{ margin: 30px 0 10px; font-size: 18px; color: #93c5fd; border-bottom: 1px solid #1e293b; padding-bottom: 6px; }}
    .sub {{ color: #94a3b8; font-size: 13px; margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 18px 0; }}
    .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 14px 16px; }}
    .card .lbl {{ font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: #94a3b8; }}
    .card .val {{ font-size: 26px; font-weight: 700; margin-top: 4px; color: #f8fafc; }}
    .card .hint {{ font-size: 12px; color: #cbd5e1; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; margin-top: 8px; font-size: 13px; }}
    th, td {{ padding: 8px 11px; border-bottom: 1px solid #334155; text-align: left; }}
    th {{ background: #0f172a; color: #94a3b8; font-weight: 600; }}
    td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    code {{ background: #0f172a; padding: 2px 5px; border-radius: 4px; font-size: 12px; color: #cbd5e1; }}
    .badge {{ padding: 2px 7px; border-radius: 999px; font-weight: 600; font-size: 11px; }}
    .badge.ok {{ background: #064e3b; color: #6ee7b7; }}
    .badge.warn {{ background: #78350f; color: #fde68a; }}
    .badge.err {{ background: #7f1d1d; color: #fecaca; }}
    .badge.muted {{ background: #334155; color: #cbd5e1; }}
  </style>
</head>
<body>
<div class="wrap">
  <h1>EGIS Confounder-Aware Lung Adenocarcinoma Discovery Engine (n = {report['total_patients']})</h1>
  <div class="sub">Zero-LLM statistical discovery across 3 independent cohorts (TCGA PanCancer + OncoSG East Asian + CPTAC Deep Proteome) · Manifest <code>{e(report['snapshot_manifest_sha256'][:16])}...</code></div>

  <div class="grid">
    <div class="card"><div class="lbl">Real LUAD Patients</div><div class="val">{report['total_patients']}</div><div class="hint">TCGA (566) + OncoSG (305) + CPTAC (110)</div></div>
    <div class="card"><div class="lbl">Stage-Adjusted Survival Hits</div><div class="val" style="color:#6ee7b7">{sv['fdr_q05_adjusted_survivors']}</div><div class="hint">{sv['confounder_killed_count']} naive hits killed by Stage/Cohort control</div></div>
    <div class="card"><div class="lbl">Gold Genotype &rarr; ADC Protein</div><div class="val" style="color:#93c5fd">{ga['gold_purity_and_protein_replicated_hits']}</div><div class="hint">FDR q&lt;0.05 + Purity-Adj + CPTAC Mass-Spec Replicated</div></div>
    <div class="card"><div class="lbl">Unsupervised Subtypes</div><div class="val" style="color:#fde68a">k = {st['optimal_k']}</div><div class="hint">Log-rank p = {st['overall_logrank_p']:.2e} (blind to labels)</div></div>
  </div>

  <h2>Cohort Coverage</h2>
  <table><thead><tr><th>Cohort</th><th>Study ID</th><th class="num">Patients</th><th class="num">WXS</th><th class="num">RNA</th><th class="num">CPTAC Protein</th></tr></thead><tbody>{cohort_rows}</tbody></table>

  <h2>1. Co-Mutation &amp; Mutual Exclusivity (FDR Filtered: {cm['false_positives_filtered_by_fdr']} Naive False Positives Removed)</h2>
  <table><thead><tr><th>Gene Pair</th><th>Relation</th><th class="num">Double Mut</th><th class="num">Odds Ratio</th><th class="num">FDR q</th><th class="num">Replicated Cohorts</th></tr></thead><tbody>{comut_rows}</tbody></table>

  <h2>2. Confounder-Adjusted Overall Survival (Stage + Age + Sex + Cohort Adjusted Cox PH)</h2>
  <table><thead><tr><th>Biomarker</th><th>Modality</th><th class="num">n (Events)</th><th class="num">Adjusted HR (95% CI)</th><th class="num">FDR q</th><th class="num">Western+Asian Same Dir</th><th class="num">Bootstrap</th></tr></thead><tbody>{surv_rows}</tbody></table>

  <h2>2B. Confounder Kills (Naive Hits That COLLAPSED After Stage/Cohort Control)</h2>
  <table><thead><tr><th>Biomarker</th><th>Modality</th><th class="num">Unadjusted</th><th class="num">Adjusted</th><th>Audit Verdict</th></tr></thead><tbody>{kill_rows}</tbody></table>

  <h2>3. GOLD Genotype &rarr; ADC Target &amp; Payload-Sensitivity Reprogramming (FDR + Purity + CPTAC Mass-Spec Protein Replicated)</h2>
  <table><thead><tr><th>Driver Mutation</th><th>ADC / Payload Target</th><th>Effect</th><th class="num">Pooled RNA &beta; (FDR q)</th><th class="num">Purity-Adj &beta; (p)</th><th class="num">CPTAC Mass-Spec Protein &beta; (p)</th></tr></thead><tbody>{gold_rows}</tbody></table>

  <h2>4. Unsupervised Multi-Cohort Subtypes (Discovered Blindly Across All 3 Cohorts)</h2>
  <table><thead><tr><th>Subtype</th><th class="num">n</th><th>Cohort Mix (Anti-Batch Check)</th><th class="num">Stage+Cohort Adj HR</th><th>Top Upregulated Genes</th><th>Key Mutations</th></tr></thead><tbody>{subtype_rows}</tbody></table>

  <h2>5. Live PubMed Novelty Grounding (Known Sanity Checks vs. Emerging / Under-Explored Signals)</h2>
  <table><thead><tr><th>Novelty Tier</th><th class="num">PubMed Papers</th><th>Audited Statistical Finding</th></tr></thead><tbody>{pubmed_rows}</tbody></table>
</div>
</body>
</html>
"""


def write_discovery_html(report: dict[str, Any], out_path: str | Path) -> Path:
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_discovery_html(report), encoding="utf-8")
    return target
