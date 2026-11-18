# Gates: integration and handoffs

Scope: stage 0-6 handoffs, final evidence report, and end-to-end integration.

- [x] G1: All seven stage handoffs exist with PASS/FAIL/BLOCKED/PENDING_EXTERNAL labels and exact invocations.
  CHECK: python -m unittest tests.test_handoffs -v
  EXPECT: OK
  EVIDENCE: Ran 1 test in 0.001s | OK

- [x] G2: The offline end-to-end demo passes with network disabled.
  CHECK: python -m unittest tests.test_demo -v
  EXPECT: OK
  EVIDENCE: Ran 1 test in 0.098s | OK
