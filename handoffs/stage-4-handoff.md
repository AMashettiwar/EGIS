# Stage 4 — Certificate replay and tamper rejection

Status: PASS

Evidence: `evidence_engine/certificate.py` emits snapshot/rule/query hashes and evidence witnesses. `evidence_engine/replay.py` reconstructs results from SQLite without calling `evaluate`; `tests/test_replay.py` checks valid replay and tamper rejection.

Invocation: `python -m unittest tests.test_replay -v`

