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

- GDC real-source snapshot: frozen public metadata is present under `snapshots/real/gdc_tcga_luad/`; completeness and hash proof are in `PROOF.md`. Blind evaluation remains `PENDING_EXTERNAL`.
- PDC real-source snapshot: `PENDING_EXTERNAL` — the study identity is documented, but the versioned live response is not frozen in this repository.
- Independent hidden evaluator: `PENDING_EXTERNAL` — runner exists; hidden expected answers and outside scoring are not present.
- Public synthetic tests: status is determined by the commands above; synthetic results must not be reported as portal results.

## Declared public test strata

The public suite covers exact structured queries, source-scope rejection, zero denominators, incomplete snapshots, cross-source relationship rejection, lineage, tampered certificates, replay scope tampering, unsupported operations, CLI smoke behavior, and network-disabled candidate replay. Free-text ambiguity/negation, real stale-portal behavior, and conflicting upstream records are outside the implemented grammar and must abstain or remain external work.

## Non-claims

This report does not establish patient identity, clinical utility, prognosis, biomarkers, causality, or real TCGA/CPTAC cohort totals.

