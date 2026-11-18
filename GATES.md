# Gates: Cancer Evidence Engine

Scope: a runnable, offline-first, proof-carrying metadata engine with synthetic fixtures, independent replay, CLI, documented source adapters, and honest external-validation status.

- [x] G1: The repository contains a documented contract, architecture, scope limits, and seven stage handoffs with explicit PASS/FAIL/BLOCKED/PENDING_EXTERNAL labels.
  CHECK: python -c "from pathlib import Path; print(sum(1 for p in Path('.').rglob('*.md') if p.name in {'README.md','PLAN.md'} or p.name.startswith('stage-') or 'handoff' in p.name))"
  EXPECT: 10
  EVIDENCE: 10

- [x] G2: The offline synthetic demo can create a frozen snapshot and answer a supported overlap query without network access.
  CHECK: python -m unittest tests.test_demo -v
  EXPECT: OK
  EVIDENCE: Ran 1 test in 0.076s | OK

- [x] G3: Source snapshots record request metadata, content hashes, schema/adapter versions, coverage, and atomic publication semantics; network acquisition is bounded and never required by the offline demo.
  CHECK: python -m unittest tests.test_snapshot -v
  EXPECT: OK
  EVIDENCE: Ran 5 tests in 0.183s | OK

- [x] G4: Typed entities and documented relationships preserve source namespaces and distinguish cases, samples, aliquots, assays, and files without substring identity joins.
  CHECK: python -m unittest tests.test_normalization -v
  EXPECT: OK
  EVIDENCE: Ran 2 tests in 0.000s | OK

- [x] G5: Supported operations return deterministic statuses, reason codes, explicit denominators, and abstentions for incomplete, ambiguous, conflicting, and unsupported evidence.
  CHECK: python -m unittest tests.test_engine -v
  EXPECT: OK
  EVIDENCE: Ran 16 tests in 1.730s | OK

- [x] G6: Certificates replay through an independent checker, reject tampering and snapshot mismatch, and enforce duplicate, hierarchy, and zero-denominator invariants.
  CHECK: python -m unittest tests.test_replay -v
  EXPECT: OK
  EVIDENCE: Ran 1 test in 0.054s | OK

- [x] G7: The documented CLI commands and one-command offline demo execute successfully with normalized export and certificate output.
  CHECK: python -m unittest tests.test_cli -v
  EXPECT: OK
  EVIDENCE: Ran 1 test in 0.322s | OK

- [x] G8: Public tests cover the implemented exact structured-query grammar plus source-scope, zero-denominator, incomplete-snapshot, missing-lineage, tamper, and unsupported-operation negatives; unsupported ambiguity/negation remains an explicit out-of-scope case.
  CHECK: python -m unittest discover -s tests -v
  EXPECT: OK
  EVIDENCE: Ran 34 tests in 3.282s | OK

- [x] G9: A hidden-input evaluator runner exists and disables network during replay; expected answers/reference data are outside the candidate package or explicitly marked unavailable.
  CHECK: python -m unittest tests.test_evaluator_runner -v
  EXPECT: OK
  EVIDENCE: Ran 1 test in 0.050s | OK

- [x] G10: Real GDC/PDC acquisition and an independent held-out evaluator are not falsely marked complete; their current state is recorded as BLOCKED or PENDING_EXTERNAL with exact reasons and no fabricated counts.
  EVIDENCE: README.md, docs/evidence-report.md, and handoffs/stage-1-handoff.md plus handoffs/stage-6-handoff.md mark both external areas PENDING_EXTERNAL.

- [x] G11: The workspace is a real Git repository with a non-empty initial commit containing the engine, tests, schemas, docs, and demo fixture.
  CHECK: python -c "import subprocess; assert subprocess.check_output(['git','rev-parse','--is-inside-work-tree'], text=True).strip() == 'true'; assert subprocess.check_output(['git','rev-list','--count','HEAD'], text=True).strip() != '0'; tracked=subprocess.check_output(['git','ls-files'], text=True); assert 'README.md' in tracked and 'src/cancer_evidence/cli.py' in tracked and 'tests/test_demo.py' in tracked; print('repository commit and tracked deliverables passed')"
  EXPECT: repository commit and tracked deliverables passed
  EVIDENCE: repository commit and tracked deliverables passed

- [x] G12: A fresh synthetic demo invocation produces a supported result, a replay-valid certificate, normalized export, and a frozen snapshot artifact.
  CHECK: python scripts/run_demo.py --out artifacts/gate-demo
  EXPECT: CERTIFICATE_VERIFIED
  EVIDENCE: CERTIFICATE_VERIFIED; demo JSON reports SUPPORTED and replay valid true.

- [x] G13: The repository documents the exact offline demo command and distinguishes synthetic evidence from real-source and held-out evaluation status.
  EVIDENCE: README.md documents `python scripts/run_demo.py`; docs/evidence-report.md records the real snapshot acquisition and held-out evaluation remains PENDING_EXTERNAL.

- [x] G14: The required `src/cancer_evidence/`, `tests/`, `examples/`, and packaging metadata exist for the inspectable offline demo.
  CHECK: python -c "from pathlib import Path; required=['README.md','pyproject.toml','src/cancer_evidence/__init__.py','tests/test_synthetic_engine.py','examples/synthetic_fixture.json','examples/synthetic_overlap.json']; assert all(Path(p).exists() for p in required); print('required offline demo files passed')"
  EXPECT: required offline demo files passed
  EVIDENCE: required offline demo files passed

