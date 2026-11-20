# Verification proof

All commands below were run from the repository root. The command outputs are
copied from the runs; no skipped check is represented as a pass.

## Offline demo and verification

Command:

```text
python scripts/run_demo.py --out artifacts/audit-demo
```

Output included:

```text
"status": "SUPPORTED"
"valid": true
CERTIFICATE_VERIFIED
```

Verification command:

```text
python -m cancer_evidence verify --snapshot artifacts/verified-demo/snapshot --certificate artifacts/verified-demo/certificate.json
{"reason_code": "NONE", "status": "SUPPORTED", "valid": true}
```

## W1 — replay matrix and totality

Command:

```text
python -c "import sys,unittest; sys.path.insert(0,'src'); s=unittest.defaultTestLoader.discover('tests', pattern='test_replay_matrix.py'); r=unittest.TextTestRunner(verbosity=0).run(s); raise SystemExit(not r.wasSuccessful())"
```

Output:

```text
Ran 6 tests ... OK
```

The matrix covers SUPPORTED, UNKNOWN, CONFLICT, AMBIGUOUS, OUT_OF_SCOPE, and
ERROR, plus fixture/cohort mismatch and malformed/null-signature certificates.

## W2 — independent replay and mutations

Command:

```text
python scripts/mutation_check.py
```

Decisive output:

```text
"kill_rate": 1.0
"killed": 4
"total": 4
```

The four source mutations are wrong denominator basis, incorrect set overlap,
skipped overlap conflict checking, and skipped availability conflict checking.

## W3 — raw/normalized derivation binding

Command:

```text
python -c "import sys,unittest; sys.path.insert(0,'src'); s=unittest.defaultTestLoader.discover('tests', pattern='test_derivation_binding.py'); r=unittest.TextTestRunner(verbosity=0).run(s); raise SystemExit(not r.wasSuccessful())"
```

Output:

```text
Ran 1 test ... OK
```

Editing `normalized.json` without re-running derivation returns
`DERIVATION_MISMATCH`.

## W4 — Ed25519 certificates

Command:

```text
python -c "import sys,unittest; sys.path.insert(0,'src'); s=unittest.defaultTestLoader.discover('tests', pattern='test_signatures.py'); r=unittest.TextTestRunner(verbosity=0).run(s); raise SystemExit(not r.wasSuccessful())"
```

Output:

```text
Ran 3 tests ... OK
```

The tests cover forged content hashes and attacker-supplied public-key paths.
The fixed public key is `keys/public_key.pem`.

## W5 — snapshot-derived denominator

Command:

```text
python -m pytest -q tests/test_denominator_universe.py
```

Output:

```text
2 passed
```

The eligible synthetic cohort has denominator 2 and numerator 1. A wrong
cohort returns UNKNOWN with ZERO_DENOMINATOR rather than a definitive zero.

## W6 — one runtime package

Command:

```text
python -c "from pathlib import Path; import subprocess; assert Path('src/cancer_evidence').exists(); r=subprocess.run(['git','grep','-n','evidence_engine','--','*.py','*.toml'], text=True, capture_output=True); assert r.returncode in (0,1) and not r.stdout; print('consolidated package present')"
```

Output:

```text
consolidated package present
```

The product runtime is `src/cancer_evidence`; historical fixture-only code is
under `legacy/synthetic_demo/`.

## W7 — frozen GDC snapshot

Acquisition command:

```text
python scripts/acquire_public_snapshots.py gdc --out snapshots/real/gdc_tcga_luad
```

Recorded manifest facts:

```text
source: gdc
scope: TCGA-LUAD
retrieved_count: 585
expected_total: 585
page_count: 3
complete: true
raw_content_sha256: 95ac037978e5c268d21bc07258f01089924fe19164d10ef6cd6aa8acbffc734d
```

The exact hash is stored in `snapshots/real/gdc_tcga_luad/manifest.json`; the
offline test verifies the count and the incomplete-page behavior.

Command:

```text
python -m pytest -q tests/test_real_snapshots.py
```

Output:

```text
3 passed
```

One captured page removed and completeness marked false returns
`UNKNOWN/INCOMPLETE_SNAPSHOT`, never a lower count.

## W8 — frozen PDC snapshot and namespace refusal

Acquisition command:

```text
python scripts/acquire_public_snapshots.py pdc --out snapshots/real/pdc_pdc000153
```

Recorded manifest facts:

```text
source: pdc
scope: PDC000153
retrieved_count: 115
expected_total: 115
page_count: 1
complete: true
```

The captured study response was checked for `PDC000153`, `Proteome`, and
`TMT10`.

Command:

```text
python scripts/compare_real_inventories.py snapshots/real/gdc_tcga_luad snapshots/real/pdc_pdc000153
```

Decisive output:

