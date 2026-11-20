# Human Reviewer Brief: EGIS GDC TCGA-LUAD v3 Demo

> **Important Notice — Developer-Prepared Brief (Not an External Audit)**
>
> This document and the Human-Verifier Demo UI (`artifacts/human-verifier-ui/index.html`) were **prepared by the EGIS developer** to support independent human inspection. Neither this brief, the UI, nor EGIS's self-reconciliation scripts (`scripts/independent_recheck_gdc_v3.py`, `scripts/reconcile_and_verify_gdc_v3.py`) or 48-case regression runs constitute an independent external audit.
>
> **Research Use Only:** EGIS is **not** clinically validated, cleared, or production-ready, and must **not** be used for patient-care or clinical decisions. Do **not** request, transmit, or rely on private signing keys or evaluator answer keys during review.

---

## 1. What EGIS Is & What This Demo Demonstrates

EGIS (`cancer-evidence-engine`) is a deterministic, non-generative evidence query engine operating strictly over versioned, frozen biomedical snapshots stored as canonical JSON + SQLite databases, accompanied by Ed25519-signed query certificates and an independent SQLite replay verifier (`src/cancer_evidence/replay.py`).

This featured Human-Verifier Demo inspects one narrow real-data workflow over frozen GDC `TCGA-LUAD` snapshot `snapshots/real/gdc_tcga_luad_v3` (`snapshot_id: gdc-8d5ccf3bc2893baa`, GDC `Data Release 46.0 - August 10, 2026`):

- **Supported subset observation (`40 / 571`, `7.005253940455342%`)**: Out of `571` distinct `Primary Tumor` samples linked to released open-access WXS `Masked Somatic Mutation` (`MAF`) files (`tcga_luad_gdc_r46_wxs_masked_ssm_primary_tumor_samples`), `40` distinct `Primary Tumor` samples have an observed somatic `EGFR` `p.L858R` (`22`) or `p.E746_A750del` (`18`) call in GDC `/ssm_occurrences` on `GRCh38` canonical transcript `ENST00000275493`.
- **Single counting per sample**: Each sample is counted once in the denominator (`571`) and once in the numerator (`40`) even though `21` file-linked `Primary Tumor` samples carry `>1` WXS MAF file and `13` EGFR-mutated samples carry compound `EGFR` variants.
- **Honest abstention (`UNKNOWN / INCOMPLETE_SNAPSHOT`)**: Querying `call_status: "NEGATIVE"` (`require_explicit_negative_coverage: true`) or querying an unacquired gene (`KRAS p.G12C`) returns `UNKNOWN / INCOMPLETE_SNAPSHOT` with `0` witnesses rather than inventing a negative count.
- **Offline replayability**: Both the supported certificate (`40 / 571`) and abstaining certificates replay independently offline against `data.sqlite`.

---

## 2. Exact Narrow Interpretation & What This Does NOT Demonstrate

- **Exact narrow interpretation of `40 / 571`**: *"Forty distinct `Primary Tumor` samples among `571` `Primary Tumor` samples linked via `aliquot_id` to a released open-access WXS Masked Somatic Mutation (`MAF`) file in `snapshots/real/gdc_tcga_luad_v3` had a recorded `EGFR` `p.L858R` or `p.E746_A750del` call on `GRCh38` / `ENST00000275493` in GDC `Data Release 46.0`."*
- **What it does NOT establish**:
  1. **Not TCGA-LUAD-wide prevalence or mutation rate**: The snapshot contains `1,075` `Primary Tumor` samples across `585` cases; `504` `Primary Tumor` samples (`26` cases entirely) lack open WXS MAF links in this subset, and `12` cases contribute `2` WXS-linked `Primary Tumor` samples each (`559` distinct cases).
  2. **Not wild-type / negative status**: GDC `/ssm_occurrences` and open-access WXS Masked Somatic Mutation MAFs record **positive called masked somatic variants only** (`POSITIVE_ONLY_OPEN_ACCESS_MAF`). Unmutated locus sequencing depth is absent and germline variants are masked, so **"not listed" never equals "wild-type (`NEGATIVE`)"**.
  3. **Not clinical validity, assay/QC completeness, or production readiness**: Biological assay QC, broad negative-call locus coverage, CPTAC biospecimen integration, governed cross-source identity mappings, external blind audit, and production key custody remain incomplete (`PENDING_EXTERNAL`).
  4. **Passing replay does not prove biology**: A passing `evidence verify` replay proves deterministic SQL reconstruction over `data.sqlite` and signature verification against `keys/public_key.pem`; it does not prove that upstream GDC annotations or cohort filters are biologically complete.

---

## 3. Artifacts & Files to Inspect (Read-Only, Public / Non-Secret Only)

Do **not** inspect or request `keys/private_key.pem` or any evaluator `answer_key.json`. Inspect only these artifacts and source files:

