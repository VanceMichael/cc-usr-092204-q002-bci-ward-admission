"""床旁操作日志：断网不丢、链式防篡改、幂等同步。

床旁终端先把操作追加到本地日志池（JSONL，哈希链），网络恢复后
再同步到中心端；中心端校验序号连续性与哈希链，按 entry_id 去重，
重复同步不会重复入账。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.admission.store import Store

GENESIS = "GENESIS"


def _canonical(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def entry_hash(entry: dict) -> str:
    body = {key: value for key, value in entry.items() if key != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


class BedsideLog:
    """床旁终端的本地日志池：先落盘，后同步。"""

    def __init__(self, terminal_id: str, spool_dir: str | Path):
        self.terminal_id = terminal_id
        directory = Path(spool_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.spool = directory / f"{terminal_id}.jsonl"
        self.state_file = directory / f"{terminal_id}.state.json"
        self.entries: list[dict] = []
        if self.spool.exists():
            self.entries = [
                json.loads(line)
                for line in self.spool.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        self.synced_upto = 0
        if self.state_file.exists():
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.synced_upto = state.get("synced_upto", 0)

    def append(self, actor: str, action: str, payload: dict, ts: str) -> dict:
        seq = len(self.entries) + 1
        prev_hash = self.entries[-1]["hash"] if self.entries else GENESIS
        entry = {
            "entry_id": f"{self.terminal_id}-{seq:06d}",
            "terminal_id": self.terminal_id,
            "seq": seq,
            "actor": actor,
            "action": action,
            "payload": payload,
            "ts": ts,
            "prev_hash": prev_hash,
        }
        entry["hash"] = entry_hash(entry)
        with self.spool.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.entries.append(entry)
        return entry

    def pending(self) -> list[dict]:
        return [entry for entry in self.entries if entry["seq"] > self.synced_upto]

    def mark_synced(self, upto_seq: int) -> None:
        self.synced_upto = max(self.synced_upto, upto_seq)
        self.state_file.write_text(
            json.dumps({"synced_upto": self.synced_upto}), encoding="utf-8"
        )


class CentralLog:
    """中心端：校验后并入审计日志，重复条目幂等跳过。"""

    def __init__(self, store: Store):
        self.store = store

    def ingest(self, terminal_id: str, entries: list[dict]) -> dict:
        accepted = 0
        duplicates = 0
        rejected: list[dict] = []
        known_ids = {entry.get("entry_id") for entry in self.store.audit_log}
        last_seq = self.store.terminal_seq.get(terminal_id, 0)
        last_hash = self.store.terminal_hash.get(terminal_id, GENESIS)

        for entry in entries:
            if rejected:
                rejected.append(
                    {"entry_id": entry.get("entry_id"), "reason": "前序条目未通过校验"}
                )
                continue
            entry_id = entry.get("entry_id")
            if entry_id in known_ids or entry.get("seq", 0) <= last_seq:
                duplicates += 1
                continue
            if entry.get("seq") != last_seq + 1:
                rejected.append({"entry_id": entry_id, "reason": "序号不连续"})
                continue
            if entry.get("prev_hash") != last_hash:
                rejected.append({"entry_id": entry_id, "reason": "哈希链断裂"})
                continue
            if entry_hash(entry) != entry.get("hash"):
                rejected.append({"entry_id": entry_id, "reason": "哈希校验失败"})
                continue
            self.store.audit_log.append(
                {
                    "entry_id": entry_id,
                    "actor": entry["actor"],
                    "action": entry["action"],
                    "details": entry["payload"],
                    "ts": entry["ts"],
                    "terminal_id": terminal_id,
                    "seq": entry["seq"],
                    "hash": entry["hash"],
                }
            )
            known_ids.add(entry_id)
            last_seq = entry["seq"]
            last_hash = entry["hash"]
            accepted += 1

        self.store.terminal_seq[terminal_id] = last_seq
        self.store.terminal_hash[terminal_id] = last_hash
        return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}
