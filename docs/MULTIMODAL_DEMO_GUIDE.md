# EGIS Multimodal Umbrella — 10-Minute Walkthrough Guide

> **Plain-English Goal:** Show how one deterministic, zero-LLM backbone unifies messy data across siloed departments (Clinical, Genomics/RNA, Proteomics, and Pathology Slide Images) in seconds — with zero silent errors and an easy plug-in path for ADC research.

---

## 1. Quick Commands (100% Free Stack)

```bash
# 1. Install multimodal dependencies (DuckDB + PyArrow)
pip install -e ".[multimodal,test]"

# 2. Acquire real public Lung Cancer data from TWO independent NCI portals:
#    - NCI GDC (CPTAC-3 Lung: Clinical + Open RNA-Seq TPM + Open WXS Mutations)
#    - NCI PDC (PDC000153: Quantitative Mass-Spec Proteomics)
python scripts/acquire_cptac3_lung_multimodal.py

# 3. Run the offline deterministic unification & generate the 10-minute HTML demo
python scripts/run_multimodal_demo.py
# Open: artifacts/multimodal-demo/cptac3_lung_v1/index.html

# 4. Run full automated verification
pytest -q
```

---

## 2. Optional Free GPU Step (`H-optimus-0` on Google Colab)

Because `bioptimus/H-optimus-0` is a 1.1B-parameter pathology foundation model requiring an NVIDIA GPU:
1. Create a free Hugging Face account and accept the Apache-2.0 model card at `https://huggingface.co/bioptimus/H-optimus-0`.
2. Open [`notebooks/extract_h_optimus_0_colab.ipynb`](../notebooks/extract_h_optimus_0_colab.ipynb) in **Google Colab** (`Runtime -> Change runtime type -> T4 GPU`, free tier).
3. Add your Hugging Face token to **Colab Secrets** as `HF_TOKEN` (**never** hardcode tokens in code or commit them to Git).
4. Run all cells -> downloads open NCI IDC `cptac_luad` H&E slides and writes `vision_embeddings_h_optimus_0.parquet`, which plugs directly into `VisionEmbeddingAdapter` with zero engine changes.
5. For local CPU-only runs without a GPU or token, `ClipPlipSlideEncoder` (`vinid/plip` or `openai/clip-vit-base-patch32`) implements the exact same `SlideEncoder` interface.

---

## 3. 10-Minute Manager / CTO Demo Script

- **Minutes 0–2 (The Silo Problem):** Explain why single-modal pipelines break in oncology R&D — Clinical, Genomics, Proteomics/Single-Cell, and Imaging live in separate silos with different IDs, lab controls, sample types, and missing timepoints.
- **Minutes 2–5 (Live Cross-Silo Funnel on Real Lung Data):** Open `artifacts/multimodal-demo/cptac3_lung_v1/index.html`:
  - Show **343 raw cross-silo records** across GDC (`CPTAC-3` Lung) + PDC (`PDC000153`).
  - Point out the **4 real non-patient pooled reference controls** automatically caught and excluded (`CONTROL_OR_NON_PATIENT`) — never counted as human patients.
  - Point out **109 LUSC** separated from **230 eligible LUAD** patients by locked denominator.
  - Point out **2 real `SAMPLE_TYPE_MISMATCH` catches** (preventing Tumor RNA from silently joining to Normal Protein) and **150 `UNKNOWN / MISSING_MODALITY`** abstentions — yielding **80 verified 4-modality `READY` packets**.
- **Minutes 5–7 (Zero-Hallucination Temporal Honesty):** Point to the **Strict Timestamp-Only Policy** callout (`READY = 0`, `UNKNOWN = 230` when `allow_shared_specimen_fallback=False`) proving the engine refuses to guess missing dates across silos.
- **Minutes 7–10 (Why Direct Protein & the `adc` Branch Add-On Matter):** Show Section 3 (Real RNA vs. Measured Protein across the 80 READY Lung patients):
  - **B7-H3 (`CD276`)**: `30.0%` discordant (24/80)
  - **TROP2 (`TACSTD2`)** & **HER3 (`ERBB3`)**: `25.0%` discordant (20/80 each)
  - **HER2 (`ERBB2`)**: `22.5%` discordant (18/80)
  - Switch to the **`adc` branch** to show how an ADC Target Stratification + Single-Cell CTC Evaluability module plugs into this exact umbrella with **zero core engine rewrites**.
