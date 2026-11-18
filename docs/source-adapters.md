# Source adapter boundary

The shipped adapter functions return request plans and an explicit `PENDING_EXTERNAL` state. They do not perform network calls. This is deliberate: answer and verify must operate on a named snapshot, not on whatever an upstream portal returns today.

## GDC

The plan records the public endpoint family, project `TCGA-LUAD`, fields, and pagination. A production acquisition must record exact request bodies, selected fields, every page response, retrieval timestamp, content hashes, coverage, and the source release/version when available. Repeated, missing, or truncated pages must make the snapshot incomplete rather than produce a definitive zero.

## PDC

The plan records a `studyCatalog`-first workflow for `PDC000153`. The catalog version UUID must be stored before the study query. The study page currently supports the identity `CPTAC LUAD Discovery Study - Proteome`; its live counts are not fixture counts and are not treated as frozen evidence here.

## Identity rule

GDC and PDC entities have separate namespaces. A shared disease label, barcode shape, or similar demographic metadata is not an authoritative cross-source mapping. The engine therefore rejects cross-source overlap queries.

