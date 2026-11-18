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
