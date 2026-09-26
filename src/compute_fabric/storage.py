"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor','facilities')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_cny TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES market_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(market_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON market_index_quotes(market_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_gpu_hours TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_gpu_hours TEXT NOT NULL,
    available_gpu_hours TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_gpu_hours TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_gpu_hours TEXT NOT NULL,
    allocated_gpu_hours TEXT NOT NULL DEFAULT '0',
    delivered_gpu_hours TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    loaded_gpu_hours TEXT NOT NULL,
    expected_delivered_gpu_hours TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);

-- 设施约束容量目录：电力、制冷、承重与各类型网络端口的总量/已用/预留。
CREATE TABLE IF NOT EXISTS facility_constraints (
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    constraint_key TEXT NOT NULL,
    unit TEXT NOT NULL,
    capacity TEXT NOT NULL,
    used TEXT NOT NULL DEFAULT '0',
    reserved TEXT NOT NULL DEFAULT '0',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY(facility_id, constraint_key)
);

-- 机柜上架变更：每次修订写新版本行，(change_id, revision) 唯一，串成修订链。
CREATE TABLE IF NOT EXISTS rack_changes (
    change_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    title TEXT NOT NULL,
    bom_version TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','rejected','superseded','approved','in_progress',
                        'failed','rolling_back','manual_takeover','completed','rolled_back','cancelled')),
    window_starts_at TEXT NOT NULL,
    window_ends_at TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    supersedes_revision INTEGER,
    decision_by TEXT REFERENCES supply_users(user_id),
    decision_at TEXT,
    decision_basis TEXT,
    impact_json TEXT NOT NULL,
    impact_built_at TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    fail_reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(change_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_rack_changes_facility
ON rack_changes(facility_id, state);

CREATE INDEX IF NOT EXISTS idx_rack_changes_state
ON rack_changes(facility_id, change_id, revision);

-- 单个修订对各约束维度的需求与批准时的快照余量（用于接口展示与完成时转已用）。
CREATE TABLE IF NOT EXISTS rack_change_demands (
    change_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    constraint_key TEXT NOT NULL,
    unit TEXT NOT NULL,
    required_value TEXT NOT NULL,
    -- 批准时记录的快照值，仅 completed 版本有值。
    snapshot_capacity TEXT,
    snapshot_used TEXT,
    snapshot_reserved TEXT,
    FOREIGN KEY(change_id, revision) REFERENCES rack_changes(change_id, revision) DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY(change_id, revision, constraint_key)
);

-- 单个修订的机柜 U 位需求。
CREATE TABLE IF NOT EXISTS rack_change_locations (
    change_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    rack_id TEXT NOT NULL,
    u_start INTEGER NOT NULL,
    u_size INTEGER NOT NULL,
    FOREIGN KEY(change_id, revision) REFERENCES rack_changes(change_id, revision) DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY(change_id, revision, rack_id, u_start)
);

-- 现场实施步骤：每个步骤独立回执，不允许跳步。
CREATE TABLE IF NOT EXISTS rack_change_steps (
    change_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    name TEXT NOT NULL,
    rollback_action TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','done','skipped','failed','rolled_back','manual')),
    receipt_by TEXT REFERENCES supply_users(user_id),
    receipt_note TEXT,
    completed_at TEXT,
    FOREIGN KEY(change_id, revision) REFERENCES rack_changes(change_id, revision) DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY(change_id, revision, sequence)
);

CREATE INDEX IF NOT EXISTS idx_rack_steps_state
ON rack_change_steps(change_id, revision, state);

-- 资源预留（锁定）台账：批准即按需求锁定，回退完成释放，全部步骤成功后转入已用。
CREATE TABLE IF NOT EXISTS rack_change_reservations (
    change_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    facility_id TEXT NOT NULL,
    constraint_key TEXT NOT NULL,
    reserved_value TEXT NOT NULL,
    released_value TEXT,
    state TEXT NOT NULL DEFAULT 'locked' CHECK(state IN ('locked','released','consumed')),
    locked_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY(change_id, revision, constraint_key),
    FOREIGN KEY(change_id, revision) REFERENCES rack_changes(change_id, revision) DEFERRABLE INITIALLY DEFERRED
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    # 为 0.13 之前创建的数据库补齐约束预留列。
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(facility_constraints)").fetchall()
    }
    if columns and "reserved" not in columns:
        connection.execute("ALTER TABLE facility_constraints ADD COLUMN reserved TEXT NOT NULL DEFAULT '0'")


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
