# Cancer Evidence Engine

This is a deterministic, non-generative metadata engine. Runtime answer and
verification paths use only a frozen snapshot; they do not call an LLM, model
API, embedding service, GDC, or PDC.

The repository contains a synthetic offline demo plus frozen public metadata
snapshots. It makes no clinical, causal, patient-reidentification, regulatory,
blind-evaluation, or release-acceptance claim.

## Reproduce the offline demo

From a clean checkout:

```powershell
python -m pip install -e ".[test]"
python scripts/run_demo.py
```

The demo produces a `SUPPORTED` result and prints `CERTIFICATE_VERIFIED`.
The exact verification command for its outputs is:

```powershell
python -m cancer_evidence verify --snapshot artifacts/verified-demo/snapshot --certificate artifacts/verified-demo/certificate.json
```

`certificate.py` signs the canonical certificate with Ed25519. `keys/public_key.pem`
is the published verification key for this repository's demo key pair. The
matching private key is included only as an explicitly non-production demo key;
replace it with an externally managed signing key for any real release.

## Human-verifier demo UI (offline & read-only)

To inspect the real `TCGA-LUAD` GDC v3 workflow (`40 / 571` observed selected
`EGFR` calls among file-linked Primary Tumor samples, denominator funnel,
per-witness `Sample -> Tumor Aliquot -> Variant Record -> MAF File` lineage,
abstentions, and replay verification) without running any external server or
network call:

```bash
# Build static offline HTML report & view model (no new dependencies)
.venv/bin/python scripts/run_verifier_ui.py --out artifacts/human-verifier-ui

# Or launch the read-only local HTTP server (GET / and GET /api/view-model)
.venv/bin/python scripts/run_verifier_ui.py --out artifacts/human-verifier-ui --serve --port 8765
# Equivalent CLI subcommand:
.venv/bin/evidence verifier-ui --out artifacts/human-verifier-ui --serve --port 8765
```

See `docs/HUMAN_REVIEWER_BRIEF.md` for the developer-prepared reviewer guide and
verification checklist. Neither the UI nor the brief is an independent audit or
clinical validation.

## Query, status, and denominator contract

EGIS answers structured metadata, cohort, claim, and cross-source queries against
frozen snapshots across seven strict statuses: `SUPPORTED`, `CONTRADICTED`,
`UNKNOWN`, `CONFLICT`, `AMBIGUOUS`, `OUT_OF_SCOPE`, and `ERROR`.

- Supported operations: `cohort_count` (`patient`, `sample`, `file` levels,
  range/timestamp cutoffs, receptor panels, canonical deduplication, `0/N`
  empty subcohorts), `claim_verification` (single-subject biomarkers,
  pathology supersession chains, active conflicts, universal-zero
  counterexamples, `0/0` prevalence abstention), `cross_source_overlap_count`
  (governed `APPROVED` mappings only), `entity_attribute_query`, and
  `out_of_scope_request`, alongside `count_entities`, `assay_availability`,
  `assay_overlap` (`examples/overlap.json`), `file_lineage`, and
  `compare_inventories`.
- Snapshot provenance & integrity enforcement: corrupt table hashes,
  row-count mismatches, partition ID mismatches, broken foreign keys, malformed
  biological values (`tumor_purity`, `vaf`, ISO calendar dates), unfrozen
  snapshots, and unsupported entity levels return explicit `ERROR` statuses.
- Missing, incomplete (`PARTIAL_INGEST`), untested (`NOT_PERFORMED`), conflicting,
  ambiguous, or unsupported inputs produce explicit abstentions and never
  fabricate a zero count or clinical conclusion.

See `docs/architecture_and_roadmap.md` for full public contracts and the
Phases 1–6 roadmap.

## Certificates and replay

Certificates bind the query result to:

- the raw snapshot SHA-256;
- the normalized snapshot SHA-256 and raw-to-normalized derivation record;
- the snapshot manifest hash and rule version; and
- an Ed25519 signature verified against the fixed published public key.

Replay reconstructs the answer from SQL queries over `data.sqlite`, uses an
independent evidence/conflict sweep, and does not import or call
`engine.evaluate`. A normalized-record edit returns `DERIVATION_MISMATCH`.
Recomputing a content hash after editing a certificate returns
`SIGNATURE_INVALID`.

Run the independent mutation check with:

```powershell
python scripts/mutation_check.py
```

