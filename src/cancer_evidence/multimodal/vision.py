from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Protocol

from .adapters import AdapterPayload
from .models import MappingStatus, QCStatus, ReportStatus


@dataclass(frozen=True)
class EmbeddingRecord:
    canonical_patient_id: str
    source_specimen_id: str
    sample_type: str
    slide_id: str
    model_id: str
    device: str
    image_sha256: str
    embedding_dim: int
    l2_norm: float
    vector: tuple[float, ...]
    collection_day_offset: int | None = None
    episode_id: str | None = None
    extra_metrics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SlideEncoder(Protocol):
    """Deterministic vision feature-extractor contract (CLIP / PLIP / H-optimus-0)."""

    model_id: str
    device: str

    def encode_image(self, image_path: Path) -> tuple[list[float], str]: ...


def _sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _l2_normalize(values: list[float]) -> tuple[list[float], float]:
    norm = math.sqrt(sum(v * v for v in values))
    if not math.isfinite(norm) or norm == 0.0:
        return values, 0.0
    return [round(v / norm, 6) for v in values], round(norm, 6)


class ClipPlipSlideEncoder:
    """Free, ungated CLIP / PLIP pathology image encoder (runs on CPU or CUDA)."""

    def __init__(self, model_id: str = "vinid/plip", device: str = "auto") -> None:
        import torch
        from transformers import CLIPModel, CLIPProcessor

        self.model_id = model_id
        self.device = ("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else device
        self._torch = torch
        self._processor = CLIPProcessor.from_pretrained(model_id)
        self._model = CLIPModel.from_pretrained(model_id).to(self.device)
        self._model.eval()

    def encode_image(self, image_path: Path) -> tuple[list[float], str]:
        from PIL import Image

        img_sha = _sha256_bytes(image_path)
        image = Image.open(image_path).convert("RGB")
        inputs = self._processor(images=image, return_tensors="pt").to(self.device)
        with self._torch.no_grad():
            feats = self._model.get_image_features(**inputs)
        vec = [float(x) for x in feats[0].detach().cpu().tolist()]
        return vec, img_sha


class HOptimus0SlideEncoder:
    """Bioptimus H-optimus-0 (1.1B ViT-giant, Apache-2.0) pluggable GPU encoder.

    Requires an NVIDIA GPU (e.g., free Google Colab T4) and an HF_TOKEN environment
    variable from a Hugging Face account that has accepted bioptimus/H-optimus-0 terms.
    """

    H_OPTIMUS_MEAN = (0.707223, 0.578729, 0.703617)
    H_OPTIMUS_STD = (0.211883, 0.230117, 0.177517)

    def __init__(self, model_id: str = "hf-hub:bioptimus/H-optimus-0", device: str = "cuda") -> None:
        import timm
        import torch
        from huggingface_hub import login
        from torchvision import transforms

        token = os.environ.get("HF_TOKEN")
        if token:
            login(token=token, add_to_git_credential=False)

        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError(
                "H-optimus-0 (1.1B params) requires a CUDA GPU. "
                "Use notebooks/extract_h_optimus_0_colab.ipynb on Google Colab Free T4, "
                "or use ClipPlipSlideEncoder on CPU."
            )

        self.model_id = model_id
        self.device = device
        self._torch = torch
        self._model = timm.create_model(
            model_id,
            pretrained=True,
            init_values=1e-5,
            dynamic_img_size=False,
        ).to(self.device)
        self._model.eval()
        self._transform = transforms.Compose([
            transforms.Resize((224, 224)),
            transforms.ToTensor(),
            transforms.Normalize(mean=self.H_OPTIMUS_MEAN, std=self.H_OPTIMUS_STD),
        ])

    def encode_image(self, image_path: Path) -> tuple[list[float], str]:
        from PIL import Image

        img_sha = _sha256_bytes(image_path)
        image = Image.open(image_path).convert("RGB")
        tensor = self._transform(image).unsqueeze(0).to(self.device)
        with self._torch.inference_mode(), self._torch.autocast(
            device_type="cuda", dtype=self._torch.float16
        ):
            feats = self._model(tensor)
        vec = [float(x) for x in feats[0].detach().float().cpu().tolist()]
        return vec, img_sha


class VisionEmbeddingAdapter:
    """Pluggable ModalityAdapter turning slide embeddings (PLIP/CLIP or H-optimus-0) into EGIS tables."""

    modality_name = "vision_slide"
    source_name = "idc"

    def ingest(self, *, records: list[EmbeddingRecord]) -> AdapterPayload:
        crosswalk: list[dict[str, Any]] = []
        seen_cw: set[str] = set()
        runs: list[dict[str, Any]] = []
        feature_rows: list[dict[str, Any]] = []

        for rec in records:
            pid = rec.canonical_patient_id
            if pid not in seen_cw:
                seen_cw.add(pid)
                crosswalk.append({
                    "canonical_patient_id": pid,
                    "source": self.source_name,
                    "source_patient_id": pid,
                    "mapping_status": MappingStatus.APPROVED.value,
                    "authority": "NCI-IDC",
                    "evidence_note": f"IDC CPTAC-LUAD slide mapping for {pid}",
                })

            norm_vec, raw_norm = _l2_normalize(list(rec.vector))
            all_finite = bool(norm_vec) and all(math.isfinite(x) for x in norm_vec) and raw_norm > 0.0
            qc_status = QCStatus.PASS.value if all_finite else QCStatus.FAIL.value

            features_summary: dict[str, Any] = {
                "model_id": rec.model_id,
                "device": rec.device,
                "embedding_dim": len(norm_vec),
                "raw_l2_norm": raw_norm,
                "embedding_preview_8": norm_vec[:8],
            }
            if rec.extra_metrics:
                features_summary["metrics"] = rec.extra_metrics

            runs.append({
                "run_id": f"idc:vision:{rec.slide_id}",
                "modality": self.modality_name,
                "canonical_patient_id": pid,
                "specimen_id": f"idc:{rec.source_specimen_id}",
                "source_specimen_id": rec.source_specimen_id,
                "episode_id": rec.episode_id,
                "source": self.source_name,
                "source_run_id": rec.slide_id,
                "qc_status": qc_status,
                "report_status": ReportStatus.ACTIVE_FINAL.value,
                "sample_type": rec.sample_type,
                "collection_day_offset": rec.collection_day_offset,
                "pipeline_version": f"vision-encoder:{rec.model_id}",
                "artifact_uri": f"idc://cptac_luad/{rec.slide_id}",
                "artifact_sha256": rec.image_sha256,
                "features_json": json.dumps(features_summary, sort_keys=True),
            })

            feature_rows.append({
                "canonical_patient_id": pid,
                "source_specimen_id": rec.source_specimen_id,
                "slide_id": rec.slide_id,
                "model_id": rec.model_id,
                "image_sha256": rec.image_sha256,
                "embedding_dim": len(norm_vec),
                "embedding_json": json.dumps(norm_vec),
            })

        return AdapterPayload(
            modality_name=self.modality_name,
            source_name=self.source_name,
            identity_crosswalk=crosswalk,
            modality_runs=runs,
            feature_tables={"vision_embeddings": feature_rows},
            source_provenance={
                "source": "NCI-IDC / Pathology Vision Encoder",
                "slides_ingested": len(records),
                "models_used": sorted({r.model_id for r in records}),
            },
        )
