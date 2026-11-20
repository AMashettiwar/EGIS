from __future__ import annotations

import html
import json
import math
from pathlib import Path
from typing import Any

from .models import CohortResult, PacketStatus

ADC_PREVIEW_TARGETS = ("TACSTD2", "ERBB2", "MET", "ERBB3", "EGFR", "CD276", "CEACAM5", "MUC1")


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0.0 or dy == 0.0:
        return None
    return round(num / (dx * dy), 3)


def compute_rna_protein_summary(result: CohortResult) -> list[dict[str, Any]]:
    """Compute real cohort-level RNA TPM vs Mass-Spec Protein Log2 discordance on READY patients."""
    ready = [p for p in result.packets if p.status == PacketStatus.READY]
    rows: list[dict[str, Any]] = []
    for gene in ADC_PREVIEW_TARGETS:
        pairs: list[tuple[str, float, float]] = []
        for pkt in ready:
            rna_mod = pkt.modalities.get("rna")
            prot_mod = pkt.modalities.get("proteomics")
            if not rna_mod or not prot_mod:
                continue
            tpm = (rna_mod.features.get("tpm") or {}).get(gene)
            prot = (prot_mod.features.get("protein_log2_ratio") or {}).get(gene)
            if tpm is not None and prot is not None:
                pairs.append((pkt.canonical_patient_id, math.log2(float(tpm) + 1.0), float(prot)))
        if not pairs:
            continue
        rnas = [p[1] for p in pairs]
        prots = [p[2] for p in pairs]
        med_rna = sorted(rnas)[len(rnas) // 2]
        med_prot = sorted(prots)[len(prots) // 2]
        discordant = sum(
            1
            for _, r_val, p_val in pairs
            if (r_val >= med_rna and p_val < med_prot) or (r_val < med_rna and p_val >= med_prot)
        )
        rows.append({
            "gene": gene,
            "paired_patients": len(pairs),
            "median_log2_tpm": round(med_rna, 2),
            "median_prot_log2": round(med_prot, 2),
            "pearson_r": _pearson(rnas, prots),
            "discordant_count": discordant,
            "discordant_pct": round(100.0 * discordant / len(pairs), 1),
        })
    return rows


def render_multimodal_demo_html(
    primary_result: CohortResult,
    strict_ts_result: CohortResult | None = None,
) -> str:
    """Render a self-contained, zero-dependency, read-only 10-minute R&D Demo HTML page."""
    rna_prot = compute_rna_protein_summary(primary_result)
    e = html.escape

    funnel_rows = []
    for st in primary_result.funnel:
        pct = (
            f"{(100.0 * st.remaining / primary_result.total_raw_records):.1f}%"
            if primary_result.total_raw_records
            else "0%"
        )
        funnel_rows.append(
            f"<tr><td><code>{e(st.stage)}</code></td>"
            f"<td class='num'><strong>{st.remaining}</strong></td>"
            f"<td class='num'>{st.dropped}</td>"
            f"<td class='num'>{pct}</td>"
            f"<td><span class='pill'>{e(st.reason)}</span></td></tr>"
        )

    reason_rows = "".join(
        f"<tr><td><code>{e(k)}</code></td><td class='num'><strong>{v}</strong></td></tr>"
        for k, v in sorted(primary_result.reason_breakdown.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    target_rows = "".join(
        f"<tr><td><strong>{e(r['gene'])}</strong></td>"
        f"<td class='num'>{r['paired_patients']}</td>"
        f"<td class='num'>{r['median_log2_tpm']}</td>"
        f"<td class='num'>{r['median_prot_log2']}</td>"
        f"<td class='num'>{r['pearson_r'] if r['pearson_r'] is not None else 'N/A'}</td>"
        f"<td class='num'><strong>{r['discordant_count']} ({r['discordant_pct']}%)</strong></td></tr>"
        for r in rna_prot
    )

    # Show first 15 READY + first 10 blocked packets cleanly in HTML table (full set in JSON/Parquet)
    ready_pkts = [p for p in primary_result.packets if p.status == PacketStatus.READY]
    blocked_pkts = [p for p in primary_result.packets if p.status != PacketStatus.READY]
    sample_pkts = ready_pkts[:15] + blocked_pkts[:10]

    packet_rows = []
    for pkt in sample_pkts:
        badge_cls = "ok" if pkt.status == PacketStatus.READY else ("warn" if pkt.status == PacketStatus.UNKNOWN else "err")
        reasons = ", ".join(r.value for r in pkt.reason_codes)
        trop2_rna = ((pkt.modalities.get("rna").features.get("tpm") or {}).get("TACSTD2")) if "rna" in pkt.modalities else None
        trop2_prot = ((pkt.modalities.get("proteomics").features.get("protein_log2_ratio") or {}).get("TACSTD2")) if "proteomics" in pkt.modalities else None
        her2_prot = ((pkt.modalities.get("proteomics").features.get("protein_log2_ratio") or {}).get("ERBB2")) if "proteomics" in pkt.modalities else None
        muts = ", ".join((pkt.modalities["somatic_mutations"].features.get("mutated_genes") or [])[:4]) if "somatic_mutations" in pkt.modalities else "—"
        packet_rows.append(
            f"<tr><td><code>{e(pkt.canonical_patient_id)}</code></td>"
            f"<td><span class='badge {badge_cls}'>{e(pkt.status.value)}</span></td>"
            f"<td>{e(pkt.ajcc_stage or '—')}</td>"
            f"<td><code>{e(pkt.alignment_mode)}</code></td>"
            f"<td class='num'>{f'{trop2_rna:.1f}' if trop2_rna is not None else '—'}</td>"
            f"<td class='num'>{f'{trop2_prot:+.2f}' if trop2_prot is not None else '—'}</td>"
            f"<td class='num'>{f'{her2_prot:+.2f}' if her2_prot is not None else '—'}</td>"
            f"<td><code>{e(muts or 'none in panel')}</code></td>"
            f"<td><code>{e(reasons)}</code></td></tr>"
        )

    strict_block = ""
    if strict_ts_result is not None:
        strict_block = (
            f"<div class='callout'>"
            f"<strong>Temporal Honesty Proof (Strict Timestamp-Only Policy):</strong> "
            f"When <code>allow_shared_specimen_fallback=False</code> is enforced on this same snapshot, "
            f"EGIS refuses to guess missing collection dates across GDC and PDC: "
            f"<strong>READY = {strict_ts_result.ready_count}</strong>, "
            f"<strong>UNKNOWN = {strict_ts_result.unknown_count}</strong> "
            f"(<code>{e(json.dumps(strict_ts_result.reason_breakdown, sort_keys=True))}</code>)."
            f"</div>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';" />
  <title>EGIS Multimodal Umbrella — Real CPTAC-3 Lung Cancer Demo</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; line-height: 1.5; }}
    .wrap {{ max-width: 1180px; margin: 0 auto; padding: 28px 24px 64px; }}
    h1 {{ margin: 0 0 8px; font-size: 26px; color: #f8fafc; }}
    h2 {{ margin: 28px 0 12px; font-size: 18px; color: #93c5fd; border-bottom: 1px solid #1e293b; padding-bottom: 6px; }}
    .sub {{ color: #94a3b8; margin-bottom: 20px; font-size: 14px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 18px 0; }}
    .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 14px 16px; }}
    .card .lbl {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #94a3b8; }}
    .card .val {{ font-size: 28px; font-weight: 700; margin-top: 4px; color: #f8fafc; }}
    .card .hint {{ font-size: 12px; color: #cbd5e1; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; margin-top: 8px; font-size: 13px; }}
    th, td {{ padding: 9px 12px; border-bottom: 1px solid #334155; text-align: left; }}
    th {{ background: #0b1220; color: #94a3b8; font-weight: 600; }}
    td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    code {{ background: #0b1220; padding: 2px 5px; border-radius: 4px; font-size: 12px; color: #cbd5e1; }}
    .pill {{ background: #334155; color: #e2e8f0; padding: 2px 7px; border-radius: 999px; font-size: 11px; }}
    .badge {{ padding: 2px 8px; border-radius: 999px; font-weight: 600; font-size: 11px; }}
    .badge.ok {{ background: #064e3b; color: #6ee7b7; }}
    .badge.warn {{ background: #78350f; color: #fde68a; }}
    .badge.err {{ background: #7f1d1d; color: #fecaca; }}
    .callout {{ background: #172554; border-left: 4px solid #3b82f6; padding: 12px 16px; border-radius: 6px; margin: 16px 0; font-size: 13px; }}
    .foot {{ margin-top: 32px; font-size: 12px; color: #64748b; }}
  </style>
</head>
<body>
<div class="wrap">
  <h1>EGIS Multimodal Evidence Umbrella — Lung Cancer (CPTAC-3 + PDC000153)</h1>
  <div class="sub">
    Zero-LLM deterministic cross-silo unification over real public NCI GDC + NCI PDC cohorts ·
    Snapshot <code>{e(primary_result.snapshot_id)}</code> ·
    Manifest SHA-256 <code>{e(primary_result.snapshot_manifest_sha256[:16])}...</code>
  </div>

  <div class="grid">
    <div class="card">
      <div class="lbl">Raw Cross-Silo Records</div>
      <div class="val">{primary_result.total_raw_records}</div>
      <div class="hint">{primary_result.excluded_controls} lab reference pools auto-excluded</div>
    </div>
    <div class="card">
      <div class="lbl">Eligible LUAD Patients</div>
      <div class="val">{primary_result.eligible_human_patients}</div>
      <div class="hint">Locked cohort denominator (non-control LUAD)</div>
    </div>
    <div class="card">
      <div class="lbl">4-Modality READY Packets</div>
      <div class="val" style="color:#6ee7b7">{primary_result.ready_count}</div>
      <div class="hint">Clinical + RNA + WXS + Proteomics aligned</div>
    </div>
    <div class="card">
      <div class="lbl">Honest Abstentions / Conflicts</div>
      <div class="val" style="color:#fde68a">{primary_result.unknown_count} / {primary_result.conflict_count}</div>
      <div class="hint">UNKNOWN / CONFLICT — zero silent guesses</div>
    </div>
  </div>

  <div class="callout">
    <strong>Locked Denominator:</strong> {e(primary_result.denominator_definition)}
  </div>
  {strict_block}

  <h2>1. Cross-Silo Unification Funnel (GDC + PDC)</h2>
  <table>
    <thead><tr><th>Gate Stage</th><th class="num">Remaining</th><th class="num">Dropped</th><th class="num">% of Raw</th><th>Gate Rule</th></tr></thead>
    <tbody>{''.join(funnel_rows)}</tbody>
  </table>

  <h2>2. Exclusion &amp; Abstention Breakdown</h2>
  <table>
    <thead><tr><th>Gate Reason Code</th><th class="num">Patients Blocked</th></tr></thead>
    <tbody>{reason_rows}</tbody>
  </table>

  <h2>3. Real Lung ADC Targets: RNA (GDC) vs. Measured Protein (PDC000153)</h2>
  <p class="sub">Computed strictly on the <strong>{primary_result.ready_count}</strong> verified 4-modality READY patients (Primary Tumor specimen-aligned). High discordance shows why RNA/DNA alone cannot replace direct protein measurement for ADC patient selection.</p>
  <table>
    <thead><tr><th>Target Gene</th><th class="num">Paired READY Patients</th><th class="num">Median log2(TPM+1)</th><th class="num">Median Protein log2</th><th class="num">Pearson r</th><th class="num">Median-Split Discordant</th></tr></thead>
    <tbody>{target_rows}</tbody>
  </table>

  <h2>4. Sample Per-Patient Multimodal Feature Packets (First 15 READY + 10 Blocked)</h2>
  <table>
    <thead><tr><th>Patient ID</th><th>Status</th><th>Stage</th><th>Alignment Mode</th><th class="num">TROP2 RNA (TPM)</th><th class="num">TROP2 Prot (log2)</th><th class="num">HER2 Prot (log2)</th><th>Panel Mutations</th><th>Gate Reason</th></tr></thead>
    <tbody>{''.join(packet_rows)}</tbody>
  </table>

  <div class="foot">
    Research-use deterministic engine (zero LLM, offline frozen Parquet/DuckDB snapshot). Public NCI GDC CPTAC-3 &amp; NCI PDC PDC000153 data only; not clinically validated.
  </div>
</div>
</body>
</html>
"""


def write_multimodal_demo_ui(
    primary_result: CohortResult,
    out_path: str | Path,
    strict_ts_result: CohortResult | None = None,
) -> Path:
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_multimodal_demo_html(primary_result, strict_ts_result), encoding="utf-8")
    return target
