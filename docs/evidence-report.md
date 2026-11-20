# Evidence report

## Commands

The commands used for the offline candidate are recorded here after execution. The authoritative gate evidence is in `GATES.md` and the leaf ledgers.

Commands actually run:

```powershell
python -m unittest discover -s tests -v
python scripts/run_demo.py --out artifacts/verified-demo

Frozen public snapshot evidence (acquired 2026-11-18 UTC):

- `snapshots/real/gdc_tcga_luad/manifest.json`: 585 TCGA-LUAD cases, 3 pages, complete.
- `snapshots/real/pdc_pdc000153/manifest.json`: 115 PDC000153 cases, 1 GraphQL response, complete.
- `scripts/compare_real_inventories.py` reports the inventories side by side and returns `UNKNOWN/NO_APPROVED_MAPPING` for cross-cohort overlap.
- Blind evaluation, clinical review, and release acceptance remain `PENDING_EXTERNAL`.
& 'C:\Users\Ankith Mashettiwar\AppData\Local\Programs\Python\Python311\python.exe' -m pytest -q
```

Observed results: unittest `Ran 34 tests ... OK`; pytest `34 passed in 3.83s` using the system Python 3.11 interpreter. The default hermes `python` interpreter has no `pip`/`pytest`, so the portable gate ledger uses unittest-compatible checks; `requirements.lock` records `pytest==8.3.3` for the development environment.

## External status

- GDC real-source snapshot v1: frozen case-summary public metadata is present under `snapshots/real/gdc_tcga_luad/` (`585` TCGA-LUAD cases, `3` pages; preserved untouched).
- GDC real-source snapshot v2: frozen biospecimen and file metadata is present under `snapshots/real/gdc_tcga_luad_v2/` (`585` TCGA-LUAD cases, `1781` samples [`1075` `Primary Tumor`], `7978` aliquots, `601` open RNA-Seq `Gene Expression Quantification` `TSV`/`released` files; GDC `Data Release 46.0 - August 10, 2026`; preserved untouched). `scripts/reconcile_and_verify_gdc_v2.py` reconciles `529/1075` `Primary Tumor` RNA-Seq file availability (`49.2093%`) offline against `raw.json`, verifies offline CLI Ed25519 certification and independent SQL replay (`valid: true`), and verifies `UNKNOWN / INCOMPLETE_SNAPSHOT` abstention on unacquired assay/QC/biomarker queries (`artifacts/gdc-luad-v2-milestone/`).
- GDC real-source snapshot v3: frozen molecular evidence snapshot is present under `snapshots/real/gdc_tcga_luad_v3/` (`Data Release 46.0 - August 10, 2026`, `585` cases, `1781` samples, `7978` aliquots, `1219` files [`601` RNA-Seq TSV + `618` open WXS `Masked Somatic Mutation` MAF covering `571` `Primary Tumor` samples], and `93` `EGFR` `/ssm_occurrences` in `GRCh38` / `ENST00000275493`, 93/93 1-to-1 linked). `scripts/reconcile_and_verify_gdc_v3.py` reconciles `40/571` `Primary Tumor` samples (`7.005253940455342%`: `22` `p.L858R` + `18` `p.E746_A750del`) with `160` lineage evidence IDs offline against `raw.json`, verifies offline CLI Ed25519 certification and independent SQL replay (`valid: true`), and verifies `UNKNOWN / INCOMPLETE_SNAPSHOT` abstention for wild-type (`NEGATIVE`) requests without callable locus coverage and unacquired genes (`KRAS`) (`artifacts/gdc-luad-v3-molecular-milestone/`).
- PDC real-source snapshot: frozen case-summary public metadata is present under `snapshots/real/pdc_pdc000153/` (`115` `PDC000153` CPTAC-LUAD cases, `Proteome` / `TMT10`); sample/aliquot/assay capture remains future networked acquisition work.
- Offline real-snapshot verification report: `artifacts/real-data-readiness/real_snapshots_report.json` (`scripts/inspect_real_snapshots.py`).
- Independent hidden evaluator: prior 48-case archive was inspected during grading and is now treated as a development/regression suite (`artifacts/regression-exam-run`); a fresh untouched holdout benchmark (`artifacts/cleanroom-holdout-benchmark`) is staged for future independent third-party audit (`PENDING_EXTERNAL`). Production key custody also remains `PENDING_EXTERNAL`.
- Public synthetic tests: status is determined by the commands above; synthetic results must not be reported as portal results.

## Declared public test strata

The public suite covers exact structured queries, source-scope rejection, zero denominators, incomplete snapshots, cross-source relationship rejection, lineage, tampered certificates, replay scope tampering, unsupported operations, CLI smoke behavior, real GDC v1/v2 & PDC checks, and network-disabled candidate replay. Free-text ambiguity/negation, real stale-portal behavior, and conflicting upstream records are outside the implemented grammar and must abstain or remain external work.

## Non-claims

EGIS is scoped to **research use only** and is **not** clinically validated or production-ready. File availability is not evidence of assay completion, QC success, or biomarker status. Open-access WXS Masked Somatic Mutation MAFs and `/ssm_occurrences` record called positive somatic variants only and cannot establish negative/wild-type calls or locus-level callable reference coverage. This report does not establish patient identity, clinical utility, prognosis, biomarkers, causality, or real CPTAC biospecimen totals.

