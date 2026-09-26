"""准入后台 HTTP 服务入口。

- POST /api/command          通用命令入口 {command, actor, args}
- POST /api/clearance        治疗前放行核对（按查看者角色裁剪字段）
- POST /api/timeline         病例全量时间线（审计还原）
- POST /api/replay           按历史时点重放并核对
- POST /api/bedside/ingest   床旁断网日志补传（幂等）
- GET  /api/verify           哈希链完整性校验
- GET  /health /context      原有健康检查与领域上下文
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from src import access
from src.catalog import load_context
from src.eventstore import EventStore
from src.service import AdmissionService

LOG_PATH = os.environ.get("ADMISSION_LOG", str(Path(__file__).resolve().parents[1] / "data" / "admission_log.jsonl"))


def build_service() -> AdmissionService:
    return AdmissionService(EventStore(LOG_PATH))


def _to_jsonable(obj):
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


class Handler(BaseHTTPRequestHandler):
    service: AdmissionService = None  # type: ignore[assignment]

    def _send(self, code: int, payload) -> None:
        body = json.dumps(_to_jsonable(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def log_message(self, fmt, *args) -> None:  # 安静日志
        return

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"status": "ok"})
        elif path == "/context":
            self._send(200, load_context())
        elif path == "/api/verify":
            self._send(200, self.service.store.verify_chain())
        elif path == "/api/cases":
            self._send(200, {"cases": list(self.service.reg.cases.values())})
        elif path == "/api/devices":
            self._send(200, {"devices": list(self.service.reg.devices.values())})
        else:
            self.send_error(404)

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/command":
                self._send(200, self._dispatch(body))
            elif path == "/api/clearance":
                decision = self.service.evaluate_clearance(
                    case_id=body["case_id"], setting=body.get("setting"),
                    operator_id=body.get("operator_id"), actor=body.get("actor"))
                self._send(200, access.redact_decision(decision, body["actor"]))
            elif path == "/api/timeline":
                timeline = self.service.case_timeline(body["case_id"])
                plan = (timeline["plan_snapshots"] or [None])[-1]
                access.assert_case_scope(body["actor"], timeline["case"] or {}, plan,
                                         self.service.reg)
                self._send(200, access.redact_timeline(timeline, body["actor"]))
            elif path == "/api/replay":
                view = self.service.as_of_view(body["as_of"], body["case_id"],
                                               setting=body.get("setting"))
                self._send(200, view)
            elif path == "/api/bedside/ingest":
                ingested = [self.service.ingest_bedside(evt) for evt in body.get("events", [])]
                self._send(200, {"ingested": len(ingested),
                                 "event_ids": [e.event_id for e in ingested]})
            else:
                self.send_error(404)
        except KeyError as exc:
            self._send(400, {"error": "BAD_REQUEST", "message": f"缺少字段：{exc}"})
        except Exception as exc:  # 领域错误可读返回，其余 500
            code = getattr(exc, "code", "INTERNAL")
            self._send(409 if code != "INTERNAL" else 500,
                       {"error": code, "message": getattr(exc, "message", str(exc))})

    def _dispatch(self, body: dict) -> dict:
        command = body["command"]
        actor = body.get("actor", {"id": "anonymous", "role": ""})
        args = body.get("args", {})
        fn = getattr(self.service, command, None)
        if fn is None or command.startswith("_"):
            from src import models as M
            raise M.DomainError("UNKNOWN_COMMAND", command)
        result = fn(actor, **args)
        return {"command": command, "result": _to_jsonable(result)}


def main() -> None:
    Handler.service = build_service()
    port = int(os.environ.get("PORT", "8000"))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