- [x] G15: The documented synthetic demo runs offline and verifies a certificate bound to the fixture hash and rule version.
  CHECK: python scripts/run_demo.py
  EXPECT: CERTIFICATE_VERIFIED
  EVIDENCE: CERTIFICATE_VERIFIED | demo JSON reported SUPPORTED, numerator 1, denominator 1, output_dir artifacts\\verified-demo.

- [x] G16: The synthetic acceptance tests cover correct overlap, deduplication, entity-level confusion, missing/conflicting evidence, stale fixture hash, altered certificate, and wrong-cohort arithmetic.
  CHECK: python -c "import sys,unittest; sys.path.insert(0,'src'); suite=unittest.defaultTestLoader.discover('tests', pattern='test_synthetic_engine.py'); result=unittest.TextTestRunner(verbosity=2).run(suite); raise SystemExit(not result.wasSuccessful())"
  EXPECT: OK
  EVIDENCE: 2 focused synthetic tests, OK. The full pytest command was also run with the installed Python 3.11 interpreter: 51 passed.

<!-- Every checked gate must have actual evidence. If a gate is impossible, add ABANDON: <gate> <reason> instead of silently dropping it. -->

## Current W1-W9 repair gates

- [x] W1: Replay is total, complete, and round-trips every engine status and reason code, including abstention certificates.
  CHECK: python -c "import sys,unittest; sys.path.insert(0,'src'); s=unittest.defaultTestLoader.discover('tests', pattern='test_replay_matrix.py'); r=unittest.TextTestRunner(verbosity=0).run(s); raise SystemExit(not r.wasSuccessful())"
  EXPECT: OK
  EVIDENCE: Ran 6 tests in 1.207s | OK

- [x] W2: Replay uses an independently structured derivation and catches at least 3 of 4 deliberate engine mutations.
  CHECK: python scripts/mutation_check.py
  EXPECT: "killed": 4
  EVIDENCE: "total": 4 | }

- [x] W3: Certificates bind raw snapshot hash and replayed raw-to-normalized derivation; normalized-only edits fail with derivation mismatch.
  CHECK: python -c "import sys,unittest; sys.path.insert(0,'src'); s=unittest.defaultTestLoader.discover('tests', pattern='test_derivation_binding.py'); r=unittest.TextTestRunner(verbosity=0).run(s); raise SystemExit(not r.wasSuccessful())"
  EXPECT: OK
  EVIDENCE: Ran 1 test in 0.119s | OK

- [x] W4: Certificates use Ed25519 signing and verification with a published public key; recomputed content hashes do not forge results.
  CHECK: python -c "import sys,unittest; sys.path.insert(0,'src'); s=unittest.defaultTestLoader.discover('tests', pattern='test_signatures.py'); r=unittest.TextTestRunner(verbosity=0).run(s); raise SystemExit(not r.wasSuccessful())"
  EXPECT: OK
  EVIDENCE: Ran 3 tests in 0.235s | OK

- [x] W5: Query schema supports cohort_filter and denominator_mode, and eligible/universe denominators are snapshot-derived.
  CHECK: python -c "import sys,unittest; sys.path.insert(0,'src'); s=unittest.defaultTestLoader.discover('tests', pattern='test_denominator_universe.py'); r=unittest.TextTestRunner(verbosity=0).run(s); raise SystemExit(not r.wasSuccessful())"
  EXPECT: OK
  EVIDENCE: Ran 2 tests in 0.186s | OK

- [x] W6: Runtime product is consolidated under src/cancer_evidence; no runtime evidence_engine references remain outside legacy documentation.
  CHECK: python -c "from pathlib import Path; import subprocess; assert Path('src/cancer_evidence').exists(); r=subprocess.run(['git','grep','-n','evidence_engine','--','*.py','*.toml'], text=True, capture_output=True); assert r.returncode in (0,1) and not r.stdout; print('consolidated package present')"
  EXPECT: consolidated package present
  EVIDENCE: consolidated package present

- [x] W7: A frozen public GDC TCGA-LUAD snapshot reproduces its case count offline and incomplete-page replay returns UNKNOWN.
  CHECK: python -c "import sys,unittest; sys.path.insert(0,'src'); s=unittest.defaultTestLoader.loadTestsFromName('tests.test_real_snapshots.RealSnapshotTests'); r=unittest.TextTestRunner(verbosity=0).run(s); raise SystemExit(not r.wasSuccessful())"
  EXPECT: OK
  EVIDENCE: Ran 3 tests in 3.384s | OK

- [x] W8: A frozen public PDC snapshot is verified, kept in its own namespace, and cross-cohort patient overlap is refused without authoritative mapping.
  CHECK: python -c "import sys,unittest; sys.path.insert(0,'src'); s=unittest.defaultTestLoader.loadTestsFromName('tests.test_real_snapshots.RealSnapshotTests.test_pdc_is_separate_namespace_and_has_expected_count'); r=unittest.TextTestRunner(verbosity=0).run(s); raise SystemExit(not r.wasSuccessful())"
  EXPECT: OK
  EVIDENCE: Ran 1 test in 0.116s | OK

- [x] W9: Every completed work item has regression coverage, PROOF.md records actual commands and outputs, STATUS.md records PASS/FAIL/BLOCKED/NOT DONE, and the full suite is green.
  CHECK: python -c "import sys,unittest; sys.path.insert(0,'src'); s=unittest.defaultTestLoader.discover('tests'); r=unittest.TextTestRunner(verbosity=0).run(s); raise SystemExit(not r.wasSuccessful())"
  EXPECT: OK
  EVIDENCE: Ran 54 tests in 15.975s | OK
