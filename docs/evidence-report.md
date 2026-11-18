# Evidence report

## Commands

The commands used for the offline candidate are recorded here after execution. The authoritative gate evidence is in `GATES.md` and the leaf ledgers.

Commands actually run:

```powershell
python -m unittest discover -s tests -v
python -m evidence_engine demo --out artifacts/demo
& 'C:\Users\Ankith Mashettiwar\AppData\Local\Programs\Python\Python311\python.exe' -m pytest -q
```

Observed results: unittest `Ran 34 tests ... OK`; pytest `34 passed in 3.83s` using the system Python 3.11 interpreter. The default hermes `python` interpreter has no `pip`/`pytest`, so the portable gate ledger uses unittest-compatible checks; `requirements.lock` records `pytest==8.3.3` for the development environment.

## External status

- GDC real-source snapshot: `PENDING_EXTERNAL` — no acquisition artifact, page-completeness proof, or frozen hash is present.
- PDC real-source snapshot: `PENDING_EXTERNAL` — the study identity is documented, but the versioned live response is not frozen in this repository.
- Independent hidden evaluator: `PENDING_EXTERNAL` — runner exists; hidden expected answers and outside scoring are not present.
- Public synthetic tests: status is determined by the commands above; synthetic results must not be reported as portal results.

## Declared public test strata

The public suite covers exact structured queries, source-scope rejection, zero denominators, incomplete snapshots, cross-source relationship rejection, lineage, tampered certificates, replay scope tampering, unsupported operations, CLI smoke behavior, and network-disabled candidate replay. Free-text ambiguity/negation, real stale-portal behavior, and conflicting upstream records are outside the implemented grammar and must abstain or remain external work.

## Non-claims

This report does not establish patient identity, clinical utility, prognosis, biomarkers, causality, or real TCGA/CPTAC cohort totals.

