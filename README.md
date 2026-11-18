# Cancer Evidence Engine

Cancer Evidence Engine is a non-generative, proof-carrying metadata prototype for public TCGA and CPTAC repository metadata. It runs queries only against an explicit frozen snapshot. It does not call an LLM, make patient joins, infer clinical meaning, or make clinical, prognostic, biomarker, or causal claims.

## Quick start

```powershell
python -m evidence_engine demo --out artifacts/demo
python -m unittest discover -s tests -v
```

The demo writes a SQLite snapshot, normalized JSON, a query, a certificate, schemas, and a replay report. The synthetic fixture is clearly labeled and is not portal data.

Equivalent interface commands:

```powershell
python -m evidence_engine snapshot fetch --source synthetic --out artifacts/snapshot
python -m evidence_engine snapshot inspect --snapshot artifacts/snapshot
python -m evidence_engine answer --query examples/overlap.json --snapshot artifacts/snapshot --certificate artifacts/certificate.json
python -m evidence_engine verify --certificate artifacts/certificate.json --snapshot artifacts/snapshot
```

`--source gdc` and `--source pdc` emit bounded request plans with `PENDING_EXTERNAL`; they do not perform network access. The runtime is offline after snapshot acquisition.

## Scope and evidence

The GDC adapter boundary is based on the official GDC search/retrieval API and data model documentation. GDC documents project, case, and file endpoints, filters, fields, pagination, entities, properties, and typed links. The intended GDC project is `TCGA-LUAD`.

For PDC, the documented selection is `PDC000153`, `CPTAC LUAD Discovery Study - Proteome`, version UUID `f1c59a53-ab7c-11e9-9a07-0a80fada099c`, analytical fraction `Proteome`, and experiment type `TMT10`. A real adapter must call the study catalog first and freeze the version UUID; live portal counts are never copied into the synthetic snapshot.

No GDC-to-PDC identity mapping is included. Similar disease labels or identifiers are not an identity join.

## Honest release status

- Offline synthetic implementation: development path implemented and covered by the public tests.
- Real GDC/PDC acquisition: `PENDING_EXTERNAL`; no network snapshot, completeness proof, or current totals are claimed here.
- Independent held-out evaluator: `PENDING_EXTERNAL`; the runner exists, but expected inputs and scoring remain outside this repository.
- Clinical or regulatory use: out of scope.

See [GATES.md](GATES.md), [PLAN.md](PLAN.md), [docs/evidence-report.md](docs/evidence-report.md), and the seven [handoffs](handoffs/).

