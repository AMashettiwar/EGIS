# Stage 3 — Deterministic inference and abstention

Status: PASS

Evidence: `src/cancer_evidence/engine.py` supports structured metadata operations, explicit denominators, cross-source abstention, incomplete-snapshot rejection, and unsupported-operation status. `tests/test_engine.py` covers the key negative cases.

Invocation: `python -m unittest tests.test_engine -v`

