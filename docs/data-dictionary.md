# Data dictionary

| Entity | Meaning in this prototype | Identity rule |
|---|---|---|
| case | source-specific participant/case metadata record | namespaced `source:case:<source_id>` |
| sample | source-specific biospecimen sample | explicit edge from case |
| aliquot | source-specific aliquot | explicit edge from case/sample when present |
| assay | typed catalog record such as `rna_expression` or `protein_abundance` | never inferred from file names |
| file | metadata record for a source file | explicit edge from assay |

Unknown, missing linkage, incomplete retrieval, and observed zero are separate conditions in the result status/reason code path.

