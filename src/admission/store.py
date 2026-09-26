"""持久化：集合式存储、审计日志与 JSON 落盘。"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from src.admission import models

COLLECTION_TYPES = {
    "institutions": models.Institution,
    "standards": models.StandardVersion,
    "ethics": models.EthicsApproval,
    "cases": models.CaseFile,
    "consents": models.Consent,
    "batches": models.DeviceBatch,
    "operators": models.Operator,
    "plans": models.TreatmentPlan,
    "observations": models.Observation,
    "events": models.AdverseEvent,
    "decisions": models.ClearanceDecision,
}


class Store:
    """内存集合 + 审计日志，可整体序列化为 JSON。"""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, object]] = {name: {} for name in COLLECTION_TYPES}
        self.audit_log: list[dict] = []
        self.counters: dict[str, int] = {}
        self.terminal_seq: dict[str, int] = {}
        self.terminal_hash: dict[str, str] = {}

    def put(self, collection: str, key: str, obj: object) -> object:
        self.data[collection][key] = obj
        return obj

    def get(self, collection: str, key: str):
        try:
            return self.data[collection][key]
        except KeyError:
            raise KeyError(f"{collection} 中不存在 {key}") from None

    def all(self, collection: str) -> list:
        return list(self.data[collection].values())

    def find(self, collection: str, **conditions) -> list:
        return [
            obj
            for obj in self.all(collection)
            if all(getattr(obj, key) == value for key, value in conditions.items())
        ]

    def next_id(self, prefix: str, width: int = 4) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}-{self.counters[prefix]:0{width}d}"

    def log(self, actor: str, action: str, details: dict, ts: str) -> dict:
        entry = {
            "entry_id": self.next_id("LOG", 6),
            "actor": actor,
            "action": action,
            "details": details,
            "ts": ts,
        }
        self.audit_log.append(entry)
        return entry

    def save(self, path: str | Path) -> None:
        payload = {
            "collections": {
                name: {key: asdict(obj) for key, obj in items.items()}
                for name, items in self.data.items()
            },
            "audit_log": self.audit_log,
            "counters": self.counters,
            "terminal_seq": self.terminal_seq,
            "terminal_hash": self.terminal_hash,
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> "Store":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        store = cls()
        for name, records in payload["collections"].items():
            model = COLLECTION_TYPES[name]
            store.data[name] = {key: model(**record) for key, record in records.items()}
        store.audit_log = payload["audit_log"]
        store.counters = payload["counters"]
        store.terminal_seq = payload.get("terminal_seq", {})
        store.terminal_hash = payload.get("terminal_hash", {})
        return store
