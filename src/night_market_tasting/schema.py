"""定义试饮批次治理模块在基础库之上追加的表结构。

谱系事实（制备用料、配方版本、领取记录、台账分录）只插入不更新，
数量与状态字段允许随业务动作推进，保证历史不可覆盖。
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredient_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    ingredient_name TEXT NOT NULL,
    lot_number TEXT NOT NULL,
    quantity_received REAL NOT NULL CHECK(quantity_received > 0),
    unit TEXT NOT NULL,
    strict_tags_json TEXT NOT NULL DEFAULT '[]',
    caution_tags_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('active', 'frozen')) DEFAULT 'active',
    freeze_reason TEXT,
    frozen_by TEXT,
    frozen_at TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, ingredient_name, lot_number)
);
CREATE TABLE IF NOT EXISTS recipe_versions (
    recipe_version_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    recipe_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    components_json TEXT NOT NULL,
    strict_tags_json TEXT NOT NULL DEFAULT '[]',
    caution_tags_json TEXT NOT NULL DEFAULT '[]',
    supersedes_version_id TEXT REFERENCES recipe_versions(recipe_version_id),
    status TEXT NOT NULL CHECK(status IN ('pending_review', 'active')) DEFAULT 'pending_review',
    risk_note TEXT,
    reviewed_by TEXT REFERENCES actors(actor_id),
    reviewed_at TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, recipe_name, version)
);
CREATE TABLE IF NOT EXISTS preparations (
    prep_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    recipe_version_id TEXT NOT NULL REFERENCES recipe_versions(recipe_version_id),
    quantity_brewed REAL NOT NULL CHECK(quantity_brewed > 0),
    quantity_remaining REAL NOT NULL CHECK(quantity_remaining >= 0),
    unit TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'frozen', 'depleted')) DEFAULT 'active',
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preparation_ingredients (
    prep_id TEXT NOT NULL REFERENCES preparations(prep_id),
    batch_id TEXT NOT NULL REFERENCES ingredient_batches(batch_id),
    ingredient_name TEXT NOT NULL,
    quantity_used REAL NOT NULL CHECK(quantity_used > 0),
    unit TEXT NOT NULL,
    PRIMARY KEY (prep_id, batch_id)
);
CREATE TABLE IF NOT EXISTS containers (
    container_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    prep_id TEXT NOT NULL REFERENCES preparations(prep_id),
    label TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity >= 0),
    unit TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('on_site', 'in_transfer', 'frozen', 'emptied')) DEFAULT 'on_site',
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_entries (
    entry_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    entry_type TEXT NOT NULL CHECK(entry_type IN
        ('brew', 'fill', 'split', 'merge', 'loss', 'claim', 'transfer_out', 'transfer_in')),
    holder_type TEXT NOT NULL CHECK(holder_type IN ('preparation', 'container')),
    holder_id TEXT NOT NULL,
    delta REAL NOT NULL,
    unit TEXT NOT NULL,
    ref_type TEXT,
    ref_id TEXT,
    note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_entries_holder ON stock_entries(holder_type, holder_id);
CREATE INDEX IF NOT EXISTS idx_stock_entries_site ON stock_entries(site_id);
CREATE TABLE IF NOT EXISTS participant_declarations (
    participant_id TEXT PRIMARY KEY,
    restrictions_json TEXT NOT NULL,
    note TEXT,
    declared_by TEXT NOT NULL REFERENCES actors(actor_id),
    updated_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    holder_type TEXT NOT NULL CHECK(holder_type IN ('preparation', 'container')),
    holder_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    recipe_version_id TEXT NOT NULL REFERENCES recipe_versions(recipe_version_id),
    decision TEXT NOT NULL CHECK(decision IN ('allow', 'review')),
    explanation_json TEXT NOT NULL,
    manual_confirmed INTEGER NOT NULL CHECK(manual_confirmed IN (0, 1)),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_holder ON claims(holder_type, holder_id);
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    from_site_id TEXT NOT NULL REFERENCES sites(site_id),
    to_site_id TEXT NOT NULL REFERENCES sites(site_id),
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'rejected', 'cancelled')) DEFAULT 'pending',
    initiated_by TEXT NOT NULL REFERENCES actors(actor_id),
    resolved_by TEXT REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""
