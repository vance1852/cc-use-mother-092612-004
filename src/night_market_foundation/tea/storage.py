"""制备谱系模块的表结构。

所有数量类表只追加（流水、谱系边、冻结事件），容器表的 amount 是可由流水
复算的缓存；任何写操作都在基础服务的短事务内完成。
"""

from __future__ import annotations

TEA_SCHEMA = """
CREATE TABLE IF NOT EXISTS tea_material_batches (
    batch_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    initial_amount REAL NOT NULL CHECK(initial_amount > 0),
    unit TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','frozen')),
    frozen_reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tea_formulas (
    formula_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    ingredients_json TEXT NOT NULL,
    ingredients_hash TEXT NOT NULL,
    contraindications_json TEXT NOT NULL,
    cautions_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('candidate','approved','rejected')),
    supersedes TEXT REFERENCES tea_formulas(formula_id),
    created_by TEXT NOT NULL,
    reviewed_by TEXT,
    risk_note TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS tea_preparations (
    preparation_id TEXT PRIMARY KEY,
    formula_id TEXT NOT NULL REFERENCES tea_formulas(formula_id),
    formula_version INTEGER NOT NULL,
    formula_snapshot_json TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    output_container_id TEXT NOT NULL,
    output_amount REAL NOT NULL CHECK(output_amount > 0),
    unit TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'normal' CHECK(status IN ('normal','frozen')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tea_containers (
    container_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('pot','vessel')),
    unit TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    amount REAL NOT NULL DEFAULT 0 CHECK(amount >= 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','frozen')),
    disposition TEXT NOT NULL DEFAULT 'on_site'
        CHECK(disposition IN ('on_site','transferred_out','depleted','discarded')),
    created_in TEXT REFERENCES tea_preparations(preparation_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tea_movements (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    movement_id TEXT NOT NULL UNIQUE,
    container_id TEXT NOT NULL REFERENCES tea_containers(container_id),
    direction TEXT NOT NULL CHECK(direction IN
        ('produce','split','merge','loss','dispense','transfer_out','transfer_in')),
    signed_amount REAL NOT NULL CHECK(signed_amount <> 0),
    amount_abs REAL NOT NULL CHECK(amount_abs > 0),
    counterparty_container_id TEXT REFERENCES tea_containers(container_id),
    reference_type TEXT,
    reference_id TEXT,
    memo TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tea_movements_container ON tea_movements(container_id, seq);
CREATE TABLE IF NOT EXISTS tea_edges (
    edge_id TEXT PRIMARY KEY,
    parent_container_id TEXT NOT NULL REFERENCES tea_containers(container_id),
    child_container_id TEXT NOT NULL REFERENCES tea_containers(container_id),
    amount REAL NOT NULL CHECK(amount > 0),
    relation TEXT NOT NULL CHECK(relation IN ('split','merge')),
    reference_type TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(parent_container_id, child_container_id, reference_type, reference_id)
);
CREATE INDEX IF NOT EXISTS idx_tea_edges_parent ON tea_edges(parent_container_id);
CREATE INDEX IF NOT EXISTS idx_tea_edges_child ON tea_edges(child_container_id);
CREATE TABLE IF NOT EXISTS tea_dispenses (
    dispense_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    participant_id TEXT NOT NULL,
    container_id TEXT NOT NULL REFERENCES tea_containers(container_id),
    preparation_id TEXT REFERENCES tea_preparations(preparation_id),
    formula_id TEXT REFERENCES tea_formulas(formula_id),
    amount REAL NOT NULL CHECK(amount > 0),
    unit TEXT NOT NULL,
    restrictions_json TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('served','manual_confirm','denied')),
    reasons_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tea_participants (
    participant_id TEXT PRIMARY KEY,
    restrictions_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tea_transfers (
    transfer_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    from_site_id TEXT NOT NULL REFERENCES sites(site_id),
    to_site_id TEXT NOT NULL REFERENCES sites(site_id),
    items_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','rejected','cancelled')),
    proposed_by TEXT NOT NULL,
    confirmed_by TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    CHECK(to_site_id <> from_site_id)
);
CREATE TABLE IF NOT EXISTS tea_freezes (
    freeze_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('batch','container')),
    scope_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    affected_container_ids_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scope_type, scope_id)
);
"""


def init_tea_tables(database) -> None:
    """在已有基础库连接上幂等建表。"""

    database.connection.executescript(TEA_SCHEMA)
