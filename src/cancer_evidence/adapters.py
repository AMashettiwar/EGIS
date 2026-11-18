from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterStatus:
    source: str
    status: str
    reason: str
    request_plan: dict


def gdc_request_plan(project: str = "TCGA-LUAD") -> AdapterStatus:
    return AdapterStatus("gdc", "PENDING_EXTERNAL", "REAL_SOURCE_NOT_RUN", {"method": "GET/POST", "endpoint": "https://api.gdc.cancer.gov/projects/" + project, "fields": ["project_id", "name", "disease_type", "primary_site", "state", "released"], "pagination": {"from": 0, "size": 100}})


def pdc_request_plan(study: str = "PDC000153", study_uuid: str = "f1c59a53-ab7c-11e9-9a07-0a80fada099c") -> AdapterStatus:
    return AdapterStatus("pdc", "PENDING_EXTERNAL", "REAL_SOURCE_NOT_RUN", {"method": "POST", "endpoint": "https://pdc.cancer.gov/graphql", "study_id": study, "version_uuid": study_uuid, "query": "studyCatalog then study"})