## Verification and benchmark suites

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/mutation_check.py
.venv/bin/python scripts/inspect_real_snapshots.py
.venv/bin/python scripts/reconcile_and_verify_gdc_v2.py
.venv/bin/python scripts/reconcile_and_verify_gdc_v3.py
```

The mutation suite kills 9 of 9 deliberate engine mutations via independent
SQLite SQL replay (`kill_rate: 1.0`).

- **Preserved prior blind-run artifacts** (`artifacts/blind-exam-run/`,
  `artifacts/cor-001-blind-run/`, `artifacts/blind-exam-grading/grading_report.json`)
  remain unmodified.
- **48-case regression suite** (`artifacts/regression-exam-run/`, graded in
  `artifacts/regression-exam-grading/grading_report.json`): classified as a
  development regression suite because `answer_key.json` was previously
  inspected.
- **Clean-room untouched holdout benchmark**: staged at
  `artifacts/cleanroom-holdout-benchmark/` (`candidate-blind.zip` separate from
  `evaluator-private/answer_key.json`) for future independent external audit.

## Public snapshots

Acquisition is a separate, networked operation. Answer and verify remain
offline after acquisition.

```powershell
python scripts/acquire_public_snapshots.py gdc --out snapshots/real/gdc_tcga_luad
python scripts/acquire_public_snapshots.py gdc-v2 --out snapshots/real/gdc_tcga_luad_v2
python scripts/acquire_public_snapshots.py gdc-v3 --out snapshots/real/gdc_tcga_luad_v3
python scripts/acquire_public_snapshots.py pdc --out snapshots/real/pdc_pdc000153
```

The checked-in snapshots were acquired from public metadata on 2026-11-18 / 2026-11-19 UTC:

- GDC `TCGA-LUAD` v1 (`snapshots/real/gdc_tcga_luad`): 585 cases (case-summary only), three captured pages, complete in the manifest.
- GDC `TCGA-LUAD` v2 (`snapshots/real/gdc_tcga_luad_v2`, GDC `Data Release 46.0 - August 10, 2026`): 585 cases, 1781 samples (`1075` `Primary Tumor`), 7978 aliquots, and 601 open RNA-Seq `Gene Expression Quantification` `TSV`/`released` files linking `case -> sample -> aliquot -> file` metadata (`assays`, `pathology_reports`, and `identity_mappings` remain `PARTIAL_INGEST` with 0 rows). `scripts/reconcile_and_verify_gdc_v2.py` independently reconciles `529 / 1075` `Primary Tumor` samples (`49.2093%`) with qualifying RNA-Seq TSV files against `raw.json`, verifies offline Ed25519 signing and independent SQL replay, and verifies `UNKNOWN / INCOMPLETE_SNAPSHOT` abstention for unacquired assay/QC/biomarker queries (`artifacts/gdc-luad-v2-milestone/`).
- GDC `TCGA-LUAD` v3 (`snapshots/real/gdc_tcga_luad_v3`, GDC `Data Release 46.0 - August 10, 2026`): preserves the `v2` biospecimen and RNA-Seq foundation and adds all `618` open-access WXS `Masked Somatic Mutation` (`MAF`) files (`571` distinct `Primary Tumor` samples) plus all `93` `EGFR` `/ssm_occurrences` (`GRCh38`, canonical transcript `ENST00000275493`, 93/93 1-to-1 linked to tumor aliquots and WXS MAF files; `0` unmatched, `0` multiply matched). `scripts/reconcile_and_verify_gdc_v3.py` independently reconciles `40 / 571` `Primary Tumor` samples (`7.005253940455342%`: `22` `p.L858R` + `18` `p.E746_A750del`) with `160` full-lineage evidence IDs offline against `raw.json`, verifies CLI `evidence answer` + `evidence verify` (`valid: true`), and verifies `UNKNOWN / INCOMPLETE_SNAPSHOT` abstention for wild-type (`NEGATIVE`) requests and unacquired genes (`KRAS`) (`artifacts/gdc-luad-v3-molecular-milestone/`).
- PDC `PDC000153` (`snapshots/real/pdc_pdc000153`): 115 cases (case-summary only), one captured public GraphQL response, complete in the manifest. The response records the study/version metadata used by the adapter.

File availability metadata in `gdc_tcga_luad_v2` is not evidence of assay completion, QC success, or biomarker status, and open-access WXS Masked Somatic Mutation records in `gdc_tcga_luad_v3` represent called positive somatic variants only (`POSITIVE_ONLY_OPEN_ACCESS_MAF`), never verified negative/wild-type calls. `scripts/inspect_real_snapshots.py` verifies `gdc_tcga_luad`, `gdc_tcga_luad_v2`, `gdc_tcga_luad_v3`, and `pdc_pdc000153` offline into `artifacts/real-data-readiness/real_snapshots_report.json`.

The namespaces remain separate (`gdc:` and `pdc:`). The engine refuses
cross-source assay joins, and no GDC-to-PDC identity mapping is included.
Therefore no patient-overlap claim is made.

`PROOF.md` records the verified commands and outputs. `STATUS.md` records the
current disposition of each work item and research-readiness phase.

## Limitations

- EGIS is scoped to deterministic metadata and molecular-evidence research use
  only; it is **not** clinically validated or cleared for clinical or production
  deployment.
- Checked-in GDC v1 (`gdc_tcga_luad`) and PDC (`pdc_pdc000153`) snapshots are
  case-summary captures only; GDC v2 (`gdc_tcga_luad_v2`) adds biospecimen
  (`sample`/`aliquot`) and scoped RNA-Seq `file` records; GDC v3
  (`gdc_tcga_luad_v3`) adds open-access WXS Masked Somatic Mutation MAF files
  (`618`) and positive `EGFR` `/ssm_occurrences` (`93`) only, leaving negative
  wild-type calls, locus-level callable reference coverage, other genes,
  `pathology_reports`, and `identity_mappings` unacquired.
- Deeper CPTAC/PDC biospecimen/aliquot/file integration remains future work.
- No authoritative cross-cohort identity mapping is present.
- `keys/` ships a non-production demo keypair; external key custody is required
  for production deployment.
- Independent third-party blind evaluation (against the untouched
  `artifacts/cleanroom-holdout-benchmark/`), clinical review, regulatory review,
  and release acceptance remain `PENDING_EXTERNAL`.
