"""试饮批次治理的 HTTP/JSON 边界，复用基础层路由处理登记类接口。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from night_market_foundation.api import route as foundation_route
from night_market_foundation.errors import DomainError, ValidationError
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

from .service import TastingService


def _respond(response: dict[str, Any], created_status: int = 201) -> tuple[int, dict[str, Any]]:
    """创建类动作首次 201、重放 200；状态变更类动作传 created_status=200。"""

    if response.get("replayed"):
        return 200, response
    if response.get("resource_type") == "claim_decision":
        return 200, response
    return created_status, response


def _tasting_route(service: TastingService, method: str, path: str,
                   body: dict[str, Any], actor_id: str) -> tuple[int, dict[str, Any]]:
    parsed = urlparse(path)
    if method == "POST" and parsed.path == "/tasting/ingredient-batches":
        return _respond(service.register_ingredient_batch(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/tasting/recipe-versions":
        return _respond(service.create_recipe_version(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/tasting/recipe-risk-confirmations":
        return _respond(service.confirm_recipe_risk(actor_id=actor_id, **body), 200)
    if method == "POST" and parsed.path == "/tasting/preparations":
        return _respond(service.brew_preparation(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/tasting/container-fills":
        return _respond(service.fill_container(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/tasting/container-splits":
        return _respond(service.split_container(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/tasting/container-merges":
        return _respond(service.merge_containers(actor_id=actor_id, **body), 200)
    if method == "POST" and parsed.path == "/tasting/losses":
        return _respond(service.record_loss(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/tasting/participant-declarations":
        return _respond(service.declare_restrictions(actor_id=actor_id, **body), 200)
    if method == "POST" and parsed.path == "/tasting/claims":
        return _respond(service.record_claim(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/tasting/transfers":
        return _respond(service.initiate_transfer(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/tasting/transfer-confirmations":
        return _respond(service.confirm_transfer(actor_id=actor_id, **body), 200)
    if method == "POST" and parsed.path == "/tasting/transfer-rejections":
        return _respond(service.reject_transfer(actor_id=actor_id, **body), 200)
    if method == "POST" and parsed.path == "/tasting/ingredient-batch-freezes":
        return _respond(service.freeze_ingredient_batch(actor_id=actor_id, **body), 200)
    if method == "GET" and parsed.path == "/tasting/assessment":
        query = parse_qs(parsed.query)
        holder_type = query.get("holder_type", [""])[0]
        holder_id = query.get("holder_id", [""])[0]
        participant_id = query.get("participant_id", [""])[0]
        if not (holder_type and holder_id and participant_id):
            raise ValidationError("holder_type、holder_id 与 participant_id 不能为空")
        return 200, service.assess(holder_type=holder_type, holder_id=holder_id,
                                   participant_id=participant_id)
    if method == "GET" and parsed.path == "/tasting/lineage":
        query = parse_qs(parsed.query)
        prep_id = query.get("prep_id", [""])[0]
        if not prep_id:
            raise ValidationError("prep_id 不能为空")
        return 200, service.get_preparation_lineage(prep_id)
    if method == "GET" and parsed.path == "/tasting/recall":
        query = parse_qs(parsed.query)
        batch_id = query.get("batch_id", [""])[0]
        if not batch_id:
            raise ValidationError("batch_id 不能为空")
        return 200, service.get_recall_coverage(batch_id)
    if method == "GET" and parsed.path == "/tasting/inventory-recompute":
        query = parse_qs(parsed.query)
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, service.recompute_inventory(site_id)
    if method == "GET" and parsed.path == "/tasting/containers":
        query = parse_qs(parsed.query)
        container_id = query.get("container_id", [""])[0]
        if not container_id:
            raise ValidationError("container_id 不能为空")
        return 200, service.get_container(container_id)
    if method == "GET" and parsed.path == "/tasting/transfers":
        query = parse_qs(parsed.query)
        transfer_id = query.get("transfer_id", [""])[0]
        if not transfer_id:
            raise ValidationError("transfer_id 不能为空")
        return 200, service.get_transfer(transfer_id)
    return 404, {"error": "route_not_found", "message": "接口不存在"}


def route(foundation: DomainService, tasting: TastingService, method: str, path: str,
          body: dict[str, Any] | None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把请求分派到试饮治理模块，非试饮接口回落到基础层。"""

    headers = headers or {}
    body = body or {}
    actor_id = headers.get("X-Actor-Id", "")
    parsed = urlparse(path)
    if not parsed.path.startswith("/tasting/"):
        return foundation_route(foundation, method, path, body, headers)
    try:
        return _tasting_route(tasting, method, path, body, actor_id)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class TastingHandler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为组合路由调用。"""

    foundation: DomainService
    tasting: TastingService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.foundation, self.tasting, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动包含基础层与试饮治理模块的本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动药膳茶饮试饮批次治理服务")
    parser.add_argument("--database", default="tasting.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    TastingHandler.foundation = DomainService(database)
    TastingHandler.tasting = TastingService(database)
    server = ThreadingHTTPServer((args.host, args.port), TastingHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
