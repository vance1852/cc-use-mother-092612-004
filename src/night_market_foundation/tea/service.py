"""药膳茶饮制备谱系服务。

在基础服务（权限、幂等、事务、哈希审计）之上实现：

* 以原料批号与配方版本建立不可覆盖的制备谱系；
* 对产出、拆分、合并、报损、发放、转移形成只追加且数量守恒的流水账；
* 结合参与者忌口给出 served / manual_confirm / denied 判定及风险来源；
* 替代配方经审核者确认风险差异后方可用于后续制备；
* 原料问题触发在场关联容器与未来发放的冻结召回，历史发放保留；
* 跨摊转移双方确认后整体原子生效；
* 离线补传依靠全局 request_id 幂等与单调流水序号获得稳定结果。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..audit import append_event, canonical_json, digest
from ..clock import Clock, SystemClock
from ..errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from ..models import Actor, WriteReceipt
from ..service import IDENTIFIER, DomainService
from ..storage import Database
from .errors import (
    FrozenError,
    InventoryError,
    ManualConfirmationRequired,
    ServingDenied,
    TransferStateError,
)
from .models import (
    Balance,
    Container,
    DispenseRecord,
    Formula,
    LineageEdge,
    MaterialBatch,
    Movement,
    ParticipantProfile,
    Preparation,
    Transfer,
)
from .storage import init_tea_tables

EPSILON = 1e-9
DECISION_SERVED = "served"
DECISION_MANUAL = "manual_confirm"
DECISION_DENIED = "denied"

# 风险级别：hard 命中忌口 → 不得提供；caution 命中 → 需人工确认。
LEVEL_HARD = "hard"
LEVEL_CAUTION = "caution"


class TeaService:
    """制备谱系领域服务，复用基础服务的权限与幂等机制。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.base = DomainService(database, self.clock)
        init_tea_tables(database)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    @staticmethod
    def _id(value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    @staticmethod
    def _amount(value: Any, field: str) -> float:
        if isinstance(value, bool):
            raise ValidationError(f"{field} 必须是正数")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是数字") from exc
        if number != number or number in (float("inf"), float("-inf")) or number <= 0:
            raise ValidationError(f"{field} 必须是正数")
        return number

    @staticmethod
    def _restrictions(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValidationError("忌口项必须是非空字符串列表")
        return [item.strip() for item in value]

    def _actor(self, connection, actor_id: str) -> Actor:
        return self.base._actor(connection, actor_id)

    def _require(self, actor: Actor, *roles: str) -> None:
        self.base._require(actor, *roles)

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create) -> WriteReceipt:
        return self.base._idempotent(connection, request_id=request_id, action=action,
                                     payload=payload, create=create)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def verify_audit(self):
        """委托基础服务校验哈希审计链。"""

        return self.base.verify_audit()

    def audit_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        """委托基础服务读取审计事件（含茶饮模块追加的事件）。"""

        return self.base.audit_events(after_sequence)

    # ------------------------------------------------------------------
    # 流水账（数量守恒核心）
    # ------------------------------------------------------------------

    def _movement(self, connection, *, container_id: str, direction: str, signed_amount: float,
                  counterparty_container_id: str | None = None, reference_type: str | None = None,
                  reference_id: str | None = None, memo: str = "", created_by: str) -> Movement:
        amount_abs = abs(signed_amount)
        if amount_abs <= 0:
            raise ValidationError("流水数量必须非零")
        movement_id = uuid.uuid4().hex
        cursor = connection.execute(
            "INSERT INTO tea_movements(movement_id,container_id,direction,signed_amount,amount_abs,"
            "counterparty_container_id,reference_type,reference_id,memo,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (movement_id, container_id, direction, signed_amount, amount_abs,
             counterparty_container_id, reference_type, reference_id, memo, created_by, self._now()),
        )
        # amount 是流水的缓存：写入后立即在同一事务内复算并校验非负。
        total = connection.execute(
            "SELECT COALESCE(SUM(signed_amount),0) AS total FROM tea_movements WHERE container_id=?",
            (container_id,),
        ).fetchone()["total"]
        if total < -EPSILON:
            raise InventoryError(f"容器 {container_id} 余额不足，数量守恒校验失败")
        connection.execute("UPDATE tea_containers SET amount=? WHERE container_id=?",
                           (max(total, 0.0), container_id))
        return Movement(movement_id, cursor.lastrowid, container_id, direction, amount_abs,
                        counterparty_container_id, reference_type, reference_id, memo,
                        created_by, self._now())

    def _container_row(self, connection, container_id: str):
        row = connection.execute("SELECT * FROM tea_containers WHERE container_id=?",
                                 (container_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"容器 {container_id} 不存在")
        return row

    def _require_active_container(self, connection, container_id: str, *, on_site: bool = True):
        row = self._container_row(connection, container_id)
        if row["status"] == "frozen":
            raise FrozenError(f"容器 {container_id} 已被冻结，禁止出库操作")
        if on_site and row["disposition"] != "on_site":
            raise ConflictError(f"容器 {container_id} 当前不{row['disposition']}，不在场内可操作状态")
        return row

    def _edge(self, connection, *, parent_id: str, child_id: str, amount: float,
              relation: str, reference_type: str, reference_id: str) -> None:
        connection.execute(
            "INSERT INTO tea_edges(edge_id,parent_container_id,child_container_id,amount,relation,"
            "reference_type,reference_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, parent_id, child_id, amount, relation,
             reference_type, reference_id, self._now()),
        )

    # ------------------------------------------------------------------
    # 原料批号
    # ------------------------------------------------------------------

    def register_material_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                                name: str, initial_amount: float, unit: str,
                                site_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "name": name,
                   "initial_amount": initial_amount, "unit": unit, "site_id": site_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch_id = self._id(batch_id, "batch_id")
            name = self.base._text(name, "name")
            unit = self.base._text(unit, "unit", 20)
            initial_amount = self._amount(initial_amount, "initial_amount")
            site = self._site(connection, site_id)
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所登记原料")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO tea_material_batches(batch_id,name,initial_amount,unit,site_id,"
                        "status,created_by,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                        (batch_id, name, initial_amount, unit, site_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("原料批号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="tea.material_batch.registered",
                            resource_type="material_batch", resource_id=batch_id,
                            detail={"name": name, "initial_amount": initial_amount,
                                    "unit": unit, "site_id": site_id})
                return "material_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_register_material_batch", payload=payload, create=create)

    def get_material_batch(self, batch_id: str) -> MaterialBatch:
        row = self.database.connection.execute(
            "SELECT * FROM tea_material_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("原料批号不存在")
        return MaterialBatch(row["batch_id"], row["name"], row["initial_amount"], row["unit"],
                             row["site_id"], row["status"], row["created_by"], row["created_at"])

    # ------------------------------------------------------------------
    # 配方版本与替代审核
    # ------------------------------------------------------------------

    def _normalize_ingredients(self, ingredients: Any) -> list[dict[str, Any]]:
        if not isinstance(ingredients, list) or not ingredients:
            raise ValidationError("ingredients 必须是非空列表")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in ingredients:
            if not isinstance(item, dict):
                raise ValidationError("每个原料项必须是对象")
            material_id = str(item.get("material_id", "")).strip()
            if not IDENTIFIER.fullmatch(material_id):
                raise ValidationError("ingredients.material_id 格式无效")
            if material_id in seen:
                raise ValidationError(f"原料 {material_id} 在配方中重复")
            seen.add(material_id)
            amount = self._amount(item.get("amount"), "ingredients.amount")
            role = str(item.get("role", "material")).strip() or "material"
            normalized.append({"material_id": material_id, "amount": amount, "role": role})
        return normalized

    def create_formula(self, *, request_id: str, actor_id: str, formula_id: str, name: str,
                       ingredients: list[dict[str, Any]], contraindications: list[str] | None = None,
                       cautions: list[str] | None = None, supersedes: str | None = None) -> WriteReceipt:
        """登记一版配方；若 supersedes 既有配方，则先进入候选（替代）状态。"""

        contraindications = self._restrictions(contraindications)
        cautions = self._restrictions(cautions)
        payload = {"actor_id": actor_id, "formula_id": formula_id, "name": name,
                   "ingredients": ingredients, "contraindications": contraindications,
                   "cautions": cautions, "supersedes": supersedes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            formula_id = self._id(formula_id, "formula_id")
            name = self.base._text(name, "name")
            ingredients = self._normalize_ingredients(ingredients)
            latest = connection.execute(
                "SELECT formula_id, version FROM tea_formulas WHERE name=? ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
            if latest is not None:
                # 同名已经存在版本 → 本版是替代配方，必须显式声明替代上一版，
                # 并经审核者确认风险差异后才能用于之后的制备。
                if not supersedes:
                    raise ValidationError(
                        f"配方 {name} 已有 v{latest['version']}，新版必须通过 supersedes 声明为替代配方")
                supersedes = self._id(supersedes, "supersedes")
                previous = connection.execute("SELECT * FROM tea_formulas WHERE formula_id=?",
                                              (supersedes,)).fetchone()
                if previous is None:
                    raise NotFoundError("被替代的配方不存在")
                if previous["name"] != name:
                    raise ValidationError("替代配方必须与被替代配方同名")
                if previous["formula_id"] != latest["formula_id"]:
                    raise ConflictError(
                        f"只能替代最新版本 v{latest['version']}（{latest['formula_id']}）")
            elif supersedes:
                raise ValidationError("首个配方版本不能声明 supersedes")

            def create():
                version = latest["version"] + 1 if latest is not None else 1
                # 首版直接可用；替代版本必须等待风险审核。
                status = "candidate" if latest is not None else "approved"
                try:
                    connection.execute(
                        "INSERT INTO tea_formulas(formula_id,name,version,ingredients_json,ingredients_hash,"
                        "contraindications_json,cautions_json,status,supersedes,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (formula_id, name, version, canonical_json(ingredients), digest(ingredients),
                         canonical_json(contraindications), canonical_json(cautions), status,
                         supersedes, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("配方编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="tea.formula.created",
                            resource_type="formula", resource_id=formula_id,
                            detail={"name": name, "version": version, "status": status,
                                    "supersedes": supersedes, "ingredients_hash": digest(ingredients)})
                return "formula", formula_id, {"formula_id": formula_id, "version": version,
                                               "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_create_formula", payload=payload, create=create)

    def review_formula(self, *, request_id: str, actor_id: str, formula_id: str,
                       decision: str, risk_note: str) -> WriteReceipt:
        """审核者确认替代配方与原配方的风险差异；审核结论不可覆盖。"""

        payload = {"actor_id": actor_id, "formula_id": formula_id, "decision": decision,
                   "risk_note": risk_note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "admin")
            formula_id = self._id(formula_id, "formula_id")
            if decision not in ("approved", "rejected"):
                raise ValidationError("decision 必须是 approved 或 rejected")
            risk_note = self.base._text(risk_note, "risk_note", 1000)
            row = connection.execute("SELECT * FROM tea_formulas WHERE formula_id=?",
                                     (formula_id,)).fetchone()
            if row is None:
                raise NotFoundError("配方不存在")
            if row["status"] != "candidate":
                raise ConflictError("该配方已经审核，审核结论不可覆盖")

            def create():
                connection.execute(
                    "UPDATE tea_formulas SET status=?, reviewed_by=?, risk_note=?, reviewed_at=? "
                    "WHERE formula_id=?",
                    (decision, actor_id, risk_note, self._now(), formula_id),
                )
                self._audit(connection, actor_id=actor_id, action="tea.formula.reviewed",
                            resource_type="formula", resource_id=formula_id,
                            detail={"decision": decision, "risk_note": risk_note,
                                    "supersedes": row["supersedes"]})
                return "formula", formula_id, {"formula_id": formula_id, "status": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_review_formula", payload=payload, create=create)

    def get_formula(self, formula_id: str) -> Formula:
        row = self.database.connection.execute("SELECT * FROM tea_formulas WHERE formula_id=?",
                                               (formula_id,)).fetchone()
        if row is None:
            raise NotFoundError("配方不存在")
        return self._formula_from_row(row)

    def _formula_from_row(self, row) -> Formula:
        return Formula(
            row["formula_id"], row["name"], row["version"],
            tuple(json.loads(row["ingredients_json"])),
            tuple(json.loads(row["contraindications_json"])),
            tuple(json.loads(row["cautions_json"])),
            row["status"], row["supersedes"], row["created_by"], row["reviewed_by"],
            row["risk_note"], row["created_at"], row["reviewed_at"],
        )

    def list_formulas(self, name: str | None = None) -> list[Formula]:
        query = "SELECT * FROM tea_formulas"
        parameters: list[Any] = []
        if name:
            query += " WHERE name=?"
            parameters.append(name)
        query += " ORDER BY created_at, version"
        return [self._formula_from_row(row)
                for row in self.database.connection.execute(query, parameters)]

    # ------------------------------------------------------------------
    # 制备（产出）
    # ------------------------------------------------------------------

    def prepare_tea(self, *, request_id: str, actor_id: str, preparation_id: str,
                    formula_id: str, inputs: list[dict[str, Any]], output_amount: float,
                    output_container_id: str, site_id: str, unit: str | None = None) -> WriteReceipt:
        """按一版已审核配方与确定原料批号完成一次制备，产出进入新容器。"""

        payload = {"actor_id": actor_id, "preparation_id": preparation_id,
                   "formula_id": formula_id, "inputs": inputs, "output_amount": output_amount,
                   "output_container_id": output_container_id, "site_id": site_id, "unit": unit}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            preparation_id = self._id(preparation_id, "preparation_id")
            output_container_id = self._id(output_container_id, "output_container_id")
            output_amount = self._amount(output_amount, "output_amount")
            site = self._site(connection, site_id)
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所制备")
            formula_id_text = self._id(formula_id, "formula_id")

            def create():
                # 配方状态与原料批号状态读取放在幂等命中之后：批号可能在首次制备后被冻结，
                # 但合法重放必须稳定返回原制备回执。
                formula_row = connection.execute("SELECT * FROM tea_formulas WHERE formula_id=?",
                                                 (formula_id_text,)).fetchone()
                if formula_row is None:
                    raise NotFoundError("配方不存在")
                if formula_row["status"] != "approved":
                    raise ConflictError("配方尚未通过审核（替代配方须先确认风险差异），不得用于制备")
                formula = self._formula_from_row(formula_row)
                normalized_inputs = self._normalize_inputs(connection, inputs, formula)
                resolved_unit = self.base._text(
                    unit or self._infer_unit(connection, normalized_inputs), "unit", 20)
                if connection.execute("SELECT 1 FROM tea_containers WHERE container_id=?",
                                      (output_container_id,)).fetchone():
                    raise ConflictError("产出容器编号已经存在")
                snapshot = {
                    "formula_id": formula.formula_id,
                    "name": formula.name,
                    "version": formula.version,
                    "ingredients": list(formula.ingredients),
                    "contraindications": list(formula.contraindications),
                    "cautions": list(formula.cautions),
                    "ingredients_hash": formula_row["ingredients_hash"],
                }
                connection.execute(
                    "INSERT INTO tea_preparations(preparation_id,formula_id,formula_version,"
                    "formula_snapshot_json,site_id,output_container_id,output_amount,unit,inputs_json,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'normal',?,?)",
                    (preparation_id, formula.formula_id, formula.version, canonical_json(snapshot),
                     site_id, output_container_id, output_amount, resolved_unit,
                     canonical_json(normalized_inputs), actor_id, self._now()),
                )
                connection.execute(
                    "INSERT INTO tea_containers(container_id,kind,unit,site_id,amount,status,"
                    "disposition,created_in,created_by,created_at) "
                    "VALUES(?, 'pot', ?, ?, ?, 'active', 'on_site', ?, ?, ?)",
                    (output_container_id, resolved_unit, site_id, output_amount, preparation_id,
                     actor_id, self._now()),
                )
                self._movement(connection, container_id=output_container_id, direction="produce",
                               signed_amount=output_amount, reference_type="preparation",
                               reference_id=preparation_id,
                               memo=f"按配方 {formula.name} v{formula.version} 制备",
                               created_by=actor_id)
                self._audit(connection, actor_id=actor_id, action="tea.preparation.recorded",
                            resource_type="preparation", resource_id=preparation_id,
                            detail={"formula_id": formula.formula_id, "formula_version": formula.version,
                                    "output_container_id": output_container_id,
                                    "output_amount": output_amount, "unit": resolved_unit,
                                    "inputs": normalized_inputs,
                                    "formula_snapshot_hash": digest(snapshot)})
                return "preparation", preparation_id, {
                    "preparation_id": preparation_id,
                    "output_container_id": output_container_id,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_prepare_tea", payload=payload, create=create)

    def _normalize_inputs(self, connection, inputs: Any, formula: Formula) -> list[dict[str, Any]]:
        if not isinstance(inputs, list) or not inputs:
            raise ValidationError("inputs 必须是非空列表")
        formula_map = {item["material_id"]: item for item in formula.ingredients}
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for item in inputs:
            if not isinstance(item, dict):
                raise ValidationError("每个原料投入项必须是对象")
            batch_id = str(item.get("batch_id", "")).strip()
            material_id = str(item.get("material_id", "")).strip()
            if not IDENTIFIER.fullmatch(batch_id) or not IDENTIFIER.fullmatch(material_id):
                raise ValidationError("inputs 中批号或原料编号格式无效")
            if material_id not in formula_map:
                raise ValidationError(f"原料 {material_id} 不在配方 {formula.formula_id} 中")
            if material_id in seen:
                raise ValidationError(f"原料 {material_id} 的批号投入重复")
            seen.add(material_id)
            amount = self._amount(item.get("amount"), "inputs.amount")
            batch_row = connection.execute(
                "SELECT * FROM tea_material_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch_row is None:
                raise NotFoundError(f"原料批号 {batch_id} 不存在")
            if batch_row["status"] == "frozen":
                raise FrozenError(f"原料批号 {batch_id} 已冻结，不得用于制备")
            normalized.append({"material_id": material_id, "batch_id": batch_id,
                               "amount": amount, "batch_name": batch_row["name"]})
        missing = set(formula_map) - seen
        if missing:
            raise ValidationError(f"缺少配方要求的原料批号投入：{sorted(missing)}")
        return normalized

    @staticmethod
    def _infer_unit(connection, normalized_inputs: list[dict[str, Any]]) -> str:
        units = {row["unit"] for row in connection.execute(
            "SELECT unit FROM tea_material_batches WHERE batch_id IN ({})".format(
                ",".join("?" for _ in normalized_inputs)),
            [item["batch_id"] for item in normalized_inputs])}
        if len(units) == 1:
            return next(iter(units))
        return "serving"

    def get_preparation(self, preparation_id: str) -> Preparation:
        row = self.database.connection.execute(
            "SELECT * FROM tea_preparations WHERE preparation_id=?", (preparation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("制备记录不存在")
        return Preparation(
            row["preparation_id"], row["formula_id"], row["formula_version"],
            json.loads(row["formula_snapshot_json"]), row["site_id"],
            row["output_container_id"], row["output_amount"], row["unit"],
            tuple(json.loads(row["inputs_json"])), row["status"],
            row["created_by"], row["created_at"],
        )

    # ------------------------------------------------------------------
    # 拆分与合并
    # ------------------------------------------------------------------

    def split_container(self, *, request_id: str, actor_id: str, source_container_id: str,
                        new_container_id: str, amount: float, memo: str = "") -> WriteReceipt:
        """把在场容器中的一部分整体拆到新容器，来源与去向守恒并登记谱系边。"""

        payload = {"actor_id": actor_id, "source_container_id": source_container_id,
                   "new_container_id": new_container_id, "amount": amount, "memo": memo}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            source_container_id = self._id(source_container_id, "source_container_id")
            new_container_id = self._id(new_container_id, "new_container_id")
            amount = self._amount(amount, "amount")

            def create():
                # 状态相关校验放在幂等命中之后：合法重放时来源可能已被拆空或冻结。
                source = self._require_active_container(connection, source_container_id)
                if amount > source["amount"] + EPSILON:
                    raise InventoryError("拆分数量不能超过来源容器当前余额")
                if connection.execute("SELECT 1 FROM tea_containers WHERE container_id=?",
                                      (new_container_id,)).fetchone():
                    raise ConflictError("新容器编号已经存在")
                connection.execute(
                    "INSERT INTO tea_containers(container_id,kind,unit,site_id,amount,status,"
                    "disposition,created_in,created_by,created_at) "
                    "VALUES(?, 'vessel', ?, ?, 0, 'active', 'on_site', NULL, ?, ?)",
                    (new_container_id, source["unit"], source["site_id"],
                     actor_id, self._now()),
                )
                self._movement(connection, container_id=source_container_id, direction="split",
                               signed_amount=-amount, counterparty_container_id=new_container_id,
                               reference_type="container", reference_id=new_container_id,
                               memo=memo, created_by=actor_id)
                self._movement(connection, container_id=new_container_id, direction="split",
                               signed_amount=amount, counterparty_container_id=source_container_id,
                               reference_type="container", reference_id=source_container_id,
                               memo=memo, created_by=actor_id)
                self._edge(connection, parent_id=source_container_id, child_id=new_container_id,
                           amount=amount, relation="split", reference_type="container",
                           reference_id=new_container_id)
                self._refresh_disposition(connection, source_container_id)
                self._audit(connection, actor_id=actor_id, action="tea.container.split",
                            resource_type="container", resource_id=new_container_id,
                            detail={"source_container_id": source_container_id, "amount": amount})
                return "container", new_container_id, {
                    "container_id": new_container_id,
                    "source_container_id": source_container_id,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_split_container", payload=payload, create=create)

    def merge_containers(self, *, request_id: str, actor_id: str, source_container_ids: list[str],
                         target_container_id: str, amount: float | None = None,
                         memo: str = "") -> WriteReceipt:
        """把多个在场容器合并到一个新容器；默认全部并入，也可指定各来源等量并入量。

        合并后的新容器同时继承所有来源的谱系（多父边），因此风险与召回可沿任一来源追溯。
        """

        payload = {"actor_id": actor_id, "source_container_ids": source_container_ids,
                   "target_container_id": target_container_id, "amount": amount, "memo": memo}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if not isinstance(source_container_ids, list) or len(source_container_ids) < 2:
                raise ValidationError("合并至少需要两个来源容器")
            source_container_ids = [self._id(value, "source_container_ids")
                                    for value in source_container_ids]
            if len(set(source_container_ids)) != len(source_container_ids):
                raise ValidationError("来源容器不能重复")
            target_container_id = self._id(target_container_id, "target_container_id")
            each_amount = self._amount(amount, "amount") if amount is not None else None

            def create():
                # 状态相关校验放在幂等命中之后。
                sources = [self._require_active_container(connection, cid)
                           for cid in source_container_ids]
                units = {row["unit"] for row in sources}
                if len(units) != 1:
                    raise ValidationError("计量单位不一致的容器不能合并")
                sites = {row["site_id"] for row in sources}
                if len(sites) != 1:
                    raise ConflictError("跨场所容器请使用转移流程，不能直接合并")
                if each_amount is not None:
                    for row in sources:
                        if each_amount > row["amount"] + EPSILON:
                            raise InventoryError(f"容器 {row['container_id']} 余额不足以合并指定数量")
                if connection.execute("SELECT 1 FROM tea_containers WHERE container_id=?",
                                      (target_container_id,)).fetchone():
                    raise ConflictError("目标容器编号已经存在")
                unit = sources[0]["unit"]
                connection.execute(
                    "INSERT INTO tea_containers(container_id,kind,unit,site_id,amount,status,"
                    "disposition,created_in,created_by,created_at) "
                    "VALUES(?, 'vessel', ?, ?, 0, 'active', 'on_site', NULL, ?, ?)",
                    (target_container_id, unit, sources[0]["site_id"],
                     actor_id, self._now()),
                )
                total_in = 0.0
                for row in sources:
                    take = each_amount if each_amount is not None else row["amount"]
                    if take <= 0:
                        continue
                    source_id = row["container_id"]
                    self._movement(connection, container_id=source_id, direction="merge",
                                   signed_amount=-take, counterparty_container_id=target_container_id,
                                   reference_type="container", reference_id=target_container_id,
                                   memo=memo, created_by=actor_id)
                    self._movement(connection, container_id=target_container_id, direction="merge",
                                   signed_amount=take, counterparty_container_id=source_id,
                                   reference_type="container", reference_id=source_id,
                                   memo=memo, created_by=actor_id)
                    self._edge(connection, parent_id=source_id, child_id=target_container_id,
                               amount=take, relation="merge", reference_type="container",
                               reference_id=target_container_id)
                    self._refresh_disposition(connection, source_id)
                    total_in += take
                if total_in <= 0:
                    raise InventoryError("合并数量必须大于零")
                self._audit(connection, actor_id=actor_id, action="tea.container.merged",
                            resource_type="container", resource_id=target_container_id,
                            detail={"source_container_ids": source_container_ids,
                                    "total_amount": total_in})
                return "container", target_container_id, {
                    "container_id": target_container_id,
                    "merged_amount": total_in,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_merge_containers", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 报损
    # ------------------------------------------------------------------

    def report_loss(self, *, request_id: str, actor_id: str, container_id: str,
                    amount: float, reason: str) -> WriteReceipt:
        """对在场容器登记报损；报损只减库存，不产生去向容器。"""

        payload = {"actor_id": actor_id, "container_id": container_id,
                   "amount": amount, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            container_id = self._id(container_id, "container_id")
            amount = self._amount(amount, "amount")
            reason = self.base._text(reason, "reason", 500)

            def create():
                # 状态相关校验放在幂等命中之后：合法重放时容器可能已被报空或冻结。
                row = self._require_active_container(connection, container_id)
                if amount > row["amount"] + EPSILON:
                    raise InventoryError("报损数量不能超过容器当前余额")
                self._movement(connection, container_id=container_id, direction="loss",
                               signed_amount=-amount, reference_type="loss",
                               reference_id=request_id, memo=reason, created_by=actor_id)
                self._refresh_disposition(connection, container_id)
                self._audit(connection, actor_id=actor_id, action="tea.loss.reported",
                            resource_type="container", resource_id=container_id,
                            detail={"amount": amount, "reason": reason})
                return "container", container_id, {"container_id": container_id, "lost_amount": amount}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_report_loss", payload=payload, create=create)

    def _refresh_disposition(self, connection, container_id: str) -> None:
        row = connection.execute("SELECT amount FROM tea_containers WHERE container_id=?",
                                 (container_id,)).fetchone()
        if row["amount"] <= EPSILON:
            connection.execute(
                "UPDATE tea_containers SET disposition='depleted' WHERE container_id=? AND disposition='on_site'",
                (container_id,),
            )

    # ------------------------------------------------------------------
    # 参与者忌口与发放
    # ------------------------------------------------------------------

    def upsert_participant(self, *, request_id: str, actor_id: str, participant_id: str,
                           restrictions: list[str], note: str = "") -> WriteReceipt:
        """登记参与者主动提供的忌口信息（以最新一次主动提供为准）。"""

        restrictions = self._restrictions(restrictions)
        note = str(note or "").strip()
        if len(note) > 500:
            raise ValidationError("note 不能超过 500 个字符")
        payload = {"actor_id": actor_id, "participant_id": participant_id,
                   "restrictions": restrictions, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            participant_id = self._id(participant_id, "participant_id")

            def create():
                connection.execute(
                    "INSERT INTO tea_participants(participant_id,restrictions_json,note,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(participant_id) DO UPDATE SET "
                    "restrictions_json=excluded.restrictions_json,note=excluded.note,"
                    "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                    (participant_id, canonical_json(restrictions), note, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="tea.participant.updated",
                            resource_type="participant", resource_id=participant_id,
                            detail={"restrictions": restrictions, "note": note})
                return "participant", participant_id, {"participant_id": participant_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_upsert_participant", payload=payload, create=create)

    def evaluate_serving(self, *, participant_id: str, container_id: str,
                         amount: float | None = None) -> dict[str, Any]:
        """发放前评估：返回可提供 / 需人工确认 / 不得提供，并列明每条风险来源。"""

        participant_id = self._id(participant_id, "participant_id")
        container_id = self._id(container_id, "container_id")
        connection = self.database.connection
        profile_row = connection.execute("SELECT * FROM tea_participants WHERE participant_id=?",
                                         (participant_id,)).fetchone()
        if profile_row is None:
            raise NotFoundError("参与者尚未提供忌口资料")
        container = self._container_row(connection, container_id)
        restrictions = set(json.loads(profile_row["restrictions_json"]))
        preps = self._container_preparations(connection, container_id)
        if not preps:
            raise NotFoundError("该容器没有可追溯的制备记录")

        reasons: list[dict[str, Any]] = []
        decision = DECISION_SERVED
        if container["status"] == "frozen":
            decision = DECISION_DENIED
            reasons.append({"level": LEVEL_HARD, "type": "container_frozen",
                            "source": {"container_id": container_id},
                            "message": "容器已因原料问题冻结，禁止发放"})
        if container["disposition"] != "on_site":
            decision = DECISION_DENIED
            reasons.append({"level": LEVEL_HARD, "type": "not_on_site",
                            "source": {"container_id": container_id, "disposition": container["disposition"]},
                            "message": "容器不在场内可发放状态"})
        if amount is not None and amount > container["amount"] + EPSILON and decision != DECISION_DENIED:
            decision = DECISION_DENIED
            reasons.append({"level": LEVEL_HARD, "type": "insufficient_amount",
                            "source": {"container_id": container_id, "available": container["amount"]},
                            "message": "剩余量不足以发放"})
        for prep in preps:
            snapshot = prep["formula_snapshot_json"]
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            formula_id = prep["formula_id"]
            for item in restrictions:
                if item in snapshot.get("contraindications", []):
                    decision = DECISION_DENIED
                    reasons.append({
                        "level": LEVEL_HARD, "type": "contraindication",
                        "source": {"preparation_id": prep["preparation_id"],
                                   "formula_id": formula_id,
                                   "formula_version": prep["formula_version"],
                                   "restriction": item},
                        "message": f"配方 v{prep['formula_version']} 明确忌口「{item}」，不得提供",
                    })
                elif item in snapshot.get("cautions", []):
                    if decision != DECISION_DENIED:
                        decision = DECISION_MANUAL
                    reasons.append({
                        "level": LEVEL_CAUTION, "type": "caution",
                        "source": {"preparation_id": prep["preparation_id"],
                                   "formula_id": formula_id,
                                   "formula_version": prep["formula_version"],
                                   "restriction": item},
                        "message": f"配方 v{prep['formula_version']} 对「{item}」有慎用提示，需人工确认",
                    })
        return {"decision": decision, "participant_id": participant_id,
                "container_id": container_id, "reasons": reasons,
                "restrictions": sorted(restrictions),
                "available_amount": container["amount"], "unit": container["unit"]}

    def dispense(self, *, request_id: str, actor_id: str, participant_id: str,
                 container_id: str, amount: float, confirm_manual: bool = False,
                 confirmation_note: str = "") -> WriteReceipt:
        """执行发放。

        * served：直接出库；
        * manual_confirm：先调用评估接口（或试探本接口）取得风险来源；操作者核对后，
          以一个新的 request_id 带 confirm_manual=true 与确认说明完成发放——试探与确认
          是内容不同的两个请求，不能共用 request_id；
        * denied：不得提供，直接返回风险来源，不产生任何记录。
        已发生的发放事实不随后续配方修订或冻结而改变。
        """

        payload = {"actor_id": actor_id, "participant_id": participant_id,
                   "container_id": container_id, "amount": amount,
                   "confirm_manual": confirm_manual, "confirmation_note": confirmation_note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            participant_id = self._id(participant_id, "participant_id")
            container_id = self._id(container_id, "container_id")
            amount = self._amount(amount, "amount")
            confirmation_note = str(confirmation_note or "").strip()

            def create():
                # 评估读取当前状态，放在幂等命中之后：成功发放的重放即使容器
                # 随后被冻结或发空，也稳定返回原回执。
                evaluation = self.evaluate_serving(participant_id=participant_id,
                                                   container_id=container_id, amount=amount)
                decision = evaluation["decision"]
                if decision == DECISION_DENIED:
                    if any(reason["type"] == "container_frozen" for reason in evaluation["reasons"]):
                        raise FrozenError("容器已冻结，未来发放已暂停", evaluation)
                    raise ServingDenied("命中忌口或容器不可发放，不得提供", evaluation)
                if decision == DECISION_MANUAL and not confirm_manual:
                    raise ManualConfirmationRequired("该茶饮对参与者存在慎用提示，需人工确认",
                                                     evaluation)
                if decision == DECISION_MANUAL and not confirmation_note:
                    raise ValidationError("人工确认发放必须填写 confirmation_note")
                preps = self._container_preparations(connection, container_id)
                primary = preps[0]
                reasons = evaluation["reasons"]
                if decision == DECISION_MANUAL:
                    reasons = reasons + [{
                        "level": LEVEL_CAUTION, "type": "manual_confirmation",
                        "source": {"confirmed_by": actor_id, "confirmation_note": confirmation_note},
                        "message": f"操作者已人工确认：{confirmation_note}",
                    }]
                dispense_id = uuid.uuid4().hex
                self._movement(connection, container_id=container_id, direction="dispense",
                               signed_amount=-amount, reference_type="dispense",
                               reference_id=dispense_id,
                               memo=f"发放给 {participant_id}"
                                    + ("（人工确认）" if decision == DECISION_MANUAL else ""),
                               created_by=actor_id)
                self._refresh_disposition(connection, container_id)
                connection.execute(
                    "INSERT INTO tea_dispenses(dispense_id,request_id,participant_id,container_id,"
                    "preparation_id,formula_id,amount,unit,restrictions_json,decision,reasons_json,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dispense_id, request_id, participant_id, container_id,
                     primary["preparation_id"], primary["formula_id"], amount,
                     primary["unit"], canonical_json(evaluation["restrictions"]), decision,
                     canonical_json(reasons), actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="tea.dispense.recorded",
                            resource_type="dispense", resource_id=dispense_id,
                            detail={"participant_id": participant_id, "container_id": container_id,
                                    "amount": amount, "decision": decision,
                                    "reasons": reasons})
                return "dispense", dispense_id, {"dispense_id": dispense_id, "decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_dispense", payload=payload, create=create)

    def get_dispense(self, dispense_id: str) -> DispenseRecord:
        row = self.database.connection.execute("SELECT * FROM tea_dispenses WHERE dispense_id=?",
                                               (dispense_id,)).fetchone()
        if row is None:
            raise NotFoundError("发放记录不存在")
        return self._dispense_from_row(row)

    def list_dispenses(self, *, participant_id: str | None = None,
                       container_id: str | None = None) -> list[DispenseRecord]:
        query = "SELECT * FROM tea_dispenses WHERE 1=1"
        parameters: list[Any] = []
        if participant_id:
            query += " AND participant_id=?"
            parameters.append(participant_id)
        if container_id:
            query += " AND container_id=?"
            parameters.append(container_id)
        query += " ORDER BY rowid"
        return [self._dispense_from_row(row)
                for row in self.database.connection.execute(query, parameters)]

    @staticmethod
    def _dispense_from_row(row) -> DispenseRecord:
        return DispenseRecord(
            row["dispense_id"], row["request_id"], row["participant_id"], row["container_id"],
            row["preparation_id"], row["formula_id"], row["amount"], row["unit"],
            tuple(json.loads(row["restrictions_json"])), row["decision"],
            tuple(json.loads(row["reasons_json"])), row["created_by"], row["created_at"],
        )

    # ------------------------------------------------------------------
    # 容器查询、谱系与库存复算
    # ------------------------------------------------------------------

    def get_container(self, container_id: str) -> Container:
        return self._container_from_row(self._container_row(self.database.connection, container_id))

    @staticmethod
    def _container_from_row(row) -> Container:
        return Container(row["container_id"], row["kind"], row["unit"], row["site_id"],
                         row["amount"], row["status"], row["disposition"],
                         row["created_in"], row["created_by"], row["created_at"])

    def list_containers(self, *, site_id: str | None = None,
                        include_off_site: bool = True) -> list[Container]:
        query = "SELECT * FROM tea_containers"
        parameters: list[Any] = []
        clauses = []
        if site_id:
            clauses.append("site_id=?")
            parameters.append(site_id)
        if not include_off_site:
            clauses.append("disposition='on_site'")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY rowid"
        return [self._container_from_row(row)
                for row in self.database.connection.execute(query, parameters)]

    def list_movements(self, container_id: str) -> list[Movement]:
        if self.database.connection.execute("SELECT 1 FROM tea_containers WHERE container_id=?",
                                            (container_id,)).fetchone() is None:
            raise NotFoundError("容器不存在")
        rows = self.database.connection.execute(
            "SELECT * FROM tea_movements WHERE container_id=? ORDER BY seq", (container_id,)
        ).fetchall()
        return [Movement(row["movement_id"], row["seq"], row["container_id"], row["direction"],
                         row["amount_abs"], row["counterparty_container_id"],
                         row["reference_type"], row["reference_id"], row["memo"],
                         row["created_by"], row["created_at"]) for row in rows]

    def recompute_balance(self, container_id: str) -> Balance:
        """由只追加流水复算容器余额，与缓存余额比对，供自动化测试核对守恒。"""

        row = self._container_row(self.database.connection, container_id)
        result = self.database.connection.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(signed_amount),0) AS total "
            "FROM tea_movements WHERE container_id=?",
            (container_id,),
        ).fetchone()
        # 整体转移记一负一正两条流水（净额为 0，数量不随归属变化）；
        # 部分转移只在来源容器记负数、承接容器记正数。
        computed = result["total"]
        matches = abs(computed - row["amount"]) <= EPSILON
        on_site = computed if row["disposition"] == "on_site" else 0.0
        return Balance(container_id, computed, row["amount"], matches, on_site, result["count"])

    def recompute_inventory(self, site_id: str | None = None) -> dict[str, Any]:
        """复算容器余额并核对全局数量守恒。

        逐容器：由流水复算的余额必须等于缓存余额。
        全局恒等式（拆分、合并、跨摊转移在容器间成对抵消）：

            全部容器当前余额之和
            == 全部制备产出之和 − 全部报损之和 − 全部实际发放之和

        跨摊转移会改变容器归属，因此恒等式只在全局（不限定单一场所）成立；
        指定 site_id 时仅核对该场所各容器的逐笔余额。
        """

        connection = self.database.connection
        query = "SELECT container_id FROM tea_containers"
        parameters: list[Any] = []
        if site_id:
            query += " WHERE site_id=?"
            parameters.append(site_id)
        container_ids = [row["container_id"] for row in connection.execute(query, parameters)]
        items: list[dict[str, Any]] = []
        all_match = True
        for cid in container_ids:
            balance = self.recompute_balance(cid)
            dispensed = connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM tea_dispenses "
                "WHERE container_id=? AND decision IN ('served','manual_confirm')",
                (cid,),
            ).fetchone()["total"]
            losses = connection.execute(
                "SELECT COALESCE(SUM(amount_abs),0) AS total FROM tea_movements "
                "WHERE container_id=? AND direction='loss'",
                (cid,),
            ).fetchone()["total"]
            items.append({"container_id": cid,
                          "computed_amount": balance.computed_amount,
                          "stored_amount": balance.stored_amount,
                          "matches": balance.matches,
                          "dispensed_total": dispensed,
                          "loss_total": losses})
            all_match = all_match and balance.matches

        stock = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM tea_containers"
        ).fetchone()["total"]
        produced = connection.execute(
            "SELECT COALESCE(SUM(output_amount),0) AS total FROM tea_preparations"
        ).fetchone()["total"]
        lost = connection.execute(
            "SELECT COALESCE(SUM(amount_abs),0) AS total FROM tea_movements WHERE direction='loss'"
        ).fetchone()["total"]
        dispensed_total = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM tea_dispenses "
            "WHERE decision IN ('served','manual_confirm')"
        ).fetchone()["total"]
        expected_stock = produced - lost - dispensed_total
        identity_holds = abs(stock - expected_stock) <= EPSILON
        return {"scope": site_id or "global",
                "conserved": all_match and identity_holds,
                "identity_holds": identity_holds,
                "stock_total": stock,
                "produced_total": produced,
                "loss_total": lost,
                "dispensed_total": dispensed_total,
                "expected_stock_total": expected_stock,
                "containers": items}

    def _container_preparations(self, connection, container_id: str) -> list[Any]:
        """沿谱系边递归收集容器可追溯到的全部制备（含拆分/合并来源）。"""

        rows = connection.execute(
            "WITH RECURSIVE lineage(cid) AS ("
            "  SELECT ? "
            "  UNION ALL "
            "  SELECT e.parent_container_id FROM tea_edges e JOIN lineage l ON e.child_container_id = l.cid"
            ") "
            "SELECT p.* FROM tea_preparations p JOIN lineage l ON p.output_container_id = l.cid "
            "ORDER BY p.rowid",
            (container_id,),
        ).fetchall()
        return rows

    def container_lineage(self, container_id: str) -> dict[str, Any]:
        """回答一壶茶用了哪批原料、按哪版配方制作、经过哪些拆分合并。"""

        connection = self.database.connection
        if connection.execute("SELECT 1 FROM tea_containers WHERE container_id=?",
                              (container_id,)).fetchone() is None:
            raise NotFoundError("容器不存在")
        preps = self._container_preparations(connection, container_id)
        batches: dict[str, dict[str, Any]] = {}
        formulas: dict[str, dict[str, Any]] = {}
        preparation_ids: list[str] = []
        for prep in preps:
            preparation_ids.append(prep["preparation_id"])
            snapshot = json.loads(prep["formula_snapshot_json"])
            formulas[prep["formula_id"]] = {
                "formula_id": prep["formula_id"],
                "version": prep["formula_version"],
                "name": snapshot.get("name"),
                "ingredients_hash": snapshot.get("ingredients_hash"),
            }
            for item in json.loads(prep["inputs_json"]):
                batches[item["batch_id"]] = {"batch_id": item["batch_id"],
                                             "material_id": item["material_id"],
                                             "batch_name": item.get("batch_name")}
        edges = [self._edge_from_row(row) for row in connection.execute(
            "WITH RECURSIVE lineage(cid) AS ("
            "  SELECT ? "
            "  UNION ALL "
            "  SELECT e.parent_container_id FROM tea_edges e JOIN lineage l ON e.child_container_id = l.cid"
            ") SELECT e.* FROM tea_edges e WHERE e.child_container_id IN (SELECT cid FROM lineage) "
            "ORDER BY e.rowid",
            (container_id,))]
        return {"container_id": container_id,
                "preparations": preparation_ids,
                "formulas": list(formulas.values()),
                "material_batches": list(batches.values()),
                "edges": [edge.__dict__ for edge in edges]}

    @staticmethod
    def _edge_from_row(row) -> LineageEdge:
        return LineageEdge(row["edge_id"], row["parent_container_id"], row["child_container_id"],
                           row["amount"], row["relation"], row["reference_type"],
                           row["reference_id"], row["created_at"])

    def preparation_lineage(self, preparation_id: str) -> dict[str, Any]:
        prep = self.get_preparation(preparation_id)
        return {"preparation_id": preparation_id, "formula_id": prep.formula_id,
                "formula_version": prep.formula_version,
                "formula_snapshot": prep.formula_snapshot,
                "material_batches": [{"batch_id": item["batch_id"], "material_id": item["material_id"],
                                      "amount": item["amount"]} for item in prep.inputs],
                "output_container_id": prep.output_container_id}

    # ------------------------------------------------------------------
    # 冻结与召回
    # ------------------------------------------------------------------

    def freeze_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                     reason: str) -> WriteReceipt:
        """发现原料问题：冻结仍在场的关联容器与未来发放，已发生记录原样保留。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            batch_id = self._id(batch_id, "batch_id")
            reason = self.base._text(reason, "reason", 500)
            batch = connection.execute("SELECT * FROM tea_material_batches WHERE batch_id=?",
                                       (batch_id,)).fetchone()
            if batch is None:
                raise NotFoundError("原料批号不存在")

            def create():
                if batch["status"] == "frozen":
                    raise ConflictError(f"原料批号 {batch_id} 已冻结，冻结动作不可覆盖")
                connection.execute(
                    "UPDATE tea_material_batches SET status='frozen', frozen_reason=? WHERE batch_id=?",
                    (reason, batch_id),
                )
                coverage = self._freeze_descendants(connection, batch_id=batch_id, reason=reason,
                                                    actor_id=actor_id)
                connection.execute(
                    "INSERT INTO tea_freezes(freeze_id,scope_type,scope_id,reason,"
                    "affected_container_ids_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "batch", batch_id, reason,
                     canonical_json(coverage["on_site_containers"]), actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="tea.batch.frozen",
                            resource_type="material_batch", resource_id=batch_id,
                            detail={"reason": reason, **coverage})
                return "material_batch", batch_id, {"batch_id": batch_id, **coverage}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_freeze_batch", payload=payload, create=create)

    def freeze_container(self, *, request_id: str, actor_id: str, container_id: str,
                         reason: str) -> WriteReceipt:
        """直接冻结单个仍在场的容器（及其下游），保留历史记录。"""

        payload = {"actor_id": actor_id, "container_id": container_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            container_id = self._id(container_id, "container_id")
            reason = self.base._text(reason, "reason", 500)
            if connection.execute("SELECT 1 FROM tea_containers WHERE container_id=?",
                                  (container_id,)).fetchone() is None:
                raise NotFoundError("容器不存在")

            def create():
                if connection.execute(
                    "SELECT 1 FROM tea_freezes WHERE scope_type='container' AND scope_id=?",
                    (container_id,),
                ).fetchone() is not None:
                    raise ConflictError(f"容器 {container_id} 已有冻结记录，冻结动作不可覆盖")
                coverage = self._freeze_descendants(connection, root_container=container_id,
                                                    reason=reason, actor_id=actor_id)
                connection.execute(
                    "INSERT INTO tea_freezes(freeze_id,scope_type,scope_id,reason,"
                    "affected_container_ids_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "container", container_id, reason,
                     canonical_json(coverage["on_site_containers"]), actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="tea.container.frozen",
                            resource_type="container", resource_id=container_id,
                            detail={"reason": reason, **coverage})
                return "container", container_id, {"container_id": container_id, **coverage}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_freeze_container", payload=payload, create=create)

    def _freeze_descendants(self, connection, *, reason: str, actor_id: str,
                            batch_id: str | None = None,
                            root_container: str | None = None) -> dict[str, Any]:
        """沿制备 → 拆分/合并 正向传播冻结，返回召回覆盖范围。"""

        if batch_id is not None:
            # canonical_json 无空格且键序固定，但这里统一在 Python 侧精确判定，
            # 避免对 JSON 文本格式做任何假设。
            roots = [row["output_container_id"] for row in connection.execute(
                "SELECT output_container_id, inputs_json FROM tea_preparations"
            ).fetchall() if self._prep_uses_batch_json(row["inputs_json"], batch_id)]
        else:
            roots = [root_container]
        descendants = self._downstream_containers(connection, roots)
        on_site: list[str] = []
        already_off_site: list[str] = []
        for cid in descendants:
            row = connection.execute(
                "SELECT status, disposition FROM tea_containers WHERE container_id=?", (cid,)
            ).fetchone()
            if row["disposition"] == "on_site" and row["status"] != "frozen":
                connection.execute(
                    "UPDATE tea_containers SET status='frozen' WHERE container_id=?", (cid,))
                connection.execute(
                    "UPDATE tea_preparations SET status='frozen' WHERE output_container_id=?", (cid,))
            if row["disposition"] == "on_site":
                on_site.append(cid)
            else:
                already_off_site.append(cid)
        # 召回覆盖：所有由这些容器发出的历史发放（事实保留，仅用于通知/追溯）。
        dispensed = [dict(row) for row in connection.execute(
            "SELECT dispense_id, participant_id, container_id, amount, decision, created_at "
            "FROM tea_dispenses WHERE container_id IN ({}) ORDER BY rowid".format(
                ",".join("?" for _ in descendants) or "SELECT '' WHERE 0"),
            descendants,
        ).fetchall()] if descendants else []
        return {"root_containers": roots, "on_site_containers": on_site,
                "off_site_containers": already_off_site,
                "affected_dispenses": dispensed}

    @staticmethod
    def _prep_uses_batch_json(inputs_json: str, batch_id: str) -> bool:
        return any(item.get("batch_id") == batch_id for item in json.loads(inputs_json))

    @classmethod
    def _prep_uses_batch(cls, connection, container_id: str, batch_id: str) -> bool:
        row = connection.execute(
            "SELECT inputs_json FROM tea_preparations WHERE output_container_id=?", (container_id,)
        ).fetchone()
        if row is None:
            return False
        return cls._prep_uses_batch_json(row["inputs_json"], batch_id)

    @staticmethod
    def _downstream_containers(connection, roots: list[str]) -> list[str]:
        if not roots:
            return []
        seed = " UNION ALL ".join("SELECT ?" for _ in roots)
        rows = connection.execute(
            "WITH RECURSIVE downstream(cid) AS ("
            f"  {seed} "
            "  UNION "
            "  SELECT e.child_container_id FROM tea_edges e "
            "  JOIN downstream d ON e.parent_container_id = d.cid"
            ") SELECT cid FROM downstream",
            roots,
        ).fetchall()
        return [row["cid"] for row in rows]

    def recall_coverage(self, batch_id: str) -> dict[str, Any]:
        """接口直接给出某问题批号的风险来源与召回覆盖范围。"""

        connection = self.database.connection
        batch = connection.execute("SELECT * FROM tea_material_batches WHERE batch_id=?",
                                   (batch_id,)).fetchone()
        if batch is None:
            raise NotFoundError("原料批号不存在")
        roots = [row["output_container_id"] for row in connection.execute(
            "SELECT output_container_id, inputs_json FROM tea_preparations",
        ).fetchall() if self._prep_uses_batch(connection, row["output_container_id"], batch_id)]
        descendants = self._downstream_containers(connection, roots)
        containers = [self._container_from_row(connection.execute(
            "SELECT * FROM tea_containers WHERE container_id=?", (cid,)).fetchone()).__dict__
            for cid in descendants]
        dispensed = [dict(row) for row in connection.execute(
            "SELECT dispense_id, participant_id, container_id, amount, decision, created_at "
            "FROM tea_dispenses WHERE container_id IN ({}) ORDER BY rowid".format(
                ",".join("?" for _ in descendants) or "SELECT '' WHERE 0"),
            descendants,
        ).fetchall()] if descendants else []
        preparations = [{"preparation_id": row["preparation_id"],
                         "formula_id": row["formula_id"], "formula_version": row["formula_version"],
                         "output_container_id": row["output_container_id"],
                         "status": row["status"]}
                        for row in connection.execute(
                            "SELECT * FROM tea_preparations").fetchall()
                        if self._prep_uses_batch(connection, row["output_container_id"], batch_id)]
        return {"batch_id": batch_id, "batch_status": batch["status"],
                "risk_source": {"type": "material_batch", "batch_id": batch_id,
                                "name": batch["name"]},
                "preparations": preparations,
                "on_site_frozen_containers": [c["container_id"] for c in containers
                                              if c["status"] == "frozen"],
                "containers": containers,
                "dispense_records": dispensed,
                "affected_participant_ids": sorted({item["participant_id"] for item in dispensed})}

    # ------------------------------------------------------------------
    # 跨摊位转移：双方确认后整体生效
    # ------------------------------------------------------------------

    def propose_transfer(self, *, request_id: str, actor_id: str, transfer_id: str,
                         from_site_id: str, to_site_id: str,
                         items: list[dict[str, Any]]) -> WriteReceipt:
        """转出方发起跨摊转移提议；此时数量与归属均不变。"""

        payload = {"actor_id": actor_id, "transfer_id": transfer_id,
                   "from_site_id": from_site_id, "to_site_id": to_site_id, "items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            transfer_id = self._id(transfer_id, "transfer_id")
            from_site = self._site(connection, from_site_id)
            to_site = self._site(connection, to_site_id)
            if from_site_id == to_site_id:
                raise ValidationError("转入与转出摊位不能相同")
            if actor.organization_id != from_site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("只能从本组织摊位发起转出")

            def create():
                # 容器状态校验放在幂等命中之后：转移确认后来源容器归属已变化，
                # 合法重放必须稳定返回原提议回执。
                normalized_items = self._normalize_transfer_items(connection, items, from_site_id)
                try:
                    connection.execute(
                        "INSERT INTO tea_transfers(transfer_id,request_id,from_site_id,to_site_id,"
                        "items_json,status,proposed_by,created_at) VALUES(?,?,?,?,?,'proposed',?,?)",
                        (transfer_id, request_id, from_site_id, to_site_id,
                         canonical_json(normalized_items), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("转移单编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="tea.transfer.proposed",
                            resource_type="transfer", resource_id=transfer_id,
                            detail={"from_site_id": from_site_id, "to_site_id": to_site_id,
                                    "items": normalized_items})
                return "transfer", transfer_id, {"transfer_id": transfer_id, "status": "proposed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_propose_transfer", payload=payload, create=create)

    def _normalize_transfer_items(self, connection, items: Any, from_site_id: str) -> list[dict[str, Any]]:
        if not isinstance(items, list) or not items:
            raise ValidationError("items 必须是非空列表")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValidationError("每个转移项必须是对象")
            cid = str(item.get("container_id", "")).strip()
            if not IDENTIFIER.fullmatch(cid):
                raise ValidationError("items.container_id 格式无效")
            if cid in seen:
                raise ValidationError(f"容器 {cid} 在转移单中重复")
            seen.add(cid)
            row = connection.execute("SELECT * FROM tea_containers WHERE container_id=?",
                                     (cid,)).fetchone()
            if row is None:
                raise NotFoundError(f"容器 {cid} 不存在")
            if row["site_id"] != from_site_id:
                raise ValidationError(f"容器 {cid} 不属于转出摊位")
            if row["disposition"] != "on_site":
                raise ConflictError(f"容器 {cid} 已不在场内，不能转移")
            if row["status"] == "frozen":
                raise FrozenError(f"容器 {cid} 已冻结，不能转移")
            amount = self._amount(item.get("amount", row["amount"]), "items.amount")
            if amount > row["amount"] + EPSILON:
                raise InventoryError(f"容器 {cid} 余额不足以转移 {amount}")
            normalized.append({"container_id": cid, "amount": amount, "unit": row["unit"]})
        return normalized

    def confirm_transfer(self, *, request_id: str, actor_id: str, transfer_id: str) -> WriteReceipt:
        """转入方确认；确认瞬间所有容器的数量与归属在一个事务内整体生效。"""

        payload = {"actor_id": actor_id, "transfer_id": transfer_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            transfer_id = self._id(transfer_id, "transfer_id")
            row = connection.execute("SELECT * FROM tea_transfers WHERE transfer_id=?",
                                     (transfer_id,)).fetchone()
            if row is None:
                raise NotFoundError("转移单不存在")
            to_site = self._site(connection, row["to_site_id"])
            if actor.organization_id != to_site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("只有转入摊位所在组织可以确认接收")

            def create():
                # 状态校验放在幂等命中之后：确认成功后重放时状态已是 confirmed。
                if row["status"] != "proposed":
                    raise TransferStateError(f"转移单当前状态为 {row['status']}，不能确认")
                if actor_id == row["proposed_by"]:
                    raise PermissionDenied("跨摊转移必须由接收方的另一位负责人确认，不能发起人自行确认")
                items = json.loads(row["items_json"])
                # 确认时重新校验：提议后数量可能因发放/报损变化。
                for item in items:
                    container = connection.execute(
                        "SELECT * FROM tea_containers WHERE container_id=?",
                        (item["container_id"],),
                    ).fetchone()
                    if container["status"] == "frozen":
                        raise FrozenError(f"容器 {item['container_id']} 已冻结，转移不能生效")
                    if container["disposition"] != "on_site" or container["site_id"] != row["from_site_id"]:
                        raise TransferStateError("来源容器状态已变化，转移不能生效")
                    if item["amount"] > container["amount"] + EPSILON:
                        raise InventoryError(
                            f"容器 {item['container_id']} 现有余额已少于转移数量，转移不能生效")
                for item in items:
                    cid = item["container_id"]
                    if abs(item["amount"] - connection.execute(
                            "SELECT amount FROM tea_containers WHERE container_id=?",
                            (cid,)).fetchone()["amount"]) <= EPSILON:
                        # 整壶转移：容器整体归属改变，数量守恒。
                        connection.execute(
                            "UPDATE tea_containers SET site_id=?, disposition='on_site' WHERE container_id=?",
                            (row["to_site_id"], cid))
                        self._movement(connection, container_id=cid, direction="transfer_out",
                                       signed_amount=-item["amount"],
                                       reference_type="transfer", reference_id=transfer_id,
                                       memo=f"转出至 {row['to_site_id']}（整体转移）",
                                       created_by=actor_id)
                        self._movement(connection, container_id=cid, direction="transfer_in",
                                       signed_amount=item["amount"],
                                       reference_type="transfer", reference_id=transfer_id,
                                       memo=f"由 {row['from_site_id']} 接收入场",
                                       created_by=actor_id)
                    else:
                        # 部分转移：在转入摊位生成承接容器并登记谱系边。
                        new_cid = f"{cid}->recv-{uuid.uuid4().hex[:8]}"
                        connection.execute(
                            "INSERT INTO tea_containers(container_id,kind,unit,site_id,amount,status,"
                            "disposition,created_in,created_by,created_at) "
                            "VALUES(?, 'vessel', ?, ?, 0, 'active', 'on_site', NULL, ?, ?)",
                            (new_cid, item["unit"], row["to_site_id"],
                             actor_id, self._now()),
                        )
                        self._movement(connection, container_id=cid, direction="transfer_out",
                                       signed_amount=-item["amount"],
                                       counterparty_container_id=new_cid,
                                       reference_type="transfer", reference_id=transfer_id,
                                       memo=f"部分转出至 {row['to_site_id']}", created_by=actor_id)
                        self._movement(connection, container_id=new_cid, direction="transfer_in",
                                       signed_amount=item["amount"], counterparty_container_id=cid,
                                       reference_type="transfer", reference_id=transfer_id,
                                       memo=f"由 {row['from_site_id']} 接收", created_by=actor_id)
                        self._edge(connection, parent_id=cid, child_id=new_cid,
                                   amount=item["amount"], relation="split",
                                   reference_type="transfer", reference_id=transfer_id)
                        self._refresh_disposition(connection, cid)
                connection.execute(
                    "UPDATE tea_transfers SET status='confirmed', confirmed_by=?, confirmed_at=? "
                    "WHERE transfer_id=?",
                    (actor_id, self._now(), transfer_id),
                )
                self._audit(connection, actor_id=actor_id, action="tea.transfer.confirmed",
                            resource_type="transfer", resource_id=transfer_id,
                            detail={"confirmed_by": actor_id, "to_site_id": row["to_site_id"],
                                    "items": items})
                return "transfer", transfer_id, {"transfer_id": transfer_id, "status": "confirmed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_confirm_transfer", payload=payload, create=create)

    def reject_transfer(self, *, request_id: str, actor_id: str, transfer_id: str,
                        reason: str) -> WriteReceipt:
        """转入方拒绝接收；不发生任何数量变化。"""

        payload = {"actor_id": actor_id, "transfer_id": transfer_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            transfer_id = self._id(transfer_id, "transfer_id")
            reason = self.base._text(reason, "reason", 500)
            row = connection.execute("SELECT * FROM tea_transfers WHERE transfer_id=?",
                                     (transfer_id,)).fetchone()
            if row is None:
                raise NotFoundError("转移单不存在")
            to_site = self._site(connection, row["to_site_id"])
            if actor.organization_id != to_site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("只有转入摊位所在组织可以拒绝")

            def create():
                # 状态校验放在幂等命中之后：拒绝成功后重放时状态已是 rejected。
                if row["status"] != "proposed":
                    raise TransferStateError(f"转移单当前状态为 {row['status']}，不能拒绝")
                if actor_id == row["proposed_by"]:
                    raise PermissionDenied("跨摊转移必须由接收方的另一位负责人拒绝，不能发起人自行拒绝")
                connection.execute(
                    "UPDATE tea_transfers SET status='rejected', confirmed_by=?, confirmed_at=? "
                    "WHERE transfer_id=?",
                    (actor_id, self._now(), transfer_id),
                )
                self._audit(connection, actor_id=actor_id, action="tea.transfer.rejected",
                            resource_type="transfer", resource_id=transfer_id,
                            detail={"reason": reason, "rejected_by": actor_id})
                return "transfer", transfer_id, {"transfer_id": transfer_id, "status": "rejected"}

            return self._idempotent(connection, request_id=request_id,
                                    action="tea_reject_transfer", payload=payload, create=create)

    def get_transfer(self, transfer_id: str) -> Transfer:
        row = self.database.connection.execute("SELECT * FROM tea_transfers WHERE transfer_id=?",
                                               (transfer_id,)).fetchone()
        if row is None:
            raise NotFoundError("转移单不存在")
        return Transfer(row["transfer_id"], row["request_id"], row["from_site_id"],
                        row["to_site_id"], tuple(json.loads(row["items_json"])), row["status"],
                        row["proposed_by"], row["confirmed_by"], row["created_at"],
                        row["confirmed_at"])

    def get_participant(self, participant_id: str) -> ParticipantProfile:
        row = self.database.connection.execute(
            "SELECT * FROM tea_participants WHERE participant_id=?", (participant_id,)).fetchone()
        if row is None:
            raise NotFoundError("参与者资料不存在")
        return ParticipantProfile(row["participant_id"],
                                  tuple(json.loads(row["restrictions_json"])),
                                  row["note"], row["updated_by"], row["updated_at"])
