# Gates: public and negative tests

Scope: test strata and regression coverage across all public behavior.

- [x] G1: Full public test suite passes and includes the declared negative strata.
  CHECK: python -m unittest discover -s tests -v
  EXPECT: OK
  EVIDENCE: Ran 34 tests in 3.446s | OK

