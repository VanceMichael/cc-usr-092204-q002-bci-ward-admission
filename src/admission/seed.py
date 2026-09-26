"""种子数据载入：按依赖顺序把 fixtures 灌入服务。"""
from __future__ import annotations

import json
from pathlib import Path

from src.admission.service import AdmissionService


def load_seed(service: AdmissionService, seed_path: str | Path) -> None:
    data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    for record in data.get("institutions", []):
        service.add_institution(**record)
    for record in data.get("standards", []):
        service.publish_standard(**record)
    for record in data.get("ethics", []):
        service.add_ethics_approval(**record)
    for record in data.get("cases", []):
        service.add_case(**record)
    for record in data.get("consents", []):
        service.add_consent(**record)
    for record in data.get("batches", []):
        service.add_batch(**record)
    for record in data.get("operators", []):
        service.add_operator(**record)
    for record in data.get("plans", []):
        service.create_plan(**record)
    for record in data.get("observations", []):
        service.add_observation(**record)
