# Cancer Evidence Engine build plan

## Contract

The candidate is a Python package and CLI that operates on explicit frozen snapshots. Runtime answers are deterministic and non-generative. Public synthetic fixtures prove the offline path; real GDC/PDC acquisition is adapter/documentation work and remains blocked unless official endpoints can be queried and completeness is evidenced. TCGA-LUAD and the selected CPTAC study remain separate namespaces with no identity join.

Shared conventions:

- Python 3.11+, SQLite, JSON Schema, pytest; no runtime network calls during answer or verify.
- Source identifiers are namespaced as `gdc:<id>` or `pdc:<id>`.
- Entity kinds are `case`, `sample`, `aliquot`, `assay`, and `file`; relationships are explicit typed edges.
- Result statuses are `SUPPORTED`, `CONTRADICTED`, `UNKNOWN`, `CONFLICT`, `AMBIGUOUS`, `OUT_OF_SCOPE`, and `ERROR`.
- Every answer certificate includes canonical query, entity level, snapshot/rule/schema identifiers and hashes, policies, numerator/denominator, result, status/reason code, evidence references, and a witness.
- The replay checker has its own set reconstruction and arithmetic path; it may share schemas/specifications but not the production answer function.
- Never claim external evaluator or real-source completion without an external artifact. Mark it `BLOCKED` or `PENDING_EXTERNAL`.

## Tree and ownership

1. Contract and source boundary
   1.1 Core models, schemas, snapshots, and synthetic fixture (`evidence_engine/{model,snapshot,schema,fixture}.py`, `schemas/`)
   1.2 Deterministic query engine, certificates, and independent replay (`evidence_engine/{engine,certificate,replay}.py`)
   1.3 CLI, source adapter boundary, evaluator runner, and documentation (`evidence_engine/{cli,adapters,evaluator}.py`, `README.md`, `docs/`, `pyproject.toml`)
2. Integration and verification
   2.1 Public and negative tests (`tests/`)
   2.2 Stage handoffs and final evidence report (`handoffs/`, `docs/evidence-report.md`)

Each leaf owns its listed files. The root owns integration, final gate reruns, and conflict resolution. No leaf may add a dependency or alter the shared status taxonomy without recording it here first.

## Status log

| Leaf | Owner | Status | Evidence |
|---|---|---|---|
| 1.1 | root fallback after two stalled workers | PASS | `gates/leaf-1-1-core.md`; 2 tests |
| 1.2 | root | PASS | `gates/leaf-1-2-engine.md`; engine and replay tests |
| 1.3 | root | PASS | `gates/leaf-1-3-interface.md`; CLI/evaluator/docs |
| 2.1 | root | PASS | `gates/leaf-2-1-tests.md`; 12 tests |
| 2.2 | root | PASS | `gates/leaf-2-2-integration.md`; handoff/demo tests |

