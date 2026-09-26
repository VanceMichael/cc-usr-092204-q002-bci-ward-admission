"""HTTP 入口：准入后台的 JSON API。

运行：python3 -m src.server [--port 8000] [--store data/store.json] [--seed fixtures/seed.json]
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from src.admission.seed import load_seed
from src.admission.service import AdmissionService
from src.admission.store import Store
from src.catalog import load_context


def _handle_health(handler, service):
    return {"status": "ok"}


def _handle_context(handler, service):
    return load_context()


def _handle_list_standards(handler, service):
    from dataclasses import asdict

    return [asdict(item) for item in service.store.all("standards")]


def _handle_publish_standard(handler, service):
    return service.publish_standard(**handler.body())


def _handle_create(resource):
    def handle(handler, service):
        return getattr(service, f"add_{resource}")(**handler.body())

    return handle


def _handle_create_plan(handler, service):
    return service.create_plan(**handler.body())


def _handle_get_plan(handler, service, plan_id):
    view = service.plan_view(plan_id, handler.role(), **handler.role_ctx())
    if view is None:
        raise PermissionError("无权查看该计划")
    return view


def _handle_clearance(handler, service, plan_id):
    body = handler.body()
    return service.evaluate(plan_id, decided_by=body.get("decided_by", "system"))


def _handle_manual_release(handler, service, plan_id):
    body = handler.body()
    return service.manual_release(
        plan_id, actor=body["actor"], reason=body["reason"]
    )


def _handle_resolve(handler, service, plan_id):
    body = handler.body()
    kind = body["kind"]
    actor = body.get("actor", "system")
    if kind == "supplement":
        return service.resolve_supplement(plan_id, body["note"], actor)
    if kind == "reapproval":
        return service.resolve_reapproval(plan_id, body["note"], actor)
    if kind == "resume":
        return service.resume_plan(plan_id, actor)
    raise ValueError(f"未知恢复类型：{kind}")


def _handle_repin(handler, service, plan_id):
    body = handler.body()
    return service.repin_standard(plan_id, body["version"], body.get("actor", "system"))


def _handle_batch_change(handler, service, batch_id):
    body = handler.body()
    return service.change_batch_versions(
        batch_id, body["changes"], body.get("reason", ""), body.get("actor", "engineer")
    )


def _handle_reinstate_batch(handler, service, batch_id):
    body = handler.body()
    return service.reinstate_batch(
        batch_id, body["reason"], body.get("actor", "auditor")
    )


def _handle_adverse(handler, service):
    body = handler.body()
    return service.report_adverse(
        body["plan_id"], body["severity"], body["description"], body.get("actor", "system")
    )


def _handle_log_sync(handler, service):
    body = handler.body()
    return service.sync_logs(body["terminal_id"], body["entries"])


def _handle_reconstruct(handler, service, decision_id):
    return service.reconstruct(decision_id)


def _handle_plan_trail(handler, service, plan_id):
    return service.plan_trail(plan_id)


ROUTES = [
    ("GET", re.compile(r"^/health$"), _handle_health),
    ("GET", re.compile(r"^/context$"), _handle_context),
    ("GET", re.compile(r"^/api/standards$"), _handle_list_standards),
    ("POST", re.compile(r"^/api/standards$"), _handle_publish_standard),
    ("POST", re.compile(r"^/api/institutions$"), _handle_create("institution")),
    ("POST", re.compile(r"^/api/ethics$"), _handle_create("ethics_approval")),
    ("POST", re.compile(r"^/api/cases$"), _handle_create("case")),
    ("POST", re.compile(r"^/api/consents$"), _handle_create("consent")),
    ("POST", re.compile(r"^/api/batches$"), _handle_create("batch")),
    ("POST", re.compile(r"^/api/operators$"), _handle_create("operator")),
    ("POST", re.compile(r"^/api/observations$"), _handle_create("observation")),
    ("POST", re.compile(r"^/api/plans$"), _handle_create_plan),
    ("GET", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)$"), _handle_get_plan),
    ("POST", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/clearance$"), _handle_clearance),
    ("POST", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/manual-release$"), _handle_manual_release),
    ("POST", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/resolve$"), _handle_resolve),
    ("POST", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/repin$"), _handle_repin),
    ("POST", re.compile(r"^/api/batches/(?P<batch_id>[^/]+)/changes$"), _handle_batch_change),
    ("POST", re.compile(r"^/api/batches/(?P<batch_id>[^/]+)/reinstate$"), _handle_reinstate_batch),
    ("POST", re.compile(r"^/api/adverse-events$"), _handle_adverse),
    ("POST", re.compile(r"^/api/logs/sync$"), _handle_log_sync),
    ("GET", re.compile(r"^/api/audit/decisions/(?P<decision_id>[^/]+)$"), _handle_reconstruct),
    ("GET", re.compile(r"^/api/audit/plans/(?P<plan_id>[^/]+)/trail$"), _handle_plan_trail),
]


def make_handler(service: AdmissionService, store_path: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def role(self) -> str:
            return self.headers.get("X-Role", "")

        def role_ctx(self) -> dict:
            # 头部仅支持 latin-1，中文取值按 URL 编码传输
            return {
                "institution_id": unquote(self.headers.get("X-Institution", "")),
                "manufacturer": unquote(self.headers.get("X-Manufacturer", "")),
            }

        def _json(self, code: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            for route_method, pattern, handler in ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if not match:
                    continue
                try:
                    result = handler(self, service, **match.groupdict())
                except KeyError as error:
                    return self._json(404, {"error": str(error)})
                except PermissionError as error:
                    return self._json(403, {"error": str(error)})
                except ValueError as error:
                    return self._json(400, {"error": str(error)})
                if method == "POST" and store_path:
                    service.store.save(store_path)
                return self._json(200, result)
            self._json(404, {"error": "未找到资源"})

        def log_message(self, *_args):
            pass

    return Handler


def run(port: int = 8000, store_path: str | None = None, seed_path: str | None = None):
    if store_path and Path(store_path).exists():
        store = Store.load(store_path)
    else:
        store = Store()
    service = AdmissionService(store)
    if seed_path:
        load_seed(service, seed_path)
    handler = make_handler(service, store_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"准入后台已启动：http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="脑机病房设备准入控制后台")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--store", default=None, help="持久化文件路径")
    parser.add_argument("--seed", default=None, help="启动时载入的种子数据")
    args = parser.parse_args()
    run(port=args.port, store_path=args.store, seed_path=args.seed)