1. **Frozen snapshot (`snapshots/real/gdc_tcga_luad_v3/`)**:
   - `manifest.json` (`manifest_sha256: 71488379a4ed5c090e9d36bbdeca53bb44fe0922e1dda908edd463225f502dd1`)
   - `raw.json` (`raw_content_sha256: 8d5ccf3bc2893baaf5cfb5b01a374dd34c27601b5093b5e1368174f1232896c9`)
   - `normalized.json` (`normalized_content_sha256: 57ea7cf8f33afc6c2361eb92f7a795b622548ea15c7c0c3a534c54f99a02eb49`)
   - `data.sqlite` (`sqlite_sha256: b56a15ca9ae42accff15258262fbf49fbc5ce7ffce5cbfc4f43349dc5e564b48`)
2. **Query, Result, Certificate & Replay (`artifacts/gdc-luad-v3-molecular-milestone/` and `artifacts/external-reviewer-package-gdc-v3-v1/`)**:
   - `supported_query.json`, `supported_result.json`, `supported_certificate.json`, `supported_replay.json`
   - `abstaining_negative_query.json`, `abstaining_negative_result.json`, `abstaining_negative_certificate.json`, `abstaining_negative_replay.json`
   - `abstaining_unacquired_gene_query.json`, `abstaining_unacquired_gene_result.json`, `abstaining_unacquired_gene_replay.json`
3. **Public Verification Key**: `keys/public_key.pem`
4. **Independent Verification & UI Code**:
   - `src/cancer_evidence/replay.py` (clean-room SQL replay implementation; audit independence from `src/cancer_evidence/engine.py`)
   - `src/cancer_evidence/verifier_ui.py` & `scripts/run_verifier_ui.py`
   - `artifacts/gdc-luad-v3-independent-recheck-v1/independent_recheck_report.json` (project-authored recheck)

---

## 4. Independent Reviewer Verification Checklist

1. **GDC Source Semantics**: Independently query live GDC endpoints (`/status`, `/files`, `/ssm_occurrences`) outside EGIS scripts to confirm `GRCh38`, `ENST00000275493`, positive-only masked calling, and that `tumor_sample_uuid` in `/ssm_occurrences` corresponds to GDC tumor `aliquot_id`.
2. **Snapshot Hash Custody**: Recompute SHA-256 of `raw.json`, `normalized.json`, `data.sqlite`, and canonical `manifest.json` without using `cancer_evidence` code. Confirm `raw -> normalized` derivation deterministically matches `normalized.json`.
3. **Denominator Reconstruction (`1,075` → `571`)**: Write your own script or SQL query over `raw.json` / `data.sqlite` to confirm `1,075` `Primary Tumor` samples total and `571` distinct `Primary Tumor` samples linked to released open-access WXS `Masked Somatic Mutation` MAF files (spanning `559` cases and `612` files).
4. **Biospecimen & Variant Linkage**: Confirm that `93 / 93` `EGFR` `ssm_occurrences` link 1-to-1 (`aliquot_id` → `sample_id` → `case_id` → `file_id`, `0` unmatched, `0` multiply matched), and audit samples with multiple WXS MAF files (e.g., `74bcf2d5-fd42-423e-bd96-b2de1b0cf778`) and compound `EGFR` calls (`p.L858R` + `p.T790M` / `p.L62R`).
5. **Exact Witness Set (`40`)**: Confirm the union of `p.L858R` (`22`) and `p.E746_A750del` (`18`) yields the exact `40` sorted sample UUIDs, `40` `ssm_occurrence_id`s, `40` tumor aliquots, and `40` MAF files (`160` refs).
6. **Certificate Signature & Replay**: Audit `src/cancer_evidence/replay.py` for independence from `engine.py`, verify the Ed25519 signature on `supported_certificate.json` with `keys/public_key.pem`, and run:
   ```bash
   EGIS_OFFLINE=1 .venv/bin/evidence verify \
     --snapshot snapshots/real/gdc_tcga_luad_v3 \
     --certificate artifacts/gdc-luad-v3-molecular-milestone/supported_certificate.json
   ```
   Also tamper with a *copy* of `supported_certificate.json` (in `/tmp`, never modifying checked-in artifacts) to confirm `evidence verify` rejects altered counts, witnesses, or digests (`SIGNATURE_INVALID` / `RESULT_MISMATCH`).
7. **Abstention Check**: Confirm `abstaining_negative_query.json` and `abstaining_unacquired_gene_query.json` return `UNKNOWN / INCOMPLETE_SNAPSHOT` and also verify cleanly offline.

---

## 5. What to Report Back

Please report **discrepancies, hidden assumptions, edge cases, schema mismatches, or unverified claims**—not merely whether the Human-Verifier Demo UI renders cleanly or looks convincing. Specifically flag:
- Any case where `normalized.json` or `data.sqlite` diverges from `raw.json`;
- Any linkage ambiguity or denominator boundary you consider underspecified;
- Any wording in the UI or docs that could tempt a reader to over-interpret `40 / 571` as TCGA-LUAD prevalence, wild-type incidence, or a clinical conclusion.
