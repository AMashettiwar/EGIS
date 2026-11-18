# Gates: CLI, adapters, evaluator runner, and docs

Scope: executable interface, bounded public-source adapters, hidden-input runner boundary, and user documentation.

- [x] G1: Offline demo and documented commands are executable.
  CHECK: python -m unittest tests.test_cli -v
  EXPECT: OK
  EVIDENCE: Ran 3 tests in 1.079s | OK

- [x] G2: Real-source and external-evaluator limitations are explicit and not presented as completed.
  EVIDENCE: README.md, docs/source-adapters.md, docs/evidence-report.md, and stage handoffs mark real-source acquisition and held-out evaluation PENDING_EXTERNAL.

