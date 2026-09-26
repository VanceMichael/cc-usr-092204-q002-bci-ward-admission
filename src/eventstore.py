"""追加式事件存储。

每条记录是一条不可变事件，通过 prev_hash 串成哈希链：任何一条被删改，
从该点起的链式校验都会失败。事件先追加到内存列表并落盘（WAL，fsync），
再更新投影——这是床旁“操作日志不得丢失”与审计可还原的共同基础。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
import copy
from pathlib import Path
from typing import Any, Callable, Iterable


def _hash_payload(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Event:
    __slots__ = ("seq", "ts", "event_type", "actor", "data", "event_id", "prev_hash", "hash")

    def __init__(self, seq: int, ts: str, event_type: str, actor: str,
                 data: dict[str, Any], event_id: str, prev_hash: str, hash: str):
        self.seq = seq
        self.ts = ts
        self.event_type = event_type
        self.actor = actor
        self.data = data
        self.event_id = event_id
        self.prev_hash = prev_hash
        self.hash = hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "event_type": self.event_type,
            "actor": self.actor,
            "data": self.data,
            "event_id": self.event_id,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            seq=raw["seq"], ts=raw["ts"], event_type=raw["event_type"],
            actor=raw["actor"], data=raw["data"], event_id=raw["event_id"],
            prev_hash=raw["prev_hash"], hash=raw["hash"],
        )


class TamperError(RuntimeError):
    pass


class EventStore:
    """线程安全的追加日志；支持文件持久化、重放和幂等去重。"""

    GENESIS_HASH = "0" * 64

    def __init__(self, path: str | os.PathLike[str] | None = None,
                 clock: Callable[[], str] | None = None):
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._seen_ids: set[str] = set()
        self._path = Path(path) if path else None
        self._clock = clock or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if self._path and self._path.exists():
            self._load()

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        assert self._path is not None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            evt = Event.from_dict(json.loads(line))
            self._verify_link(evt)
            self._events.append(evt)
            self._seen_ids.add(evt.event_id)

    def _append_line(self, evt: Event) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 以追加方式写单行，崩溃时最多损坏最后一行，不影响此前日志
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(evt.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _verify_link(self, evt: Event) -> None:
        expected_prev = self._events[-1].hash if self._events else self.GENESIS_HASH
        if evt.prev_hash != expected_prev:
            raise TamperError(f"事件 #{evt.seq} 断链：前哈希不匹配")
        payload = {k: getattr(evt, k) for k in Event.__slots__ if k != "hash"}
        if _hash_payload(payload) != evt.hash:
            raise TamperError(f"事件 #{evt.seq} 内容哈希不匹配，日志可能被篡改")

    # ------------------------------------------------------------------ 写入
    def append(self, event_type: str, data: dict[str, Any], *, actor: str = "system",
               event_id: str | None = None, ts: str | None = None) -> Event:
        """追加事件；相同 event_id 重复提交时返回原事件（床旁补传幂等）。"""
        with self._lock:
            event_id = event_id or str(uuid.uuid4())
            if event_id in self._seen_ids:
                return next(e for e in self._events if e.event_id == event_id)
            seq = len(self._events) + 1
            ts = ts or self._clock()
            prev_hash = self._events[-1].hash if self._events else self.GENESIS_HASH
            # 深拷贝：事件一旦入链即不可变，投影对同一 dict 的后续原地修改不得改变事件哈希
            evt = Event(seq, ts, event_type, actor, copy.deepcopy(data), event_id, prev_hash, "")
            payload = {k: getattr(evt, k) for k in Event.__slots__ if k != "hash"}
            evt.hash = _hash_payload(payload)
            self._append_line(evt)
            self._events.append(evt)
            self._seen_ids.add(event_id)
            return evt

    def ingest(self, raw: dict[str, Any]) -> Event:
        """接收床旁端预先生成的事件（断网期间本地已落盘），按 event_id 幂等、按哈希链校验。"""
        evt = Event.from_dict(raw)
        with self._lock:
            if evt.event_id in self._seen_ids:
                return next(e for e in self._events if e.event_id == evt.event_id)
            # 床旁事件序号可能与中心序列不一致；以中心链重新挂接，但保留原始 seq/ts 于 data
            original = {k: raw[k] for k in ("seq", "ts")}
            evt.data.setdefault("_bedside_original", original)
            return self.append(
                evt.event_type, evt.data, actor=evt.actor,
                event_id=evt.event_id, ts=evt.ts,
            )

    # ------------------------------------------------------------------ 读取
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def replay(self, handler: Callable[[Event], None], *, until_seq: int | None = None,
               until_ts: str | None = None) -> None:
        """按序重放事件构建投影；until_* 支持事后还原“当时”的系统状态。"""
        with self._lock:
            snapshot = list(self._events)
        for evt in snapshot:
            if until_seq is not None and evt.seq > until_seq:
                break
            if until_ts is not None and evt.ts > until_ts:
                break
            handler(evt)

    def verify_chain(self) -> dict[str, Any]:
        with self._lock:
            prev = self.GENESIS_HASH
            for evt in self._events:
                payload = {k: getattr(evt, k) for k in Event.__slots__ if k != "hash"}
                if evt.prev_hash != prev or _hash_payload(payload) != evt.hash:
                    return {"ok": False, "broken_at_seq": evt.seq, "count": len(self._events)}
                prev = evt.hash
            return {"ok": True, "broken_at_seq": None, "count": len(self._events)}

    def tail_hash(self) -> str:
        with self._lock:
            return self._events[-1].hash if self._events else self.GENESIS_HASH
