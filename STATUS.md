# Work-item status

| Work item | Status | Proof |
|---|---|---|
| W1 Replay totality and status round-trip | PASS | `tests/test_replay_matrix.py`; 4 tests OK; all six statuses and mismatch cases replay validly. |
| W2 Independent replay and mutation resistance | PASS | `scripts/mutation_check.py`; 4/4 mutations killed. |
| W3 Snapshot raw/normalized derivation binding | PASS | `tests/test_derivation_binding.py`; normalized edit returns `DERIVATION_MISMATCH`. |
| W4 Ed25519 certificate signing | PASS | `tests/test_signatures.py`; 3 tests OK; fixed `keys/public_key.pem` used. |
| W5 Snapshot-derived denominators | PASS | `tests/test_denominator_universe.py`; eligible denominator and wrong-cohort abstention covered. |
| W6 One runtime package | PASS | Product is under `src/cancer_evidence`; old fixture code is under `legacy/synthetic_demo`. |
| W7 Frozen GDC TCGA-LUAD snapshot | PASS | `snapshots/real/gdc_tcga_luad`; 585 cases, 3 pages, offline replay and incomplete-page test. |
| W8 Frozen PDC PDC000153 snapshot | PASS | `snapshots/real/pdc_pdc000153`; 115 cases; separate namespace; overlap refusal is `UNKNOWN/NO_APPROVED_MAPPING`. |
| W9 Regression and proof artifacts | PASS | `python -m pytest -q`: 51 passed; Unlazy ledger: 34 met. |

## Remaining limitations

Blind external evaluation, clinical/domain review, regulatory review, and
release acceptance were not performed. They remain `PENDING_EXTERNAL` and are
not claimed as passed.
