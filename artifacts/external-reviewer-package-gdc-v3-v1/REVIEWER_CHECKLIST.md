# Developer-Prepared External Reviewer Package: GDC TCGA-LUAD v3

> **Important Notice**: This package was **prepared by the EGIS developer** to support an independent third-party audit. EGIS's own self-checks (`independent_recheck_gdc_v3.py`, `reconcile_and_verify_gdc_v3.py`, and internal regression suites) are **not** independent external validation.

## 1. Exact Scope & Non-Claims

- **Scoped claim**: In frozen snapshot `snapshots/real/gdc_tcga_luad_v3` (`gdc-8d5ccf3bc2893baa`, GDC `Data Release 46.0 - August 10, 2026`), **`40` of `571`** `Primary Tumor` samples linked to released open-access WXS `Masked Somatic Mutation` (`MAF`) files have an observed somatic `EGFR` `p.L858R` or `p.E746_A750del` call on `GRCh38` canonical transcript `ENST00000275493` (`7.005253940455342%`: `22` `p.L858R` + `18` `p.E746_A750del`).
- **Does NOT establish**: TCGA-LUAD cohort-wide prevalence (another `504` of `1,075` `Primary Tumor` samples lack open WXS MAF files in this subset; `571` WXS-linked samples span `559` distinct cases with `12` cases contributing `2` WXS-linked `Primary Tumor` samples), EGFR wild-type / negative status (unmutated locus depth is absent and germline calls are masked), clinical validity, or production readiness.

## 2. Cryptographic & Provenance Targets

- **Snapshot directory**: `snapshots/real/gdc_tcga_luad_v3`
- **`raw_content_sha256`**: `8d5ccf3bc2893baaf5cfb5b01a374dd34c27601b5093b5e1368174f1232896c9`
- **`normalized_content_sha256`**: `57ea7cf8f33afc6c2361eb92f7a795b622548ea15c7c0c3a534c54f99a02eb49`
- **`sqlite_sha256`**: `b56a15ca9ae42accff15258262fbf49fbc5ce7ffce5cbfc4f43349dc5e564b48`
- **`manifest_sha256` (canonical)**: `71488379a4ed5c090e9d36bbdeca53bb44fe0922e1dda908edd463225f502dd1`
- **Public key (`Ed25519` demo keypair)**: `keys/public_key.pem` (SHA-256: `0cbc076591b360ac6c2ca2156467c4858a95a5ecab7f6b128c2c834045e0ba32`)
- **Git `HEAD` at package creation**: `658c77483e91c3cd6c9d0df355dd9de7aaffdfb9`

## 3. External Reviewer Verification Checklist

1. **Integrity & custody**:
   - Run `sha256sum -c SHA256SUMS.txt` inside `artifacts/external-reviewer-package-gdc-v3-v1/`.
   - Independently re-hash `snapshots/real/gdc_tcga_luad_v3/{raw.json,normalized.json,data.sqlite,manifest.json}` and verify canonical `manifest_sha256`.
   - Note that GDC HTTP wire response digests are recorded at acquisition time; `raw.json` preserves the parsed response payloads (`8d5ccf3b...`).
2. **Independent source acquisition check (recommended outside EGIS code)**:
   - Independently query `https://api.gdc.cancer.gov/status`, `https://api.gdc.cancer.gov/files` (TCGA-LUAD open SNV), and `https://api.gdc.cancer.gov/ssm_occurrences` (TCGA-LUAD EGFR), and inspect an open WXS MAF header to verify live GDC schema semantics (`GRCh38`, `ENST00000275493`, positive-only masked calls, `tumor_sample_uuid == aliquot_id`).
3. **Denominator & multiplicity check**:
   - Verify independently from `raw.json` (and without running `cancer_evidence`) that `571` distinct `Primary Tumor` samples (across `559` cases, `612` WXS MAF files; `21` samples have `>1` WXS MAF file) form `tcga_luad_gdc_r46_wxs_masked_ssm_primary_tumor_samples`.
4. **Witness & lineage check**:
   - Verify independently that `93` `EGFR` `/ssm_occurrences` map `93/93` 1-to-1 to tumor aliquot, sample, case, and WXS MAF file (`0` unmatched, `0` multiply matched), and that filtering to `GRCh38` + `ENST00000275493` + `hgvsp_short in {'p.L858R', 'p.E746_A750del'}` yields `40` samples, `40` `ssm_occurrence_id`s, `40` tumor aliquots, `40` WXS MAF files (`160` lineage UUIDs). Note `74bcf2d5-fd42-423e-bd96-b2de1b0cf778` has `2` WXS MAF aliquots with `p.L858R` on `92da1bb5-2e83-4f2f-88dd-afc0cb7d91d1`, and `4` `p.L858R` samples carry a second non-target `EGFR` variant.
5. **Abstention & independent replay**:
   - Inspect `abstaining_negative_query.json` (`UNKNOWN / INCOMPLETE_SNAPSHOT`) and `abstaining_unacquired_gene_query.json` (`KRAS` -> `UNKNOWN / INCOMPLETE_SNAPSHOT`).
   - Independently re-verify the Ed25519 signature on `supported_certificate.json` using `keys/public_key.pem`, read `src/cancer_evidence/replay.py` to audit independence from `engine.py`, and execute offline:
     ```bash
     EGIS_OFFLINE=1 .venv/bin/evidence verify \
       --snapshot snapshots/real/gdc_tcga_luad_v3 \
       --certificate artifacts/external-reviewer-package-gdc-v3-v1/supported_certificate.json
     ```
6. **Fresh independent holdout evaluation**:
   - Execute an external third-party evaluation against an untouched holdout suite (not the `48`-case development regression suite) and replace the `keys/` demo keypair with external custody before any deployment beyond internal research.
