# Architecture

```text
raw source response / synthetic fixture
             |
             v
   atomic snapshot (manifest + SQLite + READY)
             |
             v
       typed entity/edge tables
             |
             v
 deterministic structured query engine ----> answer certificate
             |                                      |
             +------------------------------> independent replay checker
```

The production answer function and replay checker have separate execution paths. They share only the snapshot schema and stable query vocabulary; the replay checker reimplements set selection, lineage traversal, and evidence-reference reconstruction.

