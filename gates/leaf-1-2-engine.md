# Gates: deterministic engine and replay

Scope: supported operations, status/reason codes, answer certificates, and independent replay checker.

- [x] G1: Supported operations produce deterministic answers and precise abstentions.
  CHECK: python -m unittest tests.test_engine -v
  EXPECT: OK
  EVIDENCE: Ran 16 tests in 1.395s | OK

- [x] G2: Replay accepts valid certificates and rejects tampered or mismatched evidence.
  CHECK: python -m unittest tests.test_replay -v
  EXPECT: OK
  EVIDENCE: Ran 5 tests in 0.481s | OK

