"""制备谱系模块在边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MaterialBatch:
    """原料批号：一批进入夜市、可被多版配方引用的原料。"""

    batch_id: str
    name: str
    initial_amount: float
    unit: str
    site_id: str
    status: str  # active | frozen
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Formula:
    """配方版本：配方内容一旦发布即不可覆盖。"""

    formula_id: str
    name: str
    version: int
    ingredients: tuple[dict[str, Any], ...]
    contraindications: tuple[str, ...]
    cautions: tuple[str, ...]
    status: str  # candidate | approved | rejected
    supersedes: str | None
    created_by: str
    reviewed_by: str | None
    risk_note: str | None
    created_at: str
    reviewed_at: str | None


@dataclass(frozen=True)
class Preparation:
    """一次制备：快照所引用的原料批号、配方版本与产出。"""

    preparation_id: str
    formula_id: str
    formula_version: int
    formula_snapshot: dict[str, Any]
    site_id: str
    output_container_id: str
    output_amount: float
    unit: str
    inputs: tuple[dict[str, Any], ...]
    status: str  # normal | frozen
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Container:
    """在场容器：持有一段可追溯的茶饮数量。"""

    container_id: str
    kind: str  # pot | vessel
    unit: str
    site_id: str
    amount: float
    status: str  # active | frozen
    disposition: str  # on_site | transferred_out | depleted | discarded
    created_in: str | None
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Movement:
    """只追加的数量流水，正负号守恒。"""

    movement_id: str
    seq: int
    container_id: str
    direction: str  # produce | split | merge | loss | dispense | transfer_out | transfer_in
    amount: float
    counterparty_container_id: str | None
    reference_type: str | None
    reference_id: str | None
    memo: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class LineageEdge:
    """容器之间的谱系边：拆分与合并的物料来源。"""

    edge_id: str
    parent_container_id: str
    child_container_id: str
    amount: float
    relation: str  # split | merge
    reference_type: str
    reference_id: str
    created_at: str


@dataclass(frozen=True)
class DispenseRecord:
    """发放事实：一经发生即不可变，配方修订不影响它。"""

    dispense_id: str
    request_id: str
    participant_id: str
    container_id: str
    preparation_id: str | None
    formula_id: str | None
    amount: float
    unit: str
    restrictions: tuple[str, ...]
    decision: str  # served | manual_confirm | denied
    reasons: tuple[dict[str, Any], ...]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Transfer:
    """跨摊转移：双方确认后整体生效。"""

    transfer_id: str
    request_id: str
    from_site_id: str
    to_site_id: str
    items: tuple[dict[str, Any], ...]
    status: str  # proposed | confirmed | rejected | cancelled
    proposed_by: str
    confirmed_by: str | None
    created_at: str
    confirmed_at: str | None


@dataclass(frozen=True)
class ParticipantProfile:
    """参与者主动提供的忌口资料。"""

    participant_id: str
    restrictions: tuple[str, ...]
    note: str
    updated_by: str
    updated_at: str


@dataclass(frozen=True)
class Balance:
    """某容器由流水复算得到的余额及校验信息。"""

    container_id: str
    computed_amount: float
    stored_amount: float
    matches: bool
    on_site_amount: float
    movements: int = 0
