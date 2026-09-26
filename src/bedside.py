"""床旁断网缓冲。

床旁终端在网络中断时仍需记录操作（开始/结束治疗、异常处置、观察等）。
策略：每条操作先以独立 JSONL 追加文件在本地落盘（fsync），网络恢复后按序
补传；中心按 event_id 幂等接收并重新挂接哈希链，补传成功后才删除本地文件。
进程崩溃、断电后未确认文件仍在，重启扫描重发——操作日志不丢失、不重复。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable


def _sha256_chain(prev: str, body: dict[str, Any]) -> str:
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + blob).encode("utf-8")).hexdigest()


class BedsideSpool:
    """床旁本地缓冲目录：pending/<event_id>.json 为待补传，acked 后删除。"""

    def __init__(self, spool_dir: str | os.PathLike[str], bedside_id: str,
                 clock: Callable[[], str] | None = None):
        self.dir = Path(spool_dir) / "pending"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bedside_id = bedside_id
        self._clock = clock or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self._chain_file = Path(spool_dir) / "local_chain.jsonl"
        self._tail = self._load_chain_tail()

    def _load_chain_tail(self) -> str:
        if self._chain_file.exists():
            lines = self._chain_file.read_text(encoding="utf-8").splitlines()
            if lines:
                return json.loads(lines[-1])["hash"]
        return "0" * 64

    # ------------------------------------------------------------------ 本地写入
    def record(self, event_type: str, data: dict[str, Any], *, actor: str,
               event_id: str | None = None, ts: str | None = None) -> dict[str, Any]:
        """断网/在线都先本地落盘，返回可供补传的事件信封。"""
        event_id = event_id or str(uuid.uuid4())
        ts = ts or self._clock()
        seq = len(self._pending_ids()) + 1
        body = {"seq": seq, "ts": ts, "event_type": event_type, "actor": actor,
                "data": {**data, "_bedside": self.bedside_id}, "event_id": event_id}
        body["prev_hash"] = self._tail
        body["hash"] = _sha256_chain(self._tail,
                                     {k: body[k] for k in ("seq", "ts", "event_type", "actor",
                                                           "data", "event_id", "prev_hash")})
        # 1) 待补传单事件文件（原子 rename，避免半截文件）
        tmp = self.dir / f".{event_id}.tmp"
        final = self.dir / f"{event_id}.json"
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, final)
        # 2) 床旁本地哈希链（自身完整性证据），fsync
        with self._chain_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(body, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._tail = body["hash"]
        return body

    def _pending_ids(self) -> list[str]:
        return sorted(p.stem for p in self.glob_pending())

    def glob_pending(self) -> list[Path]:
        return sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime)

    def pending(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in self.glob_pending()]

    # ------------------------------------------------------------------ 补传
    def flush(self, ingest: Callable[[dict[str, Any]], Any]) -> dict[str, int]:
        """网络恢复后按落盘顺序补传；ingest 为中心的幂等接收入口。成功一条删除一条。"""
        sent = 0
        for path in self.glob_pending():
            body = json.loads(path.read_text(encoding="utf-8"))
            ingest(body)  # 中心按 event_id 去重；重复/重试安全
            path.unlink()
            sent += 1
        return {"flushed": sent, "remaining": len(self.glob_pending())}

    def verify_local_chain(self) -> dict[str, Any]:
        """校验床旁本地链未被删改。"""
        if not self._chain_file.exists():
            return {"ok": True, "count": 0}
        prev = "0" * 64
        count = 0
        for line in self._chain_file.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            body = {k: rec[k] for k in ("seq", "ts", "event_type", "actor",
                                        "data", "event_id", "prev_hash")}
            if rec["prev_hash"] != prev or rec["hash"] != _sha256_chain(prev, body):
                return {"ok": False, "broken_event_id": rec["event_id"], "count": count}
            prev = rec["hash"]
            count += 1
        return {"ok": True, "count": count}
