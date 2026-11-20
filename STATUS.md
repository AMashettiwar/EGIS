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
| W9 Regression and proof artifacts | PASS | `.venv/bin/python -m pytest -q`: 51 passed (historical W9 baseline); Unlazy ledger: 34 met. |
| P1 Baseline & architecture contract | PASS | `docs/architecture_and_roadmap.md`; prior frozen artifacts preserved untouched. |
| P2 Trustworthy engine & 7 statuses | PASS | `src/cancer_evidence/{model,schema,snapshot,engine}.py`; `tests/test_claims_and_mappings.py`, `tests/test_provenance_and_tamper.py` (67 passed total). |
| P3 Real-data readiness verification | PASS (case summaries) | `artifacts/real-data-readiness/real_snapshots_report.json`; GDC TCGA-LUAD (585) + PDC PDC000153 (115) verified & replayed offline. |
| P3b Real TCGA-LUAD biospecimen/file hierarchy (GDC v2) | PASS (research-use milestone) | `snapshots/real/gdc_tcga_luad_v2` (585 cases, 1781 samples, 7978 aliquots, 601 open RNA-Seq TSV files) + `artifacts/gdc-luad-v2-milestone/source_reconciliation_report.json`; `529/1075` Primary Tumor sample file-availability & `UNKNOWN/INCOMPLETE_SNAPSHOT` assay abstention independently reconciled and SQL-replayed offline. |
| P3c Real TCGA-LUAD EGFR molecular evidence (GDC v3) | PASS (research-use milestone) | `snapshots/real/gdc_tcga_luad_v3` (adds 618 open WXS Masked Somatic Mutation MAF files covering 571 Primary Tumor samples + 93 EGFR `/ssm_occurrences`, GRCh38, `ENST00000275493`, 93/93 1-to-1 linked) + `artifacts/gdc-luad-v3-molecular-milestone/source_reconciliation_report.json`; `40/571` activating EGFR (`p.L858R` / `p.E746_A750del`) & wild-type/KRAS `UNKNOWN/INCOMPLETE_SNAPSHOT` abstentions independently reconciled and CLI SQL-replayed offline. |
| P4 Independent SQL replay & mutation | PASS | `src/cancer_evidence/replay.py` + `scripts/mutation_check.py`: 9/9 mutations killed (`kill_rate: 1.0`). |
| P5 Regression suite & clean-room holdout | PASS (regression) / PENDING_EXTERNAL (audit) | `artifacts/regression-exam-run` (`42/42` executed, `0` determinacy failures, `6` pre-designated `BLK-*`); `artifacts/cleanroom-holdout-benchmark/` staged untouched for external audit. |
| P6 Operational docs & release governance | PARTIAL / PENDING_EXTERNAL | `README.md`, `STATUS.md`, `PLAN.md`, `PROOF.md`, `docs/` updated; demo keypair rotation, CPTAC/PDC biospecimen capture, external blind audit, and clinical/regulatory review remain `PENDING_EXTERNAL`. |

## Remaining limitations

- EGIS is scoped to deterministic metadata and molecular-evidence **research use only** and is not clinically validated or production-ready.
- Checked-in GDC v1 (`585` cases) and PDC (`115` cases) snapshots capture case-level summary attributes only; GDC v2 (`snapshots/real/gdc_tcga_luad_v2`) adds linked `case -> sample -> aliquot -> file` records for open RNA-Seq TSV files (`0` `assays`, `pathology_reports`, or `identity_mappings` rows); GDC v3 (`snapshots/real/gdc_tcga_luad_v3`) adds open WXS Masked Somatic Mutation MAF files (`618`) and positive `EGFR` `/ssm_occurrences` (`93`) only, leaving explicit wild-type (`NEGATIVE`) calls, locus-level callable reference coverage, other genes, `pathology_reports`, and `identity_mappings` unacquired.
- CPTAC/PDC biospecimen/file integration, locus-level callable reference coverage, authoritative cross-cohort identity mappings, production signing key custody (`keys/` demo keypair replacement), independent external execution of `artifacts/cleanroom-holdout-benchmark/`, clinical/domain review, regulatory review, and release acceptance remain future / `PENDING_EXTERNAL`.