```json
{"gdc": {"count": 585}, "pdc": {"count": 115}, "cross_cohort_overlap": {"status": "UNKNOWN", "reason_code": "NO_APPROVED_MAPPING"}}
```

No GDC-to-PDC identity join is performed.

## W9 — full regression and gate ledger

Command:

```text
python -m pytest -q
```

Output:

```text
54 passed
```

Gate command:

```text
node "C:\Users\Ankith Mashettiwar\.agents\skills\unlazy\scripts\gate-check.mjs"
```

Output:

```text
ALL MET (34 met)
```

## Phases 1–6 Engineering & Research-Readiness Proof (2026-11-18)

### Unit, Integration, Schema, Claims, Provenance & Tamper Tests
Command:
```bash
.venv/bin/python -m pytest -q
```
Output:
```text
67 passed in 16.18s
```

### Expanded 8-Mutation Replay Resistance Check
Command:
```bash
.venv/bin/python scripts/mutation_check.py
```
Decisive output:
```json
{"kill_rate": 1.0, "killed": 8, "total": 8}
```

### Offline Real-Snapshot Readiness Audit
Command:
```bash
.venv/bin/python scripts/inspect_real_snapshots.py
```
Output written to `artifacts/real-data-readiness/real_snapshots_report.json`:
- `gdc_tcga_luad`: `585` cases verified & SQL-replayed (`valid: true, status: SUPPORTED`)
- `gdc_tcga_luad_v2`: `529/1075` `Primary Tumor` RNA-Seq TSV file availability verified & SQL-replayed (`valid: true, status: SUPPORTED`), plus unacquired assay/QC/biomarker abstention (`UNKNOWN / INCOMPLETE_SNAPSHOT`, `valid: true`)
- `pdc_pdc000153`: `115` cases verified & SQL-replayed (`valid: true, status: SUPPORTED`)
- Cross-source patient overlap refusal: `UNKNOWN / NO_APPROVED_MAPPING` (`valid: true`)

### 48-Case Regression Suite Run & Grading (Prior Blind Archive Preserved Untouched)
Commands:
```bash
.venv/bin/python scripts/run_blind_exam.py --exam-zip "/mnt/c/Users/Ankith Mashettiwar/.gemini/antigravity/scratch/cancer-evidence-exam-blind.zip" --out-dir artifacts/regression-exam-run --suite-role regression_suite
.venv/bin/python scripts/grade_blind_run.py --run-dir artifacts/regression-exam-run --out artifacts/regression-exam-grading/grading_report.json --suite-role regression_suite
```
Decisive output:
- Executed: `42 / 42` (`12/12` correctness, `14/14` abstention, `8/8` provenance, `8/8` tampering), `23/23` `CRITICAL` passed, `0` `NOT_RUN`, `6` pre-designated `BLOCKED` exclusions (`BLK-001`–`BLK-006`), `0` determinism failures across 3x isolated runs (`overall_verdict: "PASS_REGRESSION_SUITE"`).
- Prior frozen blind-run artifacts verified unchanged:
  - `artifacts/blind-exam-run/FROZEN_MANIFEST.json` `manifest_sha256`: `3e7e2f472b7bd64e9ceb2321e188ff6e3dfc74d49b880bc82708ac7de4a2fe1d` (`FAIL_INCOMPLETE_COVERAGE` in `artifacts/blind-exam-grading/grading_report.json`)
  - `artifacts/cor-001-blind-run/FROZEN_MANIFEST.json` `manifest_sha256`: `3835e5940f5ad577e720cfcaf5b6ce273999c0325e40c84e100a44cdfeda78ae`

### Untouched Clean-Room Holdout Benchmark Generation
Command:
```bash
.venv/bin/python scripts/build_cleanroom_holdout.py
```
Outputs staged (not executed or tuned against):
- `artifacts/cleanroom-holdout-benchmark/candidate-blind.zip` (`16` independent synthetic cases)
- `artifacts/cleanroom-holdout-benchmark/evaluator-private/answer_key.json`

## GDC TCGA-LUAD v2 Biospecimen & Sample-Level File Availability Milestone (2026-11-18)

### Networked Acquisition (Preserving GDC v1 and PDC v1)
Command:
```bash
.venv/bin/python scripts/acquire_public_snapshots.py gdc-v2 --out snapshots/real/gdc_tcga_luad_v2
```
Captured snapshot `gdc-89322a7056bca577` (`cohort_id: tcga_luad_gdc_r46_biospecimen_v2`, `retrieved_at_utc: 2026-11-18T23:48:54+00:00`, GDC `Data Release 46.0 - August 10, 2026`):
- `585` cases, `1781` samples (`1075` `Primary Tumor`), `7978` aliquots, `601` open RNA-Seq `Gene Expression Quantification` `TSV`/`released` files, `10360` relationships, `50932` evidence rows.
- `assays` table explicitly marked `PARTIAL_INGEST` (`0` rows).

