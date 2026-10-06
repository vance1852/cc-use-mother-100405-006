"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- 种子批次（品种 + 繁殖批次，批次撤回会形成后继裁定）
CREATE TABLE IF NOT EXISTS seed_lots (
    lot_id TEXT PRIMARY KEY,
    cultivar_id TEXT NOT NULL,
    cultivar_name TEXT NOT NULL,
    lot_code TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    status TEXT NOT NULL CHECK(status IN ('active','withdrawn')),
    withdrawn_at TEXT,
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(cultivar_id, lot_code)
);
-- 试验地块及其立地条件
CREATE TABLE IF NOT EXISTS plots (
    plot_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    region_id TEXT NOT NULL,
    name TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plots_region ON plots(region_id);
-- 试验方案：品种 × 季节，规定允许的农事/观测时间窗
CREATE TABLE IF NOT EXISTS trial_protocols (
    protocol_id TEXT PRIMARY KEY,
    cultivar_id TEXT NOT NULL,
    season TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(cultivar_id, season, version)
);
-- 地块对方案的承接（一地块一方案一茬）
CREATE TABLE IF NOT EXISTS protocol_plots (
    link_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL REFERENCES trial_protocols(protocol_id),
    plot_id TEXT NOT NULL REFERENCES plots(plot_id),
    lot_id TEXT REFERENCES seed_lots(lot_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(protocol_id, plot_id)
);
-- 农事操作（播种、施肥等），可离线补录
CREATE TABLE IF NOT EXISTS farm_operations (
    operation_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL REFERENCES trial_protocols(protocol_id),
    plot_id TEXT NOT NULL REFERENCES plots(plot_id),
    operation_type TEXT NOT NULL,
    event_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    offline INTEGER NOT NULL DEFAULT 0 CHECK(offline IN (0,1)),
    UNIQUE(protocol_id, plot_id, operation_type, event_at)
);
-- 气象区间（连续暴雨等灾害性天气，支持豁免后继裁定）
CREATE TABLE IF NOT EXISTS weather_intervals (
    weather_id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    severity TEXT NOT NULL,
    disaster INTEGER NOT NULL DEFAULT 0 CHECK(disaster IN (0,1)),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(ended_at >= started_at)
);
CREATE INDEX IF NOT EXISTS idx_weather_region_time ON weather_intervals(region_id, started_at, ended_at);
-- 观测设备登记
CREATE TABLE IF NOT EXISTS devices (
    device_sn TEXT PRIMARY KEY,
    device_type TEXT NOT NULL,
    plot_id TEXT REFERENCES plots(plot_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 设备校准区间，仅在 [valid_from, valid_to) 内观测有效
CREATE TABLE IF NOT EXISTS device_calibrations (
    calibration_id TEXT PRIMARY KEY,
    device_sn TEXT NOT NULL REFERENCES devices(device_sn),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX IF NOT EXISTS idx_calibration_device ON device_calibrations(device_sn, valid_from, valid_to);
-- 排除规则（静态/人工规则，命中的观测或地块可被排除）
CREATE TABLE IF NOT EXISTS exclusion_rules (
    rule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    expression TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 原始观测（离线记录按 设备序列+观测时间 归并，重复上报不增加样本）
CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    device_sn TEXT NOT NULL,
    plot_id TEXT NOT NULL REFERENCES plots(plot_id),
    protocol_id TEXT REFERENCES trial_protocols(protocol_id),
    metric TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    reported_by TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    observed_origin TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('valid','pending','excluded')),
    pending_reason TEXT,
    duplicate_of TEXT REFERENCES observations(observation_id),
    exclusion_rule_id TEXT REFERENCES exclusion_rules(rule_id),
    UNIQUE(device_sn, observed_at, metric)
);
CREATE INDEX IF NOT EXISTS idx_observations_plot_status ON observations(plot_id, status);
CREATE INDEX IF NOT EXISTS idx_observations_protocol ON observations(protocol_id);
-- 待审事项（越序/超窗/校准失效/离线越序进入待审而非丢弃）
CREATE TABLE IF NOT EXISTS review_queue (
    review_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES observations(observation_id),
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved','rejected')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_open ON review_queue(status, created_at);
-- 冻结的分析版本：生成时复制纳入/排除快照与有效试验集合
CREATE TABLE IF NOT EXISTS analysis_versions (
    analysis_id TEXT PRIMARY KEY,
    cultivar_id TEXT NOT NULL,
    season TEXT NOT NULL,
    code_version TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    effective_set_hash TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_plot_effectiveness (
    analysis_id TEXT NOT NULL REFERENCES analysis_versions(analysis_id),
    plot_id TEXT NOT NULL REFERENCES plots(plot_id),
    effective INTEGER NOT NULL CHECK(effective IN (0,1)),
    reasons_json TEXT NOT NULL,
    included_observation_ids_json TEXT NOT NULL,
    excluded_observation_ids_json TEXT NOT NULL,
    PRIMARY KEY(analysis_id, plot_id)
);
-- 区域 × 品种裁定（推荐/限制/拒绝），版本化；每个键至多一个 effective
CREATE TABLE IF NOT EXISTS region_rulings (
    ruling_id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL,
    cultivar_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('recommend','restrict','reject')),
    version INTEGER NOT NULL CHECK(version >= 1),
    analysis_id TEXT REFERENCES analysis_versions(analysis_id),
    basis_json TEXT NOT NULL,
    basis_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed','effective','superseded')),
    superseded_by TEXT,
    amendment_kind TEXT,
    amendment_reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    effective_at TEXT,
    UNIQUE(region_id, cultivar_id, version)
);
CREATE INDEX IF NOT EXISTS idx_rulings_effective ON region_rulings(region_id, cultivar_id, status);
-- 硬性不变量：同一 区域×品种 至多一个生效裁定（版本链不被悄悄改写）
CREATE UNIQUE INDEX IF NOT EXISTS idx_rulings_one_effective ON region_rulings(region_id, cultivar_id)
    WHERE status = 'effective';
-- 并发确认：多名审查人独立确认，全部齐备后生效，乐观版本保证唯一生效
CREATE TABLE IF NOT EXISTS ruling_confirms (
    ruling_id TEXT NOT NULL REFERENCES region_rulings(ruling_id),
    reviewer_id TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY(ruling_id, reviewer_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        # 进程内按业务键串行化并发确认（SQLite BEGIN IMMEDIATE 额外保证跨进程串行写）
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()

    def key_lock(self, key: str) -> threading.Lock:
        """返回某个业务键（如 区域:品种）对应的互斥锁。"""

        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
