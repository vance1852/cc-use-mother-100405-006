"""在基础服务数据库上扩展农业试验裁定所需的表结构。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from science_strategy_foundation.storage import Database


AGRI_SCHEMA = """
CREATE TABLE IF NOT EXISTS seed_batches (
    batch_id TEXT PRIMARY KEY,
    variety_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'withdrawn')),
    withdrawal_reason TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE TABLE IF NOT EXISTS plots (
    plot_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    soil_type TEXT NOT NULL,
    area_hectares REAL NOT NULL CHECK(area_hectares > 0),
    conditions_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trial_protocols (
    protocol_id TEXT PRIMARY KEY,
    variety_id TEXT NOT NULL,
    region_id TEXT NOT NULL,
    sowing_window_start TEXT NOT NULL,
    sowing_window_end TEXT NOT NULL,
    observation_window_start TEXT NOT NULL,
    observation_window_end TEXT NOT NULL,
    min_valid_trials INTEGER NOT NULL CHECK(min_valid_trials >= 1),
    confirmations_required INTEGER NOT NULL CHECK(confirmations_required >= 1),
    requirements_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exclusion_rules (
    rule_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL REFERENCES trial_protocols(protocol_id),
    rule_type TEXT NOT NULL CHECK(rule_type IN ('protocol_deviation', 'min_observations', 'metric_range')),
    definition_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trials (
    trial_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL REFERENCES trial_protocols(protocol_id),
    plot_id TEXT NOT NULL REFERENCES plots(plot_id),
    seed_batch_id TEXT NOT NULL REFERENCES seed_batches(batch_id),
    status TEXT NOT NULL CHECK(status IN ('planned', 'active', 'deviated', 'completed', 'excluded')),
    deviation_reason TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS farming_operations (
    operation_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
    op_type TEXT NOT NULL,
    performed_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(trial_id, op_type, performed_at)
);
CREATE TABLE IF NOT EXISTS weather_intervals (
    weather_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('normal', 'disaster')),
    summary TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_calibrations (
    calibration_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    calibrated_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    device_seq INTEGER NOT NULL CHECK(device_seq >= 0),
    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
    observed_at TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted', 'pending', 'rejected')),
    pending_reason TEXT,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    UNIQUE(device_id, device_seq)
);
CREATE TABLE IF NOT EXISTS trial_exemptions (
    exemption_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
    weather_id TEXT NOT NULL REFERENCES weather_intervals(weather_id),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(trial_id, weather_id)
);
CREATE TABLE IF NOT EXISTS analysis_versions (
    version_id TEXT PRIMARY KEY,
    protocol_id TEXT NOT NULL REFERENCES trial_protocols(protocol_id),
    region_id TEXT NOT NULL,
    variety_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    valid_trials_json TEXT NOT NULL,
    excluded_trials_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('frozen')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(protocol_id, version_no)
);
CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL,
    variety_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('recommended', 'limited', 'rejected')),
    analysis_version_id TEXT NOT NULL REFERENCES analysis_versions(version_id),
    rationale TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('confirming', 'effective', 'superseded')),
    supersedes_verdict_id TEXT,
    successor_reason TEXT CHECK(successor_reason IN ('late_correction', 'disaster_exemption',
                                                     'batch_withdrawal', 'analysis_update')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    effective_at TEXT,
    superseded_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS verdicts_single_effective
    ON verdicts(region_id, variety_id) WHERE status = 'effective';
CREATE TABLE IF NOT EXISTS verdict_confirmations (
    verdict_id TEXT NOT NULL REFERENCES verdicts(verdict_id),
    reviewer_id TEXT NOT NULL REFERENCES actors(actor_id),
    request_id TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (verdict_id, reviewer_id)
);
"""


class AgriDatabase(Database):
    """在基础服务库结构上追加农业试验裁定表，并串行化并发事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._transaction_lock = threading.RLock()
        super().__init__(path)
        self.connection.executescript(AGRI_SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在共享连接上串行化事务，保证并发确认只产生一个生效版本。"""

        with self._transaction_lock:
            with super().transaction(immediate=immediate) as connection:
                yield connection
