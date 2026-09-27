"""药膳茶饮模块的 HTTP/JSON 路由。

由基础 API 在未命中内置路由时委派到这里；所有写入仍经过基础服务的
操作者权限、请求幂等、事务与哈希审计。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

from ..errors import DomainError, ValidationError
from .service import TeaService


def _query(parsed, name: str, default: str | None = None) -> str | None:
    values = parse_qs(parsed.query).get(name)
    return values[0] if values else default


def route_tea(tea: TeaService, method: str, path: str, body: dict[str, Any],
              parsed, actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """处理茶饮路径；不认识的路径返回 None 交由上层处理。"""

    p = parsed.path
    if not p.startswith("/tea"):
        return None

    def call(action: str, **kwargs):
        receipt = getattr(tea, action)(actor_id=actor_id, **kwargs)
        # 业务响应体落在幂等回执中；取回后附带回执元信息，重放也返回完整内容。
        row = tea.database.connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_id=?",
            (receipt.request_id,),
        ).fetchone()
        payload = json.loads(row["response_json"]) if row else {}
        payload.update({"request_id": receipt.request_id,
                        "resource_type": receipt.resource_type,
                        "resource_id": receipt.resource_id,
                        "replayed": receipt.replayed})
        return 200 if receipt.replayed else 201, payload

    try:
        # ---- 原料批号 ----
        if method == "POST" and p == "/tea/material-batches":
            return call("register_material_batch", **body)
        if method == "GET" and p == "/tea/material-batch":
            batch_id = _query(parsed, "batch_id", "")
            if not batch_id:
                raise ValidationError("batch_id 不能为空")
            return 200, tea.get_material_batch(batch_id).__dict__

        # ---- 配方版本 ----
        if method == "POST" and p == "/tea/formulas":
            return call("create_formula", **body)
        if method == "POST" and p == "/tea/formulas/review":
            return call("review_formula", **body)
        if method == "GET" and p == "/tea/formulas":
            return 200, {"items": [item.__dict__ for item in tea.list_formulas(_query(parsed, "name"))]}
        if method == "GET" and p == "/tea/formula":
            formula_id = _query(parsed, "formula_id", "")
            if not formula_id:
                raise ValidationError("formula_id 不能为空")
            return 200, tea.get_formula(formula_id).__dict__

        # ---- 制备 ----
        if method == "POST" and p == "/tea/preparations":
            return call("prepare_tea", **body)
        if method == "GET" and p == "/tea/preparation":
            preparation_id = _query(parsed, "preparation_id", "")
            if not preparation_id:
                raise ValidationError("preparation_id 不能为空")
            return 200, tea.preparation_lineage(preparation_id)

        # ---- 容器：拆分 / 合并 / 报损 ----
        if method == "POST" and p == "/tea/containers/split":
            return call("split_container", **body)
        if method == "POST" and p == "/tea/containers/merge":
            return call("merge_containers", **body)
        if method == "POST" and p == "/tea/losses":
            return call("report_loss", **body)
        if method == "GET" and p == "/tea/containers":
            site_id = _query(parsed, "site_id")
            include = _query(parsed, "include_off_site", "true") != "false"
            return 200, {"items": [item.__dict__ for item in
                                   tea.list_containers(site_id=site_id, include_off_site=include)]}
        if method == "GET" and p == "/tea/container":
            container_id = _query(parsed, "container_id", "")
            if not container_id:
                raise ValidationError("container_id 不能为空")
            return 200, tea.get_container(container_id).__dict__
        if method == "GET" and p == "/tea/movements":
            container_id = _query(parsed, "container_id", "")
            if not container_id:
                raise ValidationError("container_id 不能为空")
            return 200, {"items": [item.__dict__ for item in tea.list_movements(container_id)]}
        if method == "GET" and p == "/tea/lineage":
            container_id = _query(parsed, "container_id", "")
            if not container_id:
                raise ValidationError("container_id 不能为空")
            return 200, tea.container_lineage(container_id)

        # ---- 库存复算 ----
        if method == "GET" and p == "/tea/inventory":
            return 200, tea.recompute_inventory(_query(parsed, "site_id"))

        # ---- 参与者 / 忌口评估 / 发放 ----
        if method == "POST" and p == "/tea/participants":
            return call("upsert_participant", **body)
        if method == "GET" and p == "/tea/serving-evaluation":
            participant_id = _query(parsed, "participant_id", "")
            container_id = _query(parsed, "container_id", "")
            if not participant_id or not container_id:
                raise ValidationError("participant_id 与 container_id 不能为空")
            raw_amount = _query(parsed, "amount")
            amount = float(raw_amount) if raw_amount is not None else None
            return 200, tea.evaluate_serving(participant_id=participant_id,
                                            container_id=container_id, amount=amount)
        if method == "POST" and p == "/tea/dispenses":
            return call("dispense", **body)
        if method == "GET" and p == "/tea/dispenses":
            return 200, {"items": [item.__dict__ for item in tea.list_dispenses(
                participant_id=_query(parsed, "participant_id"),
                container_id=_query(parsed, "container_id"))]}

        # ---- 冻结与召回 ----
        if method == "POST" and p == "/tea/freezes/batch":
            return call("freeze_batch", **body)
        if method == "POST" and p == "/tea/freezes/container":
            return call("freeze_container", **body)
        if method == "GET" and p == "/tea/recall":
            batch_id = _query(parsed, "batch_id", "")
            if not batch_id:
                raise ValidationError("batch_id 不能为空")
            return 200, tea.recall_coverage(batch_id)

        # ---- 跨摊转移 ----
        if method == "POST" and p == "/tea/transfers/propose":
            return call("propose_transfer", **body)
        if method == "POST" and p == "/tea/transfers/confirm":
            return call("confirm_transfer", **body)
        if method == "POST" and p == "/tea/transfers/reject":
            return call("reject_transfer", **body)
        if method == "GET" and p == "/tea/transfer":
            transfer_id = _query(parsed, "transfer_id", "")
            if not transfer_id:
                raise ValidationError("transfer_id 不能为空")
            return 200, tea.get_transfer(transfer_id).__dict__

        return 404, {"error": "route_not_found", "message": "茶饮接口不存在"}
    except DomainError as exc:
        response: dict[str, Any] = {"error": exc.code, "message": str(exc)}
        evaluation = getattr(exc, "evaluation", None)
        if evaluation is not None:
            response["evaluation"] = evaluation
        return exc.status, response
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
