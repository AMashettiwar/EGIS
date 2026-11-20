# EGIS Architecture, Contracts, Baseline & Research-Readiness Roadmap

## 1. End Goal & System Positioning

EGIS (`cancer_evidence`) is a deterministic, non-generative biomedical evidence engine that answers explicitly scoped cohort, claim, and cross-source questions over **frozen, versioned evidence snapshots**, preserves full evidence lineage and source identifiers, distinguishes lack of evidence (`UNKNOWN`) from affirmative evidence of absence (`CONTRADICTED`), and allows an **independent verifier** to cryptographically and relationally replay every result against SQLite (`data.sqlite`) without trusting the query engine.

Core non-negotiable guarantees:
- **Zero generative or LLM logic** in evaluation, snapshot verification, or replay.
- **Zero live network calls** during evaluation or replay (offline-first).
- **Explicit cohort denominator binding**: callers cannot silently alter denominator definitions via ad-hoc filtering.
- **Strict evidence honesty**: missing, incomplete, conflicting, ambiguous, or out-of-scope questions produce explicit taxonomic statuses rather than fabricated zeros or extrapolated clinical claims.

---

## 2. Recorded Engineering Baseline (Pre-Phase-2 Checkout)

Before beginning the Phase 2–6 upgrades, repository inspection established the following verified baseline:

### 2.1 Existing Capabilities
- **Package & CLI** (`src/cancer_evidence/`): Python 3.12 package exposing `evidence` (`demo`, `snapshot fetch|inspect|import`, `answer`, `verify`, `export`, `evaluate-hidden`).
- **Snapshot Storage** (`snapshot.py`): Atomic writes of `raw.json`, `normalized.json`, SQLite `data.sqlite` (`entities`, `relationships`, `evidence`), `manifest.json` (with `manifest_sha256` & derivation hashes), and `READY` publication marker.
- **Certificates & Replay** (`certificate.py`, `replay.py`, `signing.py`): `certificate_version: "2.0.0"` signed with Ed25519 (`keys/public_key.pem`) and independently replayed via SQLite without importing `engine.evaluate`.
- **Operations Implemented at Baseline**: `count_entities`, `assay_availability`, `assay_overlap`, `file_lineage`, `compare_inventories`, and patient-level `cohort_count`.
- **Real-Data Snapshots Present**:
  - `snapshots/real/gdc_tcga_luad`: `585` TCGA-LUAD case records from GDC REST API (`3` pages, `complete: true`).
  - `snapshots/real/pdc_pdc000153`: `115` CPTAC LUAD (`PDC000153`) case records from PDC GraphQL (`Proteome`, `TMT10`, `complete: true`).
- **Baseline Verification Metrics**:
  - Unit suite: `.venv/bin/python -m pytest -q` -> `60 passed`.
  - Mutation harness (`scripts/mutation_check.py`): `4/4` mutations killed (`kill_rate: 1.0`).
  - Preserved frozen blind-run baseline (`artifacts/blind-exam-run`, `FROZEN_MANIFEST.json` internal `manifest_sha256`: `3e7e2f472b7bd64e9ceb2321e188ff6e3dfc74d49b880bc82708ac7de4a2fe1d`, `artifacts/cor-001-blind-run` `manifest_sha256`: `3835e5940f5ad577e720cfcaf5b6ce273999c0325e40c84e100a44cdfeda78ae`): graded in `artifacts/blind-exam-grading/grading_report.json` with `6/42` executed (`4/5` blind holdouts passed, `1/1` dev regression `COR-001` passed, `36` `NOT_RUN`, `6` pre-designated `BLOCKED`).

