# EGIS Lung Adenocarcinoma Discovery & Blind External Validation Checkpoint (`v1`)

> **Checkpoint Tag:** `checkpoint-luad-981-blind-v1` (branch `adc`)  
> **Scope:** Public multi-cohort Lung Adenocarcinoma (LUAD) statistical discovery (`n = 981`) + pre-registered blind external validation (`n = 1,473` unseen patients, `0` patient overlap) + 30× shuffled-noise negative-control test. Zero LLM inside discovery or statistical execution.

---

## 1. Frozen Discovery Cohort (`n = 981` Real LUAD Patients)
- Manifest: [`snapshots/discovery/luad_981_v1/manifest.json`](../snapshots/discovery/luad_981_v1/manifest.json) (`manifest_sha256: d94a7fc81bbd3b81...`)
- **TCGA PanCancer Atlas (`luad_tcga_pan_can_atlas_2018`)**: `566` patients (`566` WXS, `510` RNA, `505` OS / `186` deaths)
- **OncoSG East Asian (`luad_oncosg_2020`)**: `305` patients (`302` WXS, `169` RNA, `302` OS / `96` deaths, tumor purity & smoking)
- **CPTAC *Cell* 2020 (`luad_cptac_2020`)**: `110` patients (`110` WXS, `110` RNA, **`110` Mass-Spec Proteomics**, tumor purity & smoking)

### Key Audited Discovery Findings
- **Co-mutation & Mutual Exclusivity (`n = 978`, 351 pairs):** `82` naive `p < 0.05` → **`30` FDR `q < 0.05`** (`52` false positives removed). Western vs East Asian shift: `EGFR` `12.4%` vs `47.4%` (`OR = 6.37, q = 1.3e-27`).
- **Stage+Age+Sex+Cohort-Adjusted Survival (Cox PH):** `19` naive `p < 0.05` → **`6` FDR `q < 0.05`** (`NKX2-1`, `NAPSA`, `STK11` mutation `HR = 1.94`, `MKI67`, `KRT7`, `SFTA2` — all `100%` bootstrap same-direction and concordant across Western TCGA & East Asian OncoSG); **`7` naive hits killed by confounder control** (`KEAP1`, `TP53`, `KRAS`, `BCL2`, `NFE2L2`).
- **Gold Genotype → ADC Target / Payload Reprogramming (FDR `q < 0.05` + Purity-Adjusted `p < 0.05` + CPTAC Mass-Spec Protein Replicated `p < 0.05`):**
  - `STK11` mut → `MSLN` (Mesothelin) **UP** (`RNA +0.74, q = 1.7e-9`; `CPTAC Protein +0.68, p = 0.0072`; `0` PubMed papers)
  - `STK11` mut → `MET` **DOWN** (`RNA -0.95`; `CPTAC Protein -1.19, p < 0.0001`) & `EGFR` **DOWN** (`CPTAC Protein -1.03, p < 0.0001`)
  - `RB1` mut → `SLFN11` (Topo-I / DXd payload sensitivity) **UP** (`RNA +0.83`; `CPTAC Protein +1.41, p = 0.0001`)
  - `TP53` mut → `TUBB3` (microtubule/MMAE resistance) **UP** (`RNA +0.52`; `CPTAC Protein +0.62, p = 0.0011`; `0` PubMed papers)
  - `KRAS` mut → `TOP1` (DXd/SN-38 target) **UP** (`RNA +0.43`; `CPTAC Protein +0.42, p = 0.044`; `1` PubMed paper)
- **Epistatic 2-way survival (`35` pairs ≥15 double mutants):** `TP53 × PIK3CA` reached nominal `p = 0.0021`, **rejected by FDR (`q = 0.074`)** → `0` FDR-significant epistatic interactions claimed.

---

## 2. Pre-Registered Blind External Validation (`n = 1,473` Unseen Patients, `0` Overlap)
- **MSKCC 2020 Surgical LUAD (`luad_mskcc_2020`, `n = 604`, `45` OS deaths, `98` RFS recurrences):**
  - Co-mutation blind replication: **`7 / 7` (`100%`) replicated at `p < 0.05`** (`EGFR×KRAS` `p = 1.1e-40`, `STK11×KEAP1` `p = 3.5e-22`, `EGFR×STK11` `p = 2.4e-14`, `EGFR×KEAP1` `p = 6.0e-12`, `TP53×RB1` `p = 0.0013`, `KRAS×ATM` `p = 0.0019`, `KRAS×STK11` `p = 0.025`).
  - Borderline `TP53 × PIK3CA` (rejected by FDR): **FAILED (`HR = 0.64, p = 0.70`)** — confirming FDR prevented a false positive.
  - `STK11` OS in early-stage surgical MSK-2020 (`45` deaths): **Did not replicate in OS (`HR = 0.90, p = 0.79`)**; RFS showed adverse trend (`HR = 1.46, p = 0.14`).
- **MSKCC 2023 Metastatic LUAD (`luad_mskcc_2023_met_organotropism`, `n = 766` with OS, `233` deaths):**
  - `STK11` OS: **REPLICATED (`HR = 1.82 [1.35–2.44], p = 7.01e-05`)**.
  - Borderline `TP53 × PIK3CA`: **FAILED again (`HR = 0.80, p = 0.58`)**.
- **CAS *Cell* 2020 (`luad_cas_2020`, `n = 103`, `51` RNA):** Underpowered for `STK11`/`KRAS`/`RB1` (`0–2` carriers) and missing `MSLN`/`TUBB3` in public RNA matrix; evaluable directions matched (`TP53→BRCA1`, `SMARCA4→CEACAM5`, `MKI67` OS/DFS) without reaching `p < 0.05` at `n = 51`.
- **30× Shuffled-Noise Negative Control:** Naive `p < 0.05` produced `3.13` false positives/run vs EGIS FDR `q < 0.05` `0.20`/run (`0` false discoveries in `80%` of runs — **`15.7×` reduction**).

---

## 3. Reproduce Anytime
```bash
pip install -e ".[multimodal,discovery,test]"
python scripts/run_luad_981_discovery.py
python scripts/run_blind_external_validation.py
pytest -q tests/test_discovery_engine.py
```
