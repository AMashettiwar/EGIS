# Cancer Evidence Engine — offline synthetic demo

This repository contains a small, inspectable, non-generative demo of a proof-carrying cancer metadata query. The runnable demo uses only the clearly labeled fixture in `examples/synthetic_fixture.json`.

The fixture is synthetic only: it is **not TCGA data, not CPTAC data, and not an extract from GDC or PDC**. The repository makes no claim of live-source integration, blind evaluation, clinical validity, or regulatory suitability.

## Run the demo

From the repository root, in an environment with Python 3.11+:

```powershell
python scripts/run_demo.py
```

The command runs entirely offline, evaluates the documented query, writes a certificate and replay report under `artifacts/synthetic-demo/`, and exits only after replay verification prints `CERTIFICATE_VERIFIED`.

For a clean development environment, install the test extra and run the full suite:

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
```

## Query and fixture

`examples/synthetic_overlap.json` asks for case-level overlap between these two assay-record sets:

```json
{
  "operation": "case_overlap",
  "fixture_id": "synthetic-case-overlap-v1",
  "cohort_id": "SYNTHETIC-COHORT-A",
  "entity_level": "case",
  "left": ["ASSAY-L-001", "ASSAY-L-002", "ASSAY-L-003"],
  "right": ["ASSAY-R-001", "ASSAY-R-002"]
}
```

The expected supported result is numerator `1`, denominator `2`, and overlap case `CASE-001`. The engine deduplicates at the requested case level, so the two left assay records belonging to `CASE-002` count as one case. Files and source records remain evidence references; they do not inflate the case count.

Missing or incomplete evidence never becomes a zero. The engine returns `UNKNOWN`, `CONFLICT`, `AMBIGUOUS`, `OUT_OF_SCOPE`, or `ERROR` when the available records do not justify a supported result.

## Certificate and replay boundary

`cancer_evidence.certificate.issue_certificate` evaluates the query and binds the result to a canonical fixture SHA-256 hash and rule version `case-overlap-v1`. `cancer_evidence.replay` independently reconstructs the answer from fixture records and does not import or call the production engine. Changing a qualifying record, the fixture hash, the certificate digest, or the cohort result causes replay verification to fail.

## Repository map

```text
examples/
  synthetic_fixture.json       synthetic cases, samples, assays, files, sources
  synthetic_overlap.json       documented case-level query
scripts/
  run_demo.py                  one-command offline entry point
src/cancer_evidence/
  engine.py                    deterministic production evaluator
  certificate.py               fixture/rule-bound certificate issuer
  replay.py                    independent certificate checker
  demo.py                      demo CLI
tests/
  test_synthetic_engine.py     offline acceptance and tamper tests
```

The older `evidence_engine/` package and its historical tests remain in the repository for continuity; this task's new inspectable demo is the `src/cancer_evidence/` package above.
