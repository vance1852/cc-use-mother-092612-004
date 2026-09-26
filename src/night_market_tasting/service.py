"""试饮批次治理：制备谱系、数量守恒台账、忌口判定、冻结召回与跨摊位转移。

该服务复用基础层的操作者、角色、幂等回执与哈希审计链，只追加领域状态。
谱系事实（制备用料、配方版本、领取记录、台账分录）只插入不更新；
数量与状态字段随业务动作推进，任何历史事实都不可覆盖。

所有写动作先查幂等回执再执行业务校验：离线补传的重复消息即使到达时
世界状态已经变化（批次冻结、转移已确认），也返回首次处理时的稳定结果。
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any, Callable

from night_market_foundation.audit import append_event, canonical_json, digest
from night_market_foundation.clock import Clock, SystemClock
from night_market_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from night_market_foundation.models import Actor
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

from .assessment import build_assessment
from .schema import SCHEMA

EPSILON = 1e-6


class TastingService:
    """协调试饮批次的谱系、台账、判定、冻结与转移规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.foundation = DomainService(database, self.clock)
        self.database.connection.executescript(SCHEMA)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _quantity(self, value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} 必须是数字")
        quantity = float(value)
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValidationError(f"{field} 必须是正数")
        rounded = round(quantity, 3)
        if rounded <= 0:
            raise ValidationError(f"{field} 精度超出 0.001 后不能为零")
        return rounded

    def _tags(self, value: Any, field: str) -> list[str]:
        if not isinstance(value, (list, tuple)):
            raise ValidationError(f"{field} 必须是字符串数组")
        tags: list[str] = []
        for item in value:
            text = str(item).strip()
            if not text or len(text) > 40:
                raise ValidationError(f"{field} 含有无效标签")
            if text not in tags:
                tags.append(text)
        return tags

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _scope(self, actor: Actor, site_row) -> None:
        if actor.role != "admin" and actor.organization_id != site_row["organization_id"]:
            raise PermissionDenied("不能操作其他组织的场所")

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        """与基础层共用 request_receipts 表；命中回执时直接返回首次的稳定响应。"""

        request_id = self.foundation._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            response = json.loads(row["response_json"])
            response["replayed"] = True
            return response
        resource_type, resource_id, detail = create()
        response = {"request_id": request_id, "resource_type": resource_type,
                    "resource_id": resource_id, **detail}
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        response["replayed"] = False
        return response

    def _ledger(self, connection, *, site_id: str, entry_type: str, holder_type: str,
                holder_id: str, delta: float, unit: str, actor_id: str,
                ref_type: str | None = None, ref_id: str | None = None,
                note: str | None = None) -> str:
        entry_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO stock_entries(entry_id,site_id,entry_type,holder_type,holder_id,delta,unit,"
            "ref_type,ref_id,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, site_id, entry_type, holder_type, holder_id, round(delta, 3), unit,
             ref_type, ref_id, note, actor_id, self._now()),
        )
        return entry_id

    def _holder_sum(self, connection, holder_type: str, holder_id: str) -> float:
        row = connection.execute(
            "SELECT COALESCE(SUM(delta), 0) AS total FROM stock_entries WHERE holder_type=? AND holder_id=?",
            (holder_type, holder_id),
        ).fetchone()
        return round(row["total"], 3)

    def _load_preparation(self, connection, prep_id: str):
        row = connection.execute("SELECT * FROM preparations WHERE prep_id=?", (prep_id,)).fetchone()
        if row is None:
            raise NotFoundError("制备批次不存在")
        return row

    def _load_container(self, connection, container_id: str):
        row = connection.execute("SELECT * FROM containers WHERE container_id=?", (container_id,)).fetchone()
        if row is None:
            raise NotFoundError("容器不存在")
        return row

    def _load_batch(self, connection, batch_id: str):
        row = connection.execute("SELECT * FROM ingredient_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("原料批次不存在")
        return row

    def _load_recipe_version(self, connection, recipe_version_id: str):
        row = connection.execute(
            "SELECT * FROM recipe_versions WHERE recipe_version_id=?", (recipe_version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("配方版本不存在")
        return row

    def _load_holder(self, connection, holder_type: str, holder_id: str):
        """返回 (持有者行, 所属制备批次行, 持有者数量, 持有者状态, 所在场所)。"""

        if holder_type == "preparation":
            prep = self._load_preparation(connection, holder_id)
            return prep, prep, prep["quantity_remaining"], prep["status"], prep["site_id"]
        if holder_type == "container":
            container = self._load_container(connection, holder_id)
            prep = self._load_preparation(connection, container["prep_id"])
            return container, prep, container["quantity"], container["status"], container["site_id"]
        raise ValidationError("holder_type 必须是 preparation 或 container")

    def _update_holder_quantity(self, connection, holder_type: str, holder_id: str,
                                new_quantity: float) -> None:
        new_quantity = round(new_quantity, 3)
        if holder_type == "preparation":
            status = "depleted" if new_quantity <= EPSILON else "active"
            connection.execute(
                "UPDATE preparations SET quantity_remaining=?, status=? WHERE prep_id=?",
                (new_quantity, status, holder_id),
            )
        else:
            status = "emptied" if new_quantity <= EPSILON else "on_site"
            connection.execute(
                "UPDATE containers SET quantity=?, status=? WHERE container_id=?",
                (new_quantity, status, holder_id),
            )

    def _recipe_version_view(self, row) -> dict[str, Any]:
        return {
            "recipe_version_id": row["recipe_version_id"],
            "recipe_name": row["recipe_name"],
            "version": row["version"],
            "components": json.loads(row["components_json"]),
            "strict_tags": json.loads(row["strict_tags_json"]),
            "caution_tags": json.loads(row["caution_tags_json"]),
            "supersedes_version_id": row["supersedes_version_id"],
            "status": row["status"],
        }

    def _prep_batches(self, connection, prep_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT b.* FROM ingredient_batches b "
            "JOIN preparation_ingredients pi ON b.batch_id=pi.batch_id "
            "WHERE pi.prep_id=? ORDER BY b.batch_id",
            (prep_id,),
        ).fetchall()
        return [{
            "batch_id": row["batch_id"],
            "ingredient_name": row["ingredient_name"],
            "lot_number": row["lot_number"],
            "strict_tags": json.loads(row["strict_tags_json"]),
            "caution_tags": json.loads(row["caution_tags_json"]),
            "status": row["status"],
        } for row in rows]

    def _participant_restrictions(self, connection, participant_id: str) -> tuple[list[str], bool]:
        row = connection.execute(
            "SELECT * FROM participant_declarations WHERE participant_id=?", (participant_id,)
        ).fetchone()
        if row is None:
            return [], False
        return json.loads(row["restrictions_json"]), True

    # ------------------------------------------------------------------
    # 原料批次与配方版本
    # ------------------------------------------------------------------

    def register_ingredient_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                                  site_id: str, ingredient_name: str, lot_number: str,
                                  quantity_received: float, unit: str,
                                  strict_tags: list[str] | None = None,
                                  caution_tags: list[str] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "site_id": site_id,
                   "ingredient_name": ingredient_name, "lot_number": lot_number,
                   "quantity_received": quantity_received, "unit": unit,
                   "strict_tags": strict_tags or [], "caution_tags": caution_tags or []}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                site = self._site(connection, site_id)
                self._scope(actor, site)
                batch_key = self.foundation._identifier(batch_id, "batch_id")
                name = self.foundation._text(ingredient_name, "ingredient_name", 80)
                lot = self.foundation._text(lot_number, "lot_number", 80)
                batch_unit = self.foundation._text(unit, "unit", 20)
                received = self._quantity(quantity_received, "quantity_received")
                strict = self._tags(payload["strict_tags"], "strict_tags")
                caution = self._tags(payload["caution_tags"], "caution_tags")
                try:
                    connection.execute(
                        "INSERT INTO ingredient_batches(batch_id,site_id,ingredient_name,lot_number,"
                        "quantity_received,unit,strict_tags_json,caution_tags_json,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
                        (batch_key, site_id, name, lot, received, batch_unit,
                         canonical_json(strict), canonical_json(caution), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("原料批次编号或同原料批号已存在") from exc
                append_event(connection, actor_id=actor_id, action="ingredient_batch.registered",
                             resource_type="ingredient_batch", resource_id=batch_key,
                             detail={"site_id": site_id, "ingredient_name": name,
                                     "lot_number": lot, "quantity_received": received,
                                     "unit": batch_unit, "strict_tags": strict, "caution_tags": caution},
                             occurred_at=self._now())
                return "ingredient_batch", batch_key, {"batch_id": batch_key, "status": "active"}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_ingredient_batch", payload=payload, create=create)

    def create_recipe_version(self, *, request_id: str, actor_id: str, recipe_version_id: str,
                              site_id: str, recipe_name: str, version: int,
                              components: list[dict[str, Any]],
                              strict_tags: list[str] | None = None,
                              caution_tags: list[str] | None = None,
                              supersedes_version_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "recipe_version_id": recipe_version_id, "site_id": site_id,
                   "recipe_name": recipe_name, "version": version, "components": components,
                   "strict_tags": strict_tags or [], "caution_tags": caution_tags or [],
                   "supersedes_version_id": supersedes_version_id}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                site = self._site(connection, site_id)
                self._scope(actor, site)
                version_key = self.foundation._identifier(recipe_version_id, "recipe_version_id")
                name = self.foundation._text(recipe_name, "recipe_name", 80)
                if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                    raise ValidationError("version 必须是正整数")
                if not isinstance(components, list) or not components:
                    raise ValidationError("components 必须是非空数组")
                normalized: list[dict[str, Any]] = []
                seen: set[str] = set()
                for component in components:
                    if not isinstance(component, dict):
                        raise ValidationError("components 元素必须是对象")
                    ingredient = self.foundation._text(str(component.get("ingredient", "")),
                                                       "components.ingredient", 80)
                    if ingredient in seen:
                        raise ValidationError("components 含有重复原料")
                    seen.add(ingredient)
                    normalized.append({
                        "ingredient": ingredient,
                        "amount": self._quantity(component.get("amount"), "components.amount"),
                        "unit": self.foundation._text(str(component.get("unit", "")), "components.unit", 20),
                    })
                strict = self._tags(payload["strict_tags"], "strict_tags")
                caution = self._tags(payload["caution_tags"], "caution_tags")
                if supersedes_version_id is not None:
                    previous = self._load_recipe_version(connection, supersedes_version_id)
                    if previous["site_id"] != site_id or previous["recipe_name"] != name:
                        raise ValidationError("替代配方必须属于同一场所的同一配方")
                try:
                    connection.execute(
                        "INSERT INTO recipe_versions(recipe_version_id,site_id,recipe_name,version,"
                        "components_json,strict_tags_json,caution_tags_json,supersedes_version_id,status,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,'pending_review',?,?)",
                        (version_key, site_id, name, version, canonical_json(normalized),
                         canonical_json(strict), canonical_json(caution), supersedes_version_id,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("配方版本编号或版本号已存在") from exc
                append_event(connection, actor_id=actor_id, action="recipe_version.created",
                             resource_type="recipe_version", resource_id=version_key,
                             detail={"site_id": site_id, "recipe_name": name, "version": version,
                                     "components": normalized, "strict_tags": strict,
                                     "caution_tags": caution,
                                     "supersedes_version_id": supersedes_version_id},
                             occurred_at=self._now())
                return "recipe_version", version_key, {
                    "recipe_version_id": version_key, "status": "pending_review"}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_recipe_version", payload=payload, create=create)

    def confirm_recipe_risk(self, *, request_id: str, actor_id: str,
                            recipe_version_id: str, risk_note: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "recipe_version_id": recipe_version_id, "risk_note": risk_note}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "reviewer")
                row = self._load_recipe_version(connection, recipe_version_id)
                site = self._site(connection, row["site_id"])
                self._scope(actor, site)
                if row["status"] != "pending_review":
                    raise ConflictError("配方版本已完成审核，不能重复确认")
                if row["created_by"] == actor.actor_id:
                    raise PermissionDenied("配方创建者不能审核自己的版本")
                note = self.foundation._text(risk_note, "risk_note")
                previous = None
                if row["supersedes_version_id"]:
                    previous = self._load_recipe_version(connection, row["supersedes_version_id"])
                old_strict = set(json.loads(previous["strict_tags_json"])) if previous else set()
                old_caution = set(json.loads(previous["caution_tags_json"])) if previous else set()
                new_strict = set(json.loads(row["strict_tags_json"]))
                new_caution = set(json.loads(row["caution_tags_json"]))
                risk_diff = {
                    "strict_added": sorted(new_strict - old_strict),
                    "strict_removed": sorted(old_strict - new_strict),
                    "caution_added": sorted(new_caution - old_caution),
                    "caution_removed": sorted(old_caution - new_caution),
                }
                connection.execute(
                    "UPDATE recipe_versions SET status='active', risk_note=?, reviewed_by=?, reviewed_at=? "
                    "WHERE recipe_version_id=?",
                    (note, actor_id, self._now(), recipe_version_id),
                )
                append_event(connection, actor_id=actor_id, action="recipe_version.risk_confirmed",
                             resource_type="recipe_version", resource_id=recipe_version_id,
                             detail={"risk_note": note, "risk_diff": risk_diff,
                                     "supersedes_version_id": row["supersedes_version_id"]},
                             occurred_at=self._now())
                return "recipe_version", recipe_version_id, {
                    "recipe_version_id": recipe_version_id, "status": "active", "risk_diff": risk_diff}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_recipe_risk", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 制备与数量守恒台账
    # ------------------------------------------------------------------

    def brew_preparation(self, *, request_id: str, actor_id: str, prep_id: str, site_id: str,
                         recipe_version_id: str, quantity_brewed: float, unit: str,
                         ingredients: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "prep_id": prep_id, "site_id": site_id,
                   "recipe_version_id": recipe_version_id, "quantity_brewed": quantity_brewed,
                   "unit": unit, "ingredients": ingredients}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                site = self._site(connection, site_id)
                self._scope(actor, site)
                prep_key = self.foundation._identifier(prep_id, "prep_id")
                brew_unit = self.foundation._text(unit, "unit", 20)
                brewed = self._quantity(quantity_brewed, "quantity_brewed")
                version = self._load_recipe_version(connection, recipe_version_id)
                if version["site_id"] != site_id:
                    raise ValidationError("配方版本不属于该场所")
                if version["status"] != "active":
                    raise ValidationError("配方版本未通过风险审核，不能用于制备")
                components = json.loads(version["components_json"])
                if not isinstance(ingredients, list) or not ingredients:
                    raise ValidationError("ingredients 必须是非空数组")
                usages: list[dict[str, Any]] = []
                seen_batches: set[str] = set()
                for item in ingredients:
                    if not isinstance(item, dict):
                        raise ValidationError("ingredients 元素必须是对象")
                    batch_id = self.foundation._identifier(str(item.get("batch_id", "")),
                                                           "ingredients.batch_id")
                    if batch_id in seen_batches:
                        raise ValidationError("同一原料批次重复登记")
                    seen_batches.add(batch_id)
                    batch = self._load_batch(connection, batch_id)
                    if batch["site_id"] != site_id:
                        raise ValidationError("原料批次不属于该场所")
                    if batch["status"] != "active":
                        raise ValidationError(
                            f"原料 {batch['ingredient_name']}（批号 {batch['lot_number']}）已冻结，不能用于制备")
                    quantity_used = self._quantity(item.get("quantity_used"), "ingredients.quantity_used")
                    used_row = connection.execute(
                        "SELECT COALESCE(SUM(quantity_used), 0) AS used FROM preparation_ingredients "
                        "WHERE batch_id=?",
                        (batch_id,),
                    ).fetchone()
                    if used_row["used"] + quantity_used > batch["quantity_received"] + EPSILON:
                        raise ValidationError(
                            f"原料 {batch['ingredient_name']}（批号 {batch['lot_number']}）库存不足")
                    usages.append({"batch": batch, "quantity_used": quantity_used})
                needed = {component["ingredient"] for component in components}
                provided = {usage["batch"]["ingredient_name"] for usage in usages}
                if needed != provided:
                    raise ValidationError("制备用料必须与配方成分一一对应")
                try:
                    connection.execute(
                        "INSERT INTO preparations(prep_id,site_id,recipe_version_id,quantity_brewed,"
                        "quantity_remaining,unit,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,'active',?,?)",
                        (prep_key, site_id, recipe_version_id, brewed, brewed,
                         brew_unit, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("制备批次编号已存在") from exc
                lineage = []
                for usage in usages:
                    batch = usage["batch"]
                    connection.execute(
                        "INSERT INTO preparation_ingredients(prep_id,batch_id,ingredient_name,quantity_used,unit) "
                        "VALUES(?,?,?,?,?)",
                        (prep_key, batch["batch_id"], batch["ingredient_name"],
                         usage["quantity_used"], batch["unit"]),
                    )
                    lineage.append({"batch_id": batch["batch_id"],
                                    "ingredient_name": batch["ingredient_name"],
                                    "lot_number": batch["lot_number"],
                                    "quantity_used": usage["quantity_used"]})
                self._ledger(connection, site_id=site_id, entry_type="brew",
                             holder_type="preparation", holder_id=prep_key,
                             delta=brewed, unit=brew_unit, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="preparation.brewed",
                             resource_type="preparation", resource_id=prep_key,
                             detail={"site_id": site_id, "recipe_version_id": recipe_version_id,
                                     "quantity_brewed": brewed, "unit": brew_unit,
                                     "ingredients": lineage},
                             occurred_at=self._now())
                return "preparation", prep_key, {
                    "prep_id": prep_key, "recipe_version_id": recipe_version_id,
                    "quantity_brewed": brewed, "quantity_remaining": brewed,
                    "unit": brew_unit, "ingredients": lineage}

            return self._idempotent(connection, request_id=request_id,
                                    action="brew_preparation", payload=payload, create=create)

    def fill_container(self, *, request_id: str, actor_id: str, container_id: str, prep_id: str,
                       label: str, quantity: float, unit: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "container_id": container_id, "prep_id": prep_id,
                   "label": label, "quantity": quantity, "unit": unit}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                prep = self._load_preparation(connection, prep_id)
                site = self._site(connection, prep["site_id"])
                self._scope(actor, site)
                if prep["status"] != "active":
                    raise ConflictError("制备批次当前状态不能分装")
                container_key = self.foundation._identifier(container_id, "container_id")
                fill_label = self.foundation._text(label, "label", 80)
                fill_quantity = self._quantity(quantity, "quantity")
                fill_unit = self.foundation._text(unit, "unit", 20)
                if fill_unit != prep["unit"]:
                    raise ValidationError("分装单位必须与制备批次一致")
                if fill_quantity > prep["quantity_remaining"] + EPSILON:
                    raise ValidationError("分装量超过制备批次剩余量")
                try:
                    connection.execute(
                        "INSERT INTO containers(container_id,site_id,prep_id,label,quantity,unit,status,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,'on_site',?,?)",
                        (container_key, prep["site_id"], prep_id, fill_label, fill_quantity,
                         prep["unit"], actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("容器编号已存在") from exc
                remaining = round(prep["quantity_remaining"] - fill_quantity, 3)
                self._update_holder_quantity(connection, "preparation", prep_id, remaining)
                self._ledger(connection, site_id=prep["site_id"], entry_type="fill",
                             holder_type="preparation", holder_id=prep_id, delta=-fill_quantity,
                             unit=prep["unit"], actor_id=actor_id,
                             ref_type="container", ref_id=container_key)
                self._ledger(connection, site_id=prep["site_id"], entry_type="fill",
                             holder_type="container", holder_id=container_key, delta=fill_quantity,
                             unit=prep["unit"], actor_id=actor_id,
                             ref_type="preparation", ref_id=prep_id)
                append_event(connection, actor_id=actor_id, action="container.filled",
                             resource_type="container", resource_id=container_key,
                             detail={"prep_id": prep_id, "label": fill_label, "quantity": fill_quantity,
                                     "unit": prep["unit"], "prep_remaining": remaining},
                             occurred_at=self._now())
                return "container", container_key, {
                    "container_id": container_key, "prep_id": prep_id,
                    "quantity": fill_quantity, "prep_remaining": remaining}

            return self._idempotent(connection, request_id=request_id,
                                    action="fill_container", payload=payload, create=create)

    def split_container(self, *, request_id: str, actor_id: str, source_container_id: str,
                        new_container_id: str, label: str, quantity: float) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "source_container_id": source_container_id,
                   "new_container_id": new_container_id, "label": label, "quantity": quantity}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                source = self._load_container(connection, source_container_id)
                site = self._site(connection, source["site_id"])
                self._scope(actor, site)
                if source["status"] != "on_site":
                    raise ConflictError("容器当前状态不能拆分")
                new_key = self.foundation._identifier(new_container_id, "new_container_id")
                split_label = self.foundation._text(label, "label", 80)
                split_quantity = self._quantity(quantity, "quantity")
                if split_quantity > source["quantity"] + EPSILON:
                    raise ValidationError("拆分量超过容器当前存量")
                try:
                    connection.execute(
                        "INSERT INTO containers(container_id,site_id,prep_id,label,quantity,unit,status,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,'on_site',?,?)",
                        (new_key, source["site_id"], source["prep_id"], split_label, split_quantity,
                         source["unit"], actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("容器编号已存在") from exc
                remaining = round(source["quantity"] - split_quantity, 3)
                self._update_holder_quantity(connection, "container", source_container_id, remaining)
                self._ledger(connection, site_id=source["site_id"], entry_type="split",
                             holder_type="container", holder_id=source_container_id,
                             delta=-split_quantity, unit=source["unit"], actor_id=actor_id,
                             ref_type="container", ref_id=new_key)
                self._ledger(connection, site_id=source["site_id"], entry_type="split",
                             holder_type="container", holder_id=new_key, delta=split_quantity,
                             unit=source["unit"], actor_id=actor_id,
                             ref_type="container", ref_id=source_container_id)
                append_event(connection, actor_id=actor_id, action="container.split",
                             resource_type="container", resource_id=source_container_id,
                             detail={"new_container_id": new_key, "quantity": split_quantity,
                                     "unit": source["unit"], "source_remaining": remaining},
                             occurred_at=self._now())
                return "container", new_key, {
                    "container_id": new_key, "source_container_id": source_container_id,
                    "quantity": split_quantity, "source_remaining": remaining}

            return self._idempotent(connection, request_id=request_id,
                                    action="split_container", payload=payload, create=create)

    def merge_containers(self, *, request_id: str, actor_id: str, source_container_id: str,
                         target_container_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "source_container_id": source_container_id,
                   "target_container_id": target_container_id}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                if source_container_id == target_container_id:
                    raise ValidationError("源容器与目标容器不能相同")
                source = self._load_container(connection, source_container_id)
                target = self._load_container(connection, target_container_id)
                site = self._site(connection, source["site_id"])
                self._scope(actor, site)
                if source["status"] != "on_site" or target["status"] != "on_site":
                    raise ConflictError("只有仍在场的容器可以合并")
                if source["prep_id"] != target["prep_id"]:
                    raise ValidationError("只有同一制备批次的容器可以合并")
                if source["site_id"] != target["site_id"]:
                    raise ValidationError("只有同一场所的容器可以合并")
                quantity = source["quantity"]
                if quantity <= EPSILON:
                    raise ValidationError("源容器已为空")
                target_quantity = round(target["quantity"] + quantity, 3)
                self._update_holder_quantity(connection, "container", source_container_id, 0.0)
                self._update_holder_quantity(connection, "container", target_container_id, target_quantity)
                self._ledger(connection, site_id=source["site_id"], entry_type="merge",
                             holder_type="container", holder_id=source_container_id, delta=-quantity,
                             unit=source["unit"], actor_id=actor_id,
                             ref_type="container", ref_id=target_container_id)
                self._ledger(connection, site_id=source["site_id"], entry_type="merge",
                             holder_type="container", holder_id=target_container_id, delta=quantity,
                             unit=source["unit"], actor_id=actor_id,
                             ref_type="container", ref_id=source_container_id)
                append_event(connection, actor_id=actor_id, action="containers.merged",
                             resource_type="container", resource_id=target_container_id,
                             detail={"source_container_id": source_container_id, "quantity": quantity,
                                     "unit": source["unit"], "target_quantity": target_quantity},
                             occurred_at=self._now())
                return "container", target_container_id, {
                    "container_id": target_container_id, "source_container_id": source_container_id,
                    "merged_quantity": quantity, "target_quantity": target_quantity}

            return self._idempotent(connection, request_id=request_id,
                                    action="merge_containers", payload=payload, create=create)

    def record_loss(self, *, request_id: str, actor_id: str, holder_type: str, holder_id: str,
                    quantity: float, unit: str, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "holder_type": holder_type, "holder_id": holder_id,
                   "quantity": quantity, "unit": unit, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                holder, _, current, status, site_id = self._load_holder(connection, holder_type, holder_id)
                site = self._site(connection, site_id)
                self._scope(actor, site)
                if status not in ("active", "on_site"):
                    raise ConflictError("当前状态不能登记报损")
                loss_quantity = self._quantity(quantity, "quantity")
                loss_unit = self.foundation._text(unit, "unit", 20)
                loss_reason = self.foundation._text(reason, "reason")
                holder_unit = holder["unit"]
                if loss_unit != holder_unit:
                    raise ValidationError("报损单位必须与持有者一致")
                if loss_quantity > current + EPSILON:
                    raise ValidationError("报损量超过当前存量")
                remaining = round(current - loss_quantity, 3)
                self._update_holder_quantity(connection, holder_type, holder_id, remaining)
                entry_id = self._ledger(connection, site_id=site_id, entry_type="loss",
                                        holder_type=holder_type, holder_id=holder_id,
                                        delta=-loss_quantity, unit=holder_unit,
                                        actor_id=actor_id, note=loss_reason)
                append_event(connection, actor_id=actor_id, action="stock.loss_recorded",
                             resource_type=holder_type, resource_id=holder_id,
                             detail={"quantity": loss_quantity, "unit": holder_unit,
                                     "reason": loss_reason, "remaining": remaining},
                             occurred_at=self._now())
                return "stock_entry", entry_id, {
                    "entry_id": entry_id, "holder_type": holder_type, "holder_id": holder_id,
                    "quantity": loss_quantity, "remaining": remaining}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_loss", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 参与者忌口与发放
    # ------------------------------------------------------------------

    def declare_restrictions(self, *, request_id: str, actor_id: str, participant_id: str,
                             restrictions: list[str], note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "participant_id": participant_id,
                   "restrictions": restrictions, "note": note}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                participant_key = self.foundation._identifier(participant_id, "participant_id")
                tags = self._tags(restrictions, "restrictions")
                text = str(note).strip()
                if len(text) > 200:
                    raise ValidationError("note 不能超过 200 个字符")
                existing = connection.execute(
                    "SELECT participant_id FROM participant_declarations WHERE participant_id=?",
                    (participant_key,),
                ).fetchone()
                if existing:
                    connection.execute(
                        "UPDATE participant_declarations SET restrictions_json=?, note=?, updated_by=?, "
                        "updated_at=? WHERE participant_id=?",
                        (canonical_json(tags), text, actor_id, self._now(), participant_key),
                    )
                else:
                    connection.execute(
                        "INSERT INTO participant_declarations(participant_id,restrictions_json,note,"
                        "declared_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (participant_key, canonical_json(tags), text, actor_id, actor_id,
                         self._now(), self._now()),
                    )
                append_event(connection, actor_id=actor_id, action="participant.declaration_recorded",
                             resource_type="participant_declaration", resource_id=participant_key,
                             detail={"restrictions": tags, "note": text},
                             occurred_at=self._now())
                return "participant_declaration", participant_key, {
                    "participant_id": participant_key, "restrictions": tags}

            return self._idempotent(connection, request_id=request_id,
                                    action="declare_restrictions", payload=payload, create=create)

    def assess(self, *, holder_type: str, holder_id: str, participant_id: str) -> dict[str, Any]:
        """只读判定：结合忌口信息给出可提供、需人工确认或不得提供的解释。"""

        connection = self.database.connection
        holder, prep, _, holder_status, _ = self._load_holder(connection, holder_type, holder_id)
        version = self._recipe_version_view(self._load_recipe_version(connection, prep["recipe_version_id"]))
        batches = self._prep_batches(connection, prep["prep_id"])
        participant_key = str(participant_id).strip()
        restrictions, found = self._participant_restrictions(connection, participant_key)
        return build_assessment(
            holder_type=holder_type, holder_id=holder_id, holder_status=holder_status,
            preparation_status=prep["status"], recipe_version=version, batches=batches,
            participant_id=participant_key, restrictions=restrictions, declaration_found=found)

    def record_claim(self, *, request_id: str, actor_id: str, claim_id: str, holder_type: str,
                     holder_id: str, participant_id: str, quantity: float, unit: str,
                     manual_confirmed: bool = False) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "claim_id": claim_id, "holder_type": holder_type,
                   "holder_id": holder_id, "participant_id": participant_id,
                   "quantity": quantity, "unit": unit, "manual_confirmed": bool(manual_confirmed)}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                holder, prep, current, status, site_id = self._load_holder(connection, holder_type, holder_id)
                site = self._site(connection, site_id)
                self._scope(actor, site)
                claim_key = self.foundation._identifier(claim_id, "claim_id")
                participant_key = self.foundation._identifier(participant_id, "participant_id")
                claim_quantity = self._quantity(quantity, "quantity")
                claim_unit = self.foundation._text(unit, "unit", 20)
                holder_unit = holder["unit"]
                if claim_unit != holder_unit:
                    raise ValidationError("发放单位必须与持有者一致")
                if claim_quantity > current + EPSILON:
                    raise ValidationError("发放量超过当前存量")
                version = self._recipe_version_view(
                    self._load_recipe_version(connection, prep["recipe_version_id"]))
                batches = self._prep_batches(connection, prep["prep_id"])
                restrictions, found = self._participant_restrictions(connection, participant_key)
                assessment = build_assessment(
                    holder_type=holder_type, holder_id=holder_id, holder_status=status,
                    preparation_status=prep["status"], recipe_version=version, batches=batches,
                    participant_id=participant_key, restrictions=restrictions, declaration_found=found)
                confirmed = bool(manual_confirmed)
                decision = assessment["decision"]
                if decision == "deny":
                    decision_id = uuid.uuid4().hex
                    return "claim_decision", decision_id, {
                        "decision_id": decision_id, "decision": "deny", "claim_id": None,
                        "explanation": assessment}
                if decision == "review" and not confirmed:
                    decision_id = uuid.uuid4().hex
                    return "claim_decision", decision_id, {
                        "decision_id": decision_id, "decision": "review", "claim_id": None,
                        "requires_manual_confirmation": True, "explanation": assessment}
                persisted = 1 if decision == "review" and confirmed else 0
                try:
                    connection.execute(
                        "INSERT INTO claims(claim_id,site_id,holder_type,holder_id,participant_id,quantity,"
                        "unit,recipe_version_id,decision,explanation_json,manual_confirmed,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (claim_key, site_id, holder_type, holder_id, participant_key, claim_quantity,
                         holder_unit, version["recipe_version_id"], decision, canonical_json(assessment),
                         persisted, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("领取编号已存在") from exc
                remaining = round(current - claim_quantity, 3)
                self._update_holder_quantity(connection, holder_type, holder_id, remaining)
                self._ledger(connection, site_id=site_id, entry_type="claim",
                             holder_type=holder_type, holder_id=holder_id, delta=-claim_quantity,
                             unit=holder_unit, actor_id=actor_id, ref_type="claim", ref_id=claim_key)
                append_event(connection, actor_id=actor_id, action="claim.recorded",
                             resource_type="claim", resource_id=claim_key,
                             detail={"holder_type": holder_type, "holder_id": holder_id,
                                     "participant_id": participant_key, "quantity": claim_quantity,
                                     "unit": holder_unit, "decision": decision,
                                     "recipe_version_id": version["recipe_version_id"],
                                     "manual_confirmed": persisted},
                             occurred_at=self._now())
                return "claim", claim_key, {
                    "claim_id": claim_key, "decision": decision, "explanation": assessment,
                    "holder_remaining": remaining}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_claim", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 跨摊位转移
    # ------------------------------------------------------------------

    def initiate_transfer(self, *, request_id: str, actor_id: str, transfer_id: str,
                          container_id: str, to_site_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "transfer_id": transfer_id, "container_id": container_id,
                   "to_site_id": to_site_id}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                container = self._load_container(connection, container_id)
                from_site = self._site(connection, container["site_id"])
                self._scope(actor, from_site)
                if container["status"] != "on_site":
                    raise ConflictError("容器当前状态不能发起转移")
                to_site = self._site(connection, to_site_id)
                if to_site["site_id"] == container["site_id"]:
                    raise ValidationError("目标场所不能与当前场所相同")
                transfer_key = self.foundation._identifier(transfer_id, "transfer_id")
                try:
                    connection.execute(
                        "INSERT INTO transfers(transfer_id,container_id,from_site_id,to_site_id,quantity,"
                        "unit,status,initiated_by,created_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                        (transfer_key, container_id, container["site_id"], to_site["site_id"],
                         container["quantity"], container["unit"], actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("转移编号已存在") from exc
                connection.execute(
                    "UPDATE containers SET status='in_transfer' WHERE container_id=?", (container_id,))
                append_event(connection, actor_id=actor_id, action="transfer.initiated",
                             resource_type="transfer", resource_id=transfer_key,
                             detail={"container_id": container_id, "from_site_id": container["site_id"],
                                     "to_site_id": to_site["site_id"],
                                     "quantity": container["quantity"], "unit": container["unit"]},
                             occurred_at=self._now())
                return "transfer", transfer_key, {
                    "transfer_id": transfer_key, "status": "pending", "container_id": container_id,
                    "from_site_id": container["site_id"], "to_site_id": to_site["site_id"],
                    "quantity": container["quantity"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="initiate_transfer", payload=payload, create=create)

    def _load_transfer(self, connection, transfer_id: str):
        row = connection.execute("SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if row is None:
            raise NotFoundError("转移单不存在")
        return row

    def _resolve_scope(self, connection, actor: Actor, transfer) -> None:
        to_site = self._site(connection, transfer["to_site_id"])
        self._scope(actor, to_site)
        if actor.actor_id == transfer["initiated_by"]:
            raise PermissionDenied("跨摊位转移必须由另一方操作者处理")

    def confirm_transfer(self, *, request_id: str, actor_id: str, transfer_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "transfer_id": transfer_id}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                transfer = self._load_transfer(connection, transfer_id)
                self._resolve_scope(connection, actor, transfer)
                if transfer["status"] != "pending":
                    raise ConflictError("转移单已处理，结果保持稳定")
                connection.execute(
                    "UPDATE transfers SET status='confirmed', resolved_by=?, resolved_at=? WHERE transfer_id=?",
                    (actor_id, self._now(), transfer_id),
                )
                connection.execute(
                    "UPDATE containers SET site_id=?, status='on_site' WHERE container_id=?",
                    (transfer["to_site_id"], transfer["container_id"]),
                )
                self._ledger(connection, site_id=transfer["from_site_id"], entry_type="transfer_out",
                             holder_type="container", holder_id=transfer["container_id"],
                             delta=-transfer["quantity"], unit=transfer["unit"], actor_id=actor_id,
                             ref_type="transfer", ref_id=transfer_id)
                self._ledger(connection, site_id=transfer["to_site_id"], entry_type="transfer_in",
                             holder_type="container", holder_id=transfer["container_id"],
                             delta=transfer["quantity"], unit=transfer["unit"], actor_id=actor_id,
                             ref_type="transfer", ref_id=transfer_id)
                append_event(connection, actor_id=actor_id, action="transfer.confirmed",
                             resource_type="transfer", resource_id=transfer_id,
                             detail={"container_id": transfer["container_id"],
                                     "from_site_id": transfer["from_site_id"],
                                     "to_site_id": transfer["to_site_id"],
                                     "quantity": transfer["quantity"], "unit": transfer["unit"]},
                             occurred_at=self._now())
                return "transfer", transfer_id, {
                    "transfer_id": transfer_id, "status": "confirmed",
                    "container_id": transfer["container_id"], "site_id": transfer["to_site_id"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_transfer", payload=payload, create=create)

    def reject_transfer(self, *, request_id: str, actor_id: str, transfer_id: str,
                        reason: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "transfer_id": transfer_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "operator")
                transfer = self._load_transfer(connection, transfer_id)
                self._resolve_scope(connection, actor, transfer)
                if transfer["status"] != "pending":
                    raise ConflictError("转移单已处理，结果保持稳定")
                text = str(reason).strip()
                if len(text) > 200:
                    raise ValidationError("reason 不能超过 200 个字符")
                connection.execute(
                    "UPDATE transfers SET status='rejected', resolved_by=?, resolved_at=? WHERE transfer_id=?",
                    (actor_id, self._now(), transfer_id),
                )
                connection.execute(
                    "UPDATE containers SET status='on_site' WHERE container_id=?",
                    (transfer["container_id"],),
                )
                append_event(connection, actor_id=actor_id, action="transfer.rejected",
                             resource_type="transfer", resource_id=transfer_id,
                             detail={"container_id": transfer["container_id"],
                                     "from_site_id": transfer["from_site_id"],
                                     "to_site_id": transfer["to_site_id"], "reason": text},
                             occurred_at=self._now())
                return "transfer", transfer_id, {
                    "transfer_id": transfer_id, "status": "rejected",
                    "container_id": transfer["container_id"], "site_id": transfer["from_site_id"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="reject_transfer", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 原料冻结与召回
    # ------------------------------------------------------------------

    def freeze_ingredient_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                                reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.foundation._actor(connection, actor_id)
                self.foundation._require(actor, "admin", "reviewer")
                batch = self._load_batch(connection, batch_id)
                site = self._site(connection, batch["site_id"])
                self._scope(actor, site)
                if batch["status"] != "active":
                    raise ConflictError("原料批次已冻结，结果保持稳定")
                freeze_reason = self.foundation._text(reason, "reason")
                connection.execute(
                    "UPDATE ingredient_batches SET status='frozen', freeze_reason=?, frozen_by=?, "
                    "frozen_at=? WHERE batch_id=?",
                    (freeze_reason, actor_id, self._now(), batch_id),
                )
                prep_rows = connection.execute(
                    "SELECT p.prep_id, p.status FROM preparations p "
                    "JOIN preparation_ingredients pi ON p.prep_id=pi.prep_id WHERE pi.batch_id=?",
                    (batch_id,),
                ).fetchall()
                prep_ids = [row["prep_id"] for row in prep_rows]
                frozen_preparations = [row["prep_id"] for row in prep_rows if row["status"] == "active"]
                for prep_id in frozen_preparations:
                    connection.execute(
                        "UPDATE preparations SET status='frozen' WHERE prep_id=?", (prep_id,))
                frozen_containers: list[str] = []
                cancelled_transfers: list[str] = []
                if prep_ids:
                    marks = ",".join("?" for _ in prep_ids)
                    container_rows = connection.execute(
                        f"SELECT container_id, status FROM containers WHERE prep_id IN ({marks}) "
                        "AND status IN ('on_site', 'in_transfer')",
                        prep_ids,
                    ).fetchall()
                    frozen_containers = [row["container_id"] for row in container_rows]
                    for container_id in frozen_containers:
                        connection.execute(
                            "UPDATE containers SET status='frozen' WHERE container_id=?", (container_id,))
                    transfer_rows = connection.execute(
                        f"SELECT t.transfer_id FROM transfers t "
                        f"JOIN containers c ON t.container_id=c.container_id "
                        f"WHERE c.prep_id IN ({marks}) AND t.status='pending'",
                        prep_ids,
                    ).fetchall()
                    cancelled_transfers = [row["transfer_id"] for row in transfer_rows]
                    for transfer_id in cancelled_transfers:
                        connection.execute(
                            "UPDATE transfers SET status='cancelled', resolved_by=?, resolved_at=? "
                            "WHERE transfer_id=?",
                            (actor_id, self._now(), transfer_id),
                        )
                append_event(connection, actor_id=actor_id, action="ingredient_batch.frozen",
                             resource_type="ingredient_batch", resource_id=batch_id,
                             detail={"reason": freeze_reason,
                                     "frozen_preparations": frozen_preparations,
                                     "frozen_containers": frozen_containers,
                                     "cancelled_transfers": cancelled_transfers},
                             occurred_at=self._now())
                return "ingredient_batch", batch_id, {
                    "batch_id": batch_id, "status": "frozen",
                    "frozen_preparations": frozen_preparations,
                    "frozen_containers": frozen_containers,
                    "cancelled_transfers": cancelled_transfers}

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_ingredient_batch", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 只读查询：谱系、召回、复算
    # ------------------------------------------------------------------

    def _container_view(self, row) -> dict[str, Any]:
        return {"container_id": row["container_id"], "site_id": row["site_id"],
                "prep_id": row["prep_id"], "label": row["label"], "quantity": row["quantity"],
                "unit": row["unit"], "status": row["status"]}

    def _claim_view(self, row) -> dict[str, Any]:
        return {"claim_id": row["claim_id"], "site_id": row["site_id"],
                "holder_type": row["holder_type"], "holder_id": row["holder_id"],
                "participant_id": row["participant_id"], "quantity": row["quantity"],
                "unit": row["unit"], "recipe_version_id": row["recipe_version_id"],
                "decision": row["decision"], "manual_confirmed": bool(row["manual_confirmed"]),
                "created_at": row["created_at"]}

    def _claims_for_holders(self, connection, prep_id: str,
                            container_ids: list[str]) -> list[dict[str, Any]]:
        claims = []
        rows = connection.execute(
            "SELECT * FROM claims WHERE holder_type='preparation' AND holder_id=? "
            "ORDER BY created_at, claim_id",
            (prep_id,),
        ).fetchall()
        claims.extend(self._claim_view(row) for row in rows)
        if container_ids:
            marks = ",".join("?" for _ in container_ids)
            rows = connection.execute(
                f"SELECT * FROM claims WHERE holder_type='container' AND holder_id IN ({marks}) "
                "ORDER BY created_at, claim_id",
                container_ids,
            ).fetchall()
            claims.extend(self._claim_view(row) for row in rows)
        return claims

    def _reconciliation(self, connection, prep_row) -> dict[str, Any]:
        """由台账分录重算一壶茶的收支配平：制备量 = 剩余 + 在容器 + 已发放 + 已报损。"""

        prep_id = prep_row["prep_id"]
        containers = connection.execute(
            "SELECT * FROM containers WHERE prep_id=? ORDER BY created_at, container_id", (prep_id,)
        ).fetchall()
        holder_ids = [prep_id] + [row["container_id"] for row in containers]
        holder_types = ["preparation"] + ["container"] * len(containers)
        distributed = 0.0
        lost = 0.0
        for holder_type, holder_id in zip(holder_types, holder_ids):
            rows = connection.execute(
                "SELECT entry_type, COALESCE(SUM(delta), 0) AS total FROM stock_entries "
                "WHERE holder_type=? AND holder_id=? AND entry_type IN ('claim', 'loss') GROUP BY entry_type",
                (holder_type, holder_id),
            ).fetchall()
            for item in rows:
                if item["entry_type"] == "claim":
                    distributed += -item["total"]
                else:
                    lost += -item["total"]
        remaining_computed = self._holder_sum(connection, "preparation", prep_id)
        in_containers_computed = round(sum(
            self._holder_sum(connection, "container", row["container_id"]) for row in containers
        ), 3)
        in_containers_stored = round(sum(row["quantity"] for row in containers), 3)
        brewed = prep_row["quantity_brewed"]
        holders_match = abs(remaining_computed - prep_row["quantity_remaining"]) <= EPSILON
        holders_match = holders_match and all(
            abs(self._holder_sum(connection, "container", row["container_id"]) - row["quantity"]) <= EPSILON
            for row in containers
        )
        balanced = abs(brewed - (remaining_computed + in_containers_computed
                                 + distributed + lost)) <= EPSILON
        return {
            "prep_id": prep_id,
            "brewed": brewed,
            "remaining": prep_row["quantity_remaining"],
            "remaining_computed": remaining_computed,
            "in_containers": in_containers_stored,
            "in_containers_computed": in_containers_computed,
            "distributed": round(distributed, 3),
            "lost": round(lost, 3),
            "consistent": holders_match and balanced,
        }

    def get_preparation_lineage(self, prep_id: str) -> dict[str, Any]:
        """闭市前核对：一壶茶的原料批号、配方版本、容器去向与收支配平。"""

        connection = self.database.connection
        prep = self._load_preparation(connection, prep_id)
        version = self._recipe_version_view(
            self._load_recipe_version(connection, prep["recipe_version_id"]))
        ingredient_rows = connection.execute(
            "SELECT pi.batch_id, pi.ingredient_name, pi.quantity_used, pi.unit, "
            "b.lot_number, b.status AS batch_status FROM preparation_ingredients pi "
            "JOIN ingredient_batches b ON b.batch_id=pi.batch_id WHERE pi.prep_id=? "
            "ORDER BY pi.batch_id",
            (prep_id,),
        ).fetchall()
        container_rows = connection.execute(
            "SELECT * FROM containers WHERE prep_id=? ORDER BY created_at, container_id", (prep_id,)
        ).fetchall()
        containers = [self._container_view(row) for row in container_rows]
        claims = self._claims_for_holders(connection, prep_id,
                                          [row["container_id"] for row in container_rows])
        return {
            "prep_id": prep_id,
            "site_id": prep["site_id"],
            "status": prep["status"],
            "unit": prep["unit"],
            "recipe_version": version,
            "ingredient_batches": [dict(row) for row in ingredient_rows],
            "containers": containers,
            "claims": claims,
            "reconciliation": self._reconciliation(connection, prep),
        }

    def get_recall_coverage(self, batch_id: str) -> dict[str, Any]:
        """给出风险来源与召回覆盖范围：受影响的制备、容器、已发放记录与数量汇总。"""

        connection = self.database.connection
        batch = self._load_batch(connection, batch_id)
        prep_rows = connection.execute(
            "SELECT p.*, pi.quantity_used FROM preparations p "
            "JOIN preparation_ingredients pi ON p.prep_id=pi.prep_id "
            "WHERE pi.batch_id=? ORDER BY p.created_at, p.prep_id",
            (batch_id,),
        ).fetchall()
        prep_ids = [row["prep_id"] for row in prep_rows]
        containers: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        lost = 0.0
        if prep_ids:
            marks = ",".join("?" for _ in prep_ids)
            container_rows = connection.execute(
                f"SELECT * FROM containers WHERE prep_id IN ({marks}) ORDER BY created_at, container_id",
                prep_ids,
            ).fetchall()
            containers = [self._container_view(row) for row in container_rows]
            claim_rows = connection.execute(
                f"SELECT * FROM claims WHERE (holder_type='preparation' AND holder_id IN ({marks})) "
                f"OR (holder_type='container' AND holder_id IN "
                f"(SELECT container_id FROM containers WHERE prep_id IN ({marks}))) "
                "ORDER BY created_at, claim_id",
                prep_ids + prep_ids,
            ).fetchall()
            claims = [self._claim_view(row) for row in claim_rows]
            loss_row = connection.execute(
                f"SELECT COALESCE(SUM(delta), 0) AS total FROM stock_entries "
                f"WHERE entry_type='loss' AND ((holder_type='preparation' AND holder_id IN ({marks})) "
                f"OR (holder_type='container' AND holder_id IN "
                f"(SELECT container_id FROM containers WHERE prep_id IN ({marks}))))",
                prep_ids + prep_ids,
            ).fetchone()
            lost = -loss_row["total"]
        distributed = round(sum(claim["quantity"] for claim in claims), 3)
        return {
            "batch_id": batch_id,
            "risk_source": {
                "ingredient_name": batch["ingredient_name"],
                "lot_number": batch["lot_number"],
                "site_id": batch["site_id"],
                "strict_tags": json.loads(batch["strict_tags_json"]),
                "caution_tags": json.loads(batch["caution_tags_json"]),
                "status": batch["status"],
                "freeze_reason": batch["freeze_reason"],
            },
            "preparations": [{
                "prep_id": row["prep_id"], "site_id": row["site_id"],
                "recipe_version_id": row["recipe_version_id"],
                "quantity_brewed": row["quantity_brewed"],
                "quantity_remaining": row["quantity_remaining"],
                "batch_quantity_used": row["quantity_used"],
                "unit": row["unit"], "status": row["status"],
            } for row in prep_rows],
            "containers": containers,
            "claims": claims,
            "totals": {
                "brewed": round(sum(row["quantity_brewed"] for row in prep_rows), 3),
                "remaining_in_preparations": round(sum(row["quantity_remaining"] for row in prep_rows), 3),
                "in_containers": round(sum(item["quantity"] for item in containers), 3),
                "distributed": distributed,
                "lost": round(lost, 3),
            },
        }

    def recompute_inventory(self, site_id: str) -> dict[str, Any]:
        """由台账分录复算场所库存，与现值比对并列出差异。"""

        connection = self.database.connection
        self._site(connection, site_id)
        holders: list[dict[str, Any]] = []
        prep_rows = connection.execute(
            "SELECT * FROM preparations WHERE site_id=? ORDER BY created_at, prep_id", (site_id,)
        ).fetchall()
        for row in prep_rows:
            computed = self._holder_sum(connection, "preparation", row["prep_id"])
            holders.append({
                "holder_type": "preparation", "holder_id": row["prep_id"],
                "stored": row["quantity_remaining"], "computed": computed,
                "matches": abs(computed - row["quantity_remaining"]) <= EPSILON,
            })
        container_rows = connection.execute(
            "SELECT * FROM containers WHERE site_id=? ORDER BY created_at, container_id", (site_id,)
        ).fetchall()
        for row in container_rows:
            computed = self._holder_sum(connection, "container", row["container_id"])
            holders.append({
                "holder_type": "container", "holder_id": row["container_id"],
                "stored": row["quantity"], "computed": computed,
                "matches": abs(computed - row["quantity"]) <= EPSILON,
            })
        discrepancies = [holder for holder in holders if not holder["matches"]]
        reconciliations = [self._reconciliation(connection, row) for row in prep_rows]
        unbalanced = [item for item in reconciliations if not item["consistent"]]
        return {
            "site_id": site_id,
            "consistent": not discrepancies and not unbalanced,
            "holders": holders,
            "discrepancies": discrepancies,
            "preparations": reconciliations,
        }

    def get_container(self, container_id: str) -> dict[str, Any]:
        row = self._load_container(self.database.connection, container_id)
        return self._container_view(row)

    def get_transfer(self, transfer_id: str) -> dict[str, Any]:
        row = self._load_transfer(self.database.connection, transfer_id)
        return {"transfer_id": row["transfer_id"], "container_id": row["container_id"],
                "from_site_id": row["from_site_id"], "to_site_id": row["to_site_id"],
                "quantity": row["quantity"], "unit": row["unit"], "status": row["status"],
                "initiated_by": row["initiated_by"], "resolved_by": row["resolved_by"]}
