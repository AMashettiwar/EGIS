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

## Query and denominator contract

The supported overlap query is in `examples/overlap.json`. It asks for case
overlap between two assay selectors. `denominator_mode` may be `left`,
`eligible`, or `universe`; `eligible` and `universe` require a
snapshot-derived `cohort_filter`.

For the frozen synthetic snapshot, an eligible-cohort query returns one
overlapping case out of two eligible TCGA-LUAD fixture cases. The denominator
is explicitly labeled as `snapshot_eligible_cases`; it is not inferred from
the IDs in the query.

Missing, incomplete, stale, conflicting, malformed, ambiguous, or unsupported
inputs produce explicit statuses and reason codes. They are never converted to
a smaller zero result.

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

The current report kills four of four deliberate engine mutations.

## Public snapshots

Acquisition is a separate, networked operation. Answer and verify remain
offline after acquisition.

```powershell
python scripts/acquire_public_snapshots.py gdc --out snapshots/real/gdc_tcga_luad
python scripts/acquire_public_snapshots.py pdc --out snapshots/real/pdc_pdc000153
```

The checked-in snapshots were acquired from public metadata on 2026-11-18:

- GDC `TCGA-LUAD`: 585 cases, three captured pages, complete in the manifest.
- PDC `PDC000153`: 115 cases, one captured public GraphQL response, complete in
  the manifest. The response records the study/version metadata used by the
  adapter.

These are source snapshots, not live counts. Re-run acquisition to obtain a
newly dated snapshot and inspect its hashes before use.

The namespaces remain separate (`gdc:` and `pdc:`). The engine refuses
cross-source assay joins, and no GDC-to-PDC identity mapping is included.
Therefore no patient-overlap claim is made.

## Verification

```powershell
python -m pytest -q
python scripts/mutation_check.py
```

`PROOF.md` records the commands and outputs for W1–W9. `STATUS.md` records the
current disposition of each work item. The old fixture-only package was moved
under `legacy/synthetic_demo/` and is not a runtime product package.

## Limitations

- The synthetic fixture is not TCGA or CPTAC data.
- The frozen GDC/PDC snapshots are public metadata captures, not controlled
  access data and not a clinical cohort analysis.
- No authoritative cross-cohort identity mapping is present.
- Blind external evaluation, clinical review, regulatory review, and release
  acceptance remain `PENDING_EXTERNAL`.