### Offline Independent Source Reconciliation, CLI Certification & SQL Replay
Command:
```bash
.venv/bin/python scripts/reconcile_and_verify_gdc_v2.py
```
Decisive output written to `artifacts/gdc-luad-v2-milestone/source_reconciliation_report.json`:
- `raw_content_sha256_verified`: `true`, `reconciled`: `true`, `mismatches`: `[]`
- Supported sample-level file availability (`tcga_luad_gdc_r46_primary_tumor_samples`): `529 / 1075` (`49.2093023255814%`), `status: SUPPORTED`, `exact_witness_set_match: true`, `replay_valid: true`
- Abstaining assay/QC/biomarker query: `status: UNKNOWN`, `reason_code: INCOMPLETE_SNAPSHOT`, `witness_count: 0`, `replay_valid: true`
- Suite scope remains **research use only** (not clinically validated; CPTAC biospecimen capture, external holdout audit, and production key custody remain `PENDING_EXTERNAL`).

### Post-GDC-v2 Test Suite Summary
- `.venv/bin/python -m pytest -q`: `69 passed` (including real-snapshot GDC v2 supported & abstaining offline replay tests in `tests/test_real_snapshots.py`)
- `.venv/bin/python scripts/mutation_check.py`: `kill_rate: 1.0` (`8/8` mutations killed)

## GDC TCGA-LUAD v3 Molecular Evidence Milestone (2026-11-19 UTC)

### Networked Acquisition (Preserving GDC v1, GDC v2, and PDC v1)
Command:
```bash
.venv/bin/python scripts/acquire_public_snapshots.py gdc-v3 --out snapshots/real/gdc_tcga_luad_v3
```
Captured snapshot `gdc-8d5ccf3bc2893baa` (`cohort_id: tcga_luad_gdc_r46_molecular_v3`, `retrieved_at_utc: 2026-11-19T00:21:26+00:00`, GDC `Data Release 46.0 - August 10, 2026`):
- Reused frozen `gdc_tcga_luad_v2` foundation (`585` cases, `1781` samples [`1075` `Primary Tumor`], `7978` aliquots, `601` open RNA-Seq TSV files).
- Added all `618` open-access WXS `Masked Somatic Mutation` (`MAF`) files (`571` distinct `Primary Tumor` samples, `612` `Primary Tumor` aliquots; `0` orphan sample or aliquot links).
- Added all `93` `EGFR` `/ssm_occurrences` (`GRCh38`, canonical transcript `ENST00000275493`, `93/93` 1-to-1 linked to tumor aliquots and WXS MAF files; `0` unmatched, `0` multiply matched).
- Total row counts: `cases: 585`, `samples: 1781`, `aliquots: 7978`, `assays: 93`, `files: 1219`, `relationships: 11164`, `evidence: 63550`.

### Offline Independent Source Reconciliation, CLI Certification & SQL Replay
Command:
```bash
.venv/bin/python scripts/reconcile_and_verify_gdc_v3.py
```
Decisive output written to `artifacts/gdc-luad-v3-molecular-milestone/source_reconciliation_report.json`:
- `raw_content_sha256_verified`: `true`, `normalized_content_sha256_verified`: `true`, `reconciled`: `true`, `mismatches`: `[]`
- Supported classical activating `EGFR` somatic variants (`tcga_luad_gdc_r46_wxs_masked_ssm_primary_tumor_samples`, `hgvsp_short_in: ["p.L858R", "p.E746_A750del"]`, `ENST00000275493`, `GRCh38`): `40 / 571` (`7.005253940455342%`, `22` `p.L858R` + `18` `p.E746_A750del`), `exact_witness_set_match: true`, `exact_variant_record_id_match: true`, `exact_aliquot_id_match: true`, `exact_file_id_match: true`, `exact_evidence_refs_match: true` (`160` refs), `replay_valid: true`
- Wild-type (`NEGATIVE`, `require_explicit_negative_coverage: true`) abstention: `status: UNKNOWN`, `reason_code: INCOMPLETE_SNAPSHOT`, `witness_count: 0`, `replay_valid: true`
- Unacquired gene (`KRAS`) abstention: `status: UNKNOWN`, `reason_code: INCOMPLETE_SNAPSHOT`, `witness_count: 0`, `replay_valid: true`

### Post-GDC-v3 Test Suite & Mutation Check Summary
- `.venv/bin/python -m pytest -q`: `76 passed` (includes `tests/test_molecular_evidence.py` and GDC v3 tests in `tests/test_real_snapshots.py`)
- `.venv/bin/python scripts/mutation_check.py`: `kill_rate: 1.0` (`9/9` mutations killed, including `molecular_dropped_variant_witness`)
- Suite scope remains **research use only** (not clinically validated or production-ready; explicit negative wild-type calls require locus-level callable reference depth not present in open-access WXS Masked Somatic Mutation MAFs).