### 2.2 Baseline Defects & Release Blockers Identified
1. **Missing `CONTRADICTED` Status**: `src/cancer_evidence/model.py` defined 6 statuses, omitting `CONTRADICTED` even though `PLAN.md` required all 7 statuses (`SUPPORTED`, `CONTRADICTED`, `UNKNOWN`, `CONFLICT`, `AMBIGUOUS`, `OUT_OF_SCOPE`, `ERROR`).
2. **`COR-008` Evidence Grounding Bug**: On canonical patient deduplication queries without assay-level QC/status denominator filters (when duplicate case rows carry `is_duplicate_case_entry: true`), `cohort_count` emitted `ASSAY-*` IDs alongside canonical `PT-*` IDs, failing strict patient-level witness grounding (`actual=['ASSAY-201A', ..., 'PT-203']` vs `expected=['PT-201', 'PT-202', 'PT-203']`).
3. **Incomplete `cohort_count` Granularity & Predicates**: `cohort_count` only supported `entity_level="patient"`; lacked `sample`, `file`, `case`, `aliquot`, and `assay` counting, numerical/timestamp bounds (`min_tumor_purity`, `max_collection_timestamp`), receptor panel predicates (`er_status`, `pr_status`, `her2_status`), and valid `0/N` empty subcohort counts (`SUPPORTED` when `N > 0` vs `UNKNOWN` when `N == 0`).
4. **Missing Core Operations**: No support for `claim_verification` (single-subject biomarker/stage claims and cohort-wide universal negative/prevalence claims), `cross_source_overlap_count` over governed `identity_mappings`, or `entity_attribute_query` with alias collision detection.
5. **Missing Provenance & Relational Integrity Gates**: `snapshot.py` lacked storage/validation for `pathology_reports` (`ACTIVE_FINAL` vs `SUPERSEDED` + `superseded_by` resolution), `identity_mappings` (`APPROVED`, `DRAFT_UNAPPROVED`, `APPROVED_CANDIDATE_SET`, `REVOKED`), per-table manifests (`COMPLETE` vs `PARTIAL_INGEST`, `declared_row_count`, `sha256`, `partition_snapshot_id`), `is_frozen`/`freeze_timestamp`/`provenance_custody_chain` enforcement, `supported_entity_levels` ontology checks, foreign-key/lineage validation, and biological range validation (`tumor_purity`, `vaf`, ISO calendar dates).
6. **Limited Mutation & Test-Envelope Replay Coverage**: `replay.py` had no documented verifier mode for structured witness-Merkle test envelopes (`SYNTHETIC_EXAM_TEST_ENVELOPE`), and `scripts/mutation_check.py` only tested 4 mutations.
7. **Real-Data Metadata Shallow Capture**: Checked-in GDC and PDC snapshots only capture `case`-level summary fields (`relationships: []`), omitting sample/aliquot/assay/file hierarchy and clinical/biomarker attributes needed for biomarker cohort evaluation.
8. **Stale Docs & Compromised Blindness of 48-Case Archive**: `docs/evidence-report.md` line 27 falsely listed PDC as `PENDING_EXTERNAL`, and inspecting `answer_key.json` during grading converted the 48-case archive into a **development/regression suite** rather than an independent blind audit.

> **Preservation Guarantee**: All prior artifacts in `artifacts/blind-exam-run/`, `artifacts/cor-001-blind-run/`, and `artifacts/blind-exam-grading/grading_report.json` MUST remain byte-for-byte untouched.

---

## 3. Public System Contracts

### 3.1 Query Contract (`schemas/query.schema.json`)
- Every structured query specifies `operation` (`query_type` alias supported by CLI/adapters) in:
  `count_entities | assay_availability | assay_overlap | file_lineage | compare_inventories | cohort_count | claim_verification | cross_source_overlap_count | entity_attribute_query | out_of_scope_request`.
- Any count operation (`cohort_count`, `cross_source_overlap_count`) must explicitly supply `entity_level` and non-empty `denominator_basis`; omitting either returns `AMBIGUOUS`.
- Free-text clinical recommendation, prognosis, survival, imputation, causal inference, or live external query requests return `OUT_OF_SCOPE`.

### 3.2 Snapshot & Provenance Contract (`schemas/snapshot.schema.json`)
- A queryable snapshot must be frozen (`is_frozen: true`, valid `freeze_timestamp`, non-empty `provenance_custody_chain`), pass all table-level SHA-256 and `declared_row_count` checks, have consistent `snapshot_id` across all tables/partitions, maintain referential integrity across `patient -> case -> sample -> aliquot -> assay -> file` and `pathology_reports`, and contain valid domain values (`0.0 <= tumor_purity <= 1.0`, `0.0 <= vaf <= 1.0`, valid ISO-8601 calendar dates).
- Violations of snapshot provenance, integrity, domain syntax, or unsupported entity levels (`entity_level` outside `supported_entity_levels`) must return `Status.ERROR` with zero fabricated counts or evidence.
- Partial ingestion partitions (`declared_completeness: "PARTIAL_INGEST"`) must abstain with `UNKNOWN` for open-world negative/count claims unless an affirmative QC-passed counterexample contradicts a universal zero claim (`CONTRADICTED`).

