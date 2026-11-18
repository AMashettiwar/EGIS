# Gates: core models and snapshots

Scope: typed domain model, schemas, frozen SQLite snapshots, and deterministic offline synthetic fixture.

- [x] G1: Core modules import without runtime network or generative dependencies.
  CHECK: python -c "import evidence_engine.model, evidence_engine.snapshot, evidence_engine.schema, evidence_engine.fixture; print('core imports passed')"
  EXPECT: core imports passed
  EVIDENCE: core imports passed

- [x] G2: Synthetic fixture produces a deterministic snapshot with hashes and explicit typed relationships.
  CHECK: python -m unittest tests.test_snapshot -v
  EXPECT: OK
  EVIDENCE: Ran 5 tests in 0.175s | OK