### 3.3 Taxonomic Result Contract (`schemas/result.schema.json`)
Outputs strictly use the 7 taxonomic statuses:
1. `SUPPORTED`: Valid query, verified frozen snapshot, sufficient qualifying evidence (including valid `0 / N` empty subcohort counts where `N > 0` and all members are completely assayed).
2. `CONTRADICTED`: Affirmative, QC-passed, active (non-superseded) evidence directly falsifies the claim (or a valid positive counterexample falsifies a universal zero claim).
3. `UNKNOWN`: Insufficient evidence, untested/missing assays (`NOT_PERFORMED`), `PARTIAL_INGEST` partitions, `0 / 0` prevalence denominators, or unapproved (`DRAFT_UNAPPROVED` / missing) cross-source mappings.
4. `CONFLICT`: Unresolved conflicting active pathology reports, unresolved conflicting replicate assays, or conflicting `APPROVED` identity mappings across authorities.
5. `AMBIGUOUS`: Missing `entity_level` or `denominator_basis`, non-unique `legacy_subject_alias`, or `AMBIGUOUS_ONE_TO_MANY` identity mappings.
6. `OUT_OF_SCOPE`: Clinical advice, survival/prognostic modeling, imputation, causal inference, live portal lookups, or cross-source assay joins without mappings.
7. `ERROR`: Invalid query syntax, provenance/hash/row-count/FK/freeze violations, malformed biological records, unsupported entity levels, or certificate tampering.

### 3.4 Certificate & Independent Replay Contract
- **Production Format (`certificate_version: "2.0.0"`)**: Binds `query_sha256`, `snapshot_id`, `snapshot_manifest_sha256`, `snapshot_sha256`, `derivation`, `result`, `evidence_scope`, `rule_version`, and Ed25519 `signature` verified against `keys/public_key.pem`.
- **Structured Test Envelope Mode (`signature.signature_kind == "SYNTHETIC_EXAM_TEST_ENVELOPE"`)**: Explicitly supported only when a caller submits a structured Merkle/digest test certificate alongside a snapshot carrying `trusted_signing_keys`. Verifies `signer_key_id` is `ACTIVE_TRUSTED`, validates `snapshot_id`/`snapshot_sha256`, recomputes `query_sha256`, `result_sha256`, witness leaf hashes + binary `evidence_merkle_root`, `certificate_body_sha256`, checks signature binding, AND executes full independent SQL reconstruction against `data.sqlite` (catching omitted witnesses and self-consistent forged certificates).
- **Clean-Room Isolation**: `replay.py` never imports or invokes `engine.evaluate`.

---

## 4. Prioritized Roadmap to Research-Grade Release

1. **Phase 1 (Complete)**: Baseline audit, bug inventory, public contracts, and preservation of frozen prior artifacts.
2. **Phase 2 (Engine & Provenance Correctness)**: Add `CONTRADICTED`, rich relational snapshot schema (`pathology_reports`, `identity_mappings`, table manifests, provenance integrity checks), full `cohort_count` levels/predicates/dedup, `claim_verification`, `cross_source_overlap_count`, and `entity_attribute_query`.
3. **Phase 3 (Snapshot & Real-Data Readiness)**: Fix stale real-data status docs, validate GDC TCGA-LUAD (`585` cases) and PDC `PDC000153` (`115` cases) offline into `artifacts/real-data-readiness/real_snapshots_report.json`, and document exact field capture vs missing assay/biospecimen hierarchy. (Future networked extension: expand GDC/PDC adapters to freeze sample/file/clinical metadata into `snapshots/real/`.)
4. **Phase 4 (Independent Replay & Mutation Hardening)**: Complete independent SQL reconstruction in `replay.py` across all operations and test envelopes; expand `scripts/mutation_check.py` to 8+ mutations at 100% kill rate.
5. **Phase 5 (Verification, Regression & Clean-Room Holdout)**: Achieve `100%` pass on unit tests + 42-case regression suite (`artifacts/regression-exam-run`) AND publish a brand-new, unexecuted clean-room holdout benchmark (`artifacts/cleanroom-holdout-benchmark`) for external third-party evaluation.
6. **Phase 6 (Operational & External Governance — Post-Engineering Release Gates)**:
   - Migrate demo keypair in `keys/` to externally managed HSM/KMS Ed25519 production keys.
   - Run enriched GDC/PDC sample+assay snapshot ingestion with network custody logs.
   - Conduct independent third-party blind execution of `artifacts/cleanroom-holdout-benchmark` + clinical/domain SME sign-off.
