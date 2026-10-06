"""在基础服务边界上实现多地点农业试验与推广裁定。

证据链覆盖种子批次、地块条件、试验方案、农事操作、气象区间、设备校准和
排除规则；离线观测按设备序列归并，重复上报不增加样本，越序、超窗或校准
失效的记录进入待审而不是丢弃；区域结论引用冻结的分析版本，迟到更正、
灾害性气象豁免和品种批次撤回只能通过后继裁定生效，已发布范围不会被
悄悄改写。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from science_strategy_foundation.audit import append_event, canonical_json, digest
from science_strategy_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from science_strategy_foundation.models import Actor, WriteReceipt
from science_strategy_foundation.service import DomainService

from .models import EvaluationResult


DECISIONS = frozenset({"recommended", "limited", "rejected"})
SUCCESSOR_REASONS = frozenset({
    "late_correction",
    "disaster_exemption",
    "batch_withdrawal",
    "analysis_update",
})
RULE_TYPES = frozenset({"protocol_deviation", "min_observations", "metric_range"})
SEVERITIES = frozenset({"normal", "disaster"})


def _parse_instant(value: Any, field: str) -> datetime:
    text = str(value).strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
    if moment.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return moment.astimezone(timezone.utc)


def _format_instant(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class AgriTrialService(DomainService):
    """在权限、幂等、事务与审计边界上扩展农业试验裁定规则。"""

    # ---------- 基础工具 ----------

    def _instant(self, value: Any, field: str) -> str:
        return _format_instant(_parse_instant(value, field))

    def _window(self, start: Any, end: Any, field: str) -> tuple[str, str]:
        start_moment = _parse_instant(start, f"{field}_start")
        end_moment = _parse_instant(end, f"{field}_end")
        if start_moment > end_moment:
            raise ValidationError(f"{field} 起点不能晚于终点")
        return _format_instant(start_moment), _format_instant(end_moment)

    def _one(self, connection, query: str, parameters: tuple, message: str) -> sqlite3.Row:
        row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise NotFoundError(message)
        return row

    def _positive_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValidationError(f"{field} 必须是正整数")
        return value

    def _number(self, value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} 必须是数值")
        return float(value)

    def _object(self, value: Any, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValidationError(f"{field} 必须是对象")
        return value

    def _site_row(self, connection, site_id: str) -> sqlite3.Row:
        return self._one(connection, "SELECT * FROM sites WHERE site_id=?", (site_id,), "场所不存在")

    def _check_site_scope(self, actor: Actor, site_row: sqlite3.Row) -> None:
        if actor.role != "admin" and actor.organization_id != site_row["organization_id"]:
            raise PermissionDenied("不能写入其他组织的场所")

    def _trial_context(self, connection, trial_id: str):
        trial = self._one(connection, "SELECT * FROM trials WHERE trial_id=?", (trial_id,), "试验不存在")
        plot = self._one(connection, "SELECT * FROM plots WHERE plot_id=?", (trial["plot_id"],), "地块不存在")
        site = self._site_row(connection, plot["site_id"])
        protocol = self._one(connection, "SELECT * FROM trial_protocols WHERE protocol_id=?",
                             (trial["protocol_id"],), "试验方案不存在")
        return trial, plot, site, protocol

    def _rich_idempotent(self, connection, *, request_id: str, action: str,
                         payload: dict[str, Any],
                         create: Callable[[], tuple[str, str, dict[str, Any]]]) -> tuple[WriteReceipt, dict[str, Any]]:
        """沿用基础服务的幂等回执协议，同时把完整响应一并返回（重放时返回存档响应）。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True), json.loads(row["response_json"])
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False), response

    # ---------- 证据链登记 ----------

    def register_seed_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                            variety_id: str, origin: str,
                            metadata: dict[str, Any] | None = None) -> WriteReceipt:
        metadata = self._object(metadata, "metadata")
        payload = {"actor_id": actor_id, "batch_id": batch_id, "variety_id": variety_id,
                   "origin": origin, "metadata": metadata}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch_id = self._identifier(batch_id, "batch_id")
            variety_id = self._identifier(variety_id, "variety_id")
            origin = self._text(origin, "origin")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO seed_batches(batch_id,variety_id,origin,metadata_json,status,created_by,created_at) "
                        "VALUES(?,?,?,?,'active',?,?)",
                        (batch_id, variety_id, origin, canonical_json(metadata), actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("种子批次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="seed_batch.registered",
                             resource_type="seed_batch", resource_id=batch_id,
                             detail={"variety_id": variety_id, "origin": origin}, occurred_at=self._now())
                return "seed_batch", batch_id, {"batch_id": batch_id, "variety_id": variety_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_seed_batch", payload=payload, create=create)

    def withdraw_seed_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                            reason: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            batch_id = self._identifier(batch_id, "batch_id")
            reason = self._text(reason, "reason")
            batch = self._one(connection, "SELECT * FROM seed_batches WHERE batch_id=?",
                              (batch_id,), "种子批次不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if batch["status"] == "withdrawn":
                    raise ConflictError("种子批次已撤回")
                connection.execute(
                    "UPDATE seed_batches SET status='withdrawn', withdrawal_reason=?, withdrawn_at=? WHERE batch_id=?",
                    (reason, self._now(), batch_id),
                )
                affected = [row["verdict_id"] for row in connection.execute(
                    "SELECT v.verdict_id FROM verdicts v "
                    "JOIN analysis_versions a ON v.analysis_version_id=a.version_id "
                    "WHERE v.status='effective' AND a.variety_id=?", (batch["variety_id"],))]
                append_event(connection, actor_id=actor_id, action="seed_batch.withdrawn",
                             resource_type="seed_batch", resource_id=batch_id,
                             detail={"variety_id": batch["variety_id"], "reason": reason,
                                     "successor_required_for": affected}, occurred_at=self._now())
                return "seed_batch", batch_id, {"batch_id": batch_id, "status": "withdrawn",
                                                "successor_required_for": affected}

            return self._rich_idempotent(connection, request_id=request_id,
                                         action="withdraw_seed_batch", payload=payload, create=create)

    def register_plot(self, *, request_id: str, actor_id: str, plot_id: str, site_id: str,
                      name: str, soil_type: str, area_hectares: float,
                      conditions: dict[str, Any] | None = None) -> WriteReceipt:
        conditions = self._object(conditions, "conditions")
        payload = {"actor_id": actor_id, "plot_id": plot_id, "site_id": site_id, "name": name,
                   "soil_type": soil_type, "area_hectares": area_hectares, "conditions": conditions}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            plot_id = self._identifier(plot_id, "plot_id")
            name = self._text(name, "name")
            soil_type = self._text(soil_type, "soil_type", 80)
            area = self._number(area_hectares, "area_hectares")
            if area <= 0:
                raise ValidationError("area_hectares 必须大于 0")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO plots(plot_id,site_id,name,soil_type,area_hectares,conditions_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (plot_id, site_id, name, soil_type, area, canonical_json(conditions),
                         actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("地块编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="plot.registered",
                             resource_type="plot", resource_id=plot_id,
                             detail={"site_id": site_id, "name": name, "soil_type": soil_type,
                                     "area_hectares": area}, occurred_at=self._now())
                return "plot", plot_id, {"plot_id": plot_id, "site_id": site_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_plot", payload=payload, create=create)

    def register_protocol(self, *, request_id: str, actor_id: str, protocol_id: str,
                          variety_id: str, region_id: str,
                          sowing_window_start: str, sowing_window_end: str,
                          observation_window_start: str, observation_window_end: str,
                          min_valid_trials: int = 2, confirmations_required: int = 2,
                          requirements: dict[str, Any] | None = None) -> WriteReceipt:
        requirements = self._object(requirements, "requirements")
        payload = {"actor_id": actor_id, "protocol_id": protocol_id, "variety_id": variety_id,
                   "region_id": region_id, "sowing_window_start": sowing_window_start,
                   "sowing_window_end": sowing_window_end,
                   "observation_window_start": observation_window_start,
                   "observation_window_end": observation_window_end,
                   "min_valid_trials": min_valid_trials,
                   "confirmations_required": confirmations_required,
                   "requirements": requirements}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            protocol_id = self._identifier(protocol_id, "protocol_id")
            variety_id = self._identifier(variety_id, "variety_id")
            region_id = self._identifier(region_id, "region_id")
            sowing_start, sowing_end = self._window(sowing_window_start, sowing_window_end, "sowing_window")
            obs_start, obs_end = self._window(observation_window_start, observation_window_end, "observation_window")
            min_valid = self._positive_int(min_valid_trials, "min_valid_trials")
            quorum = self._positive_int(confirmations_required, "confirmations_required")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO trial_protocols(protocol_id,variety_id,region_id,sowing_window_start,"
                        "sowing_window_end,observation_window_start,observation_window_end,min_valid_trials,"
                        "confirmations_required,requirements_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (protocol_id, variety_id, region_id, sowing_start, sowing_end, obs_start, obs_end,
                         min_valid, quorum, canonical_json(requirements), actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("试验方案编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="protocol.registered",
                             resource_type="trial_protocol", resource_id=protocol_id,
                             detail={"variety_id": variety_id, "region_id": region_id,
                                     "min_valid_trials": min_valid, "confirmations_required": quorum},
                             occurred_at=self._now())
                return "trial_protocol", protocol_id, {"protocol_id": protocol_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_protocol", payload=payload, create=create)

    def register_exclusion_rule(self, *, request_id: str, actor_id: str, rule_id: str,
                                protocol_id: str, rule_type: str,
                                definition: dict[str, Any] | None = None) -> WriteReceipt:
        definition = self._object(definition, "definition")
        payload = {"actor_id": actor_id, "rule_id": rule_id, "protocol_id": protocol_id,
                   "rule_type": rule_type, "definition": definition}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            self._one(connection, "SELECT * FROM trial_protocols WHERE protocol_id=?",
                      (protocol_id,), "试验方案不存在")
            rule_id = self._identifier(rule_id, "rule_id")
            if rule_type not in RULE_TYPES:
                raise ValidationError("rule_type 不在允许范围内")
            if rule_type == "min_observations":
                self._positive_int(definition.get("count"), "definition.count")
            if rule_type == "metric_range":
                self._text(definition.get("metric", ""), "definition.metric", 80)
                low = self._number(definition.get("min"), "definition.min")
                high = self._number(definition.get("max"), "definition.max")
                if low > high:
                    raise ValidationError("definition.min 不能大于 definition.max")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO exclusion_rules(rule_id,protocol_id,rule_type,definition_json,active,created_by,created_at) "
                        "VALUES(?,?,?,?,1,?,?)",
                        (rule_id, protocol_id, rule_type, canonical_json(definition), actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("排除规则编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="exclusion_rule.registered",
                             resource_type="exclusion_rule", resource_id=rule_id,
                             detail={"protocol_id": protocol_id, "rule_type": rule_type,
                                     "definition": definition}, occurred_at=self._now())
                return "exclusion_rule", rule_id, {"rule_id": rule_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_exclusion_rule", payload=payload, create=create)

    def register_device_calibration(self, *, request_id: str, actor_id: str, calibration_id: str,
                                    device_id: str, calibrated_at: str, valid_until: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "calibration_id": calibration_id, "device_id": device_id,
                   "calibrated_at": calibrated_at, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            calibration_id = self._identifier(calibration_id, "calibration_id")
            device_id = self._identifier(device_id, "device_id")
            start, end = self._window(calibrated_at, valid_until, "calibration")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO device_calibrations(calibration_id,device_id,calibrated_at,valid_until,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (calibration_id, device_id, start, end, actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("校准记录编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="device.calibrated",
                             resource_type="device_calibration", resource_id=calibration_id,
                             detail={"device_id": device_id, "calibrated_at": start, "valid_until": end},
                             occurred_at=self._now())
                return "device_calibration", calibration_id, {"calibration_id": calibration_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_device_calibration", payload=payload, create=create)

    def create_trial(self, *, request_id: str, actor_id: str, trial_id: str,
                     protocol_id: str, plot_id: str, seed_batch_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "trial_id": trial_id, "protocol_id": protocol_id,
                   "plot_id": plot_id, "seed_batch_id": seed_batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            protocol = self._one(connection, "SELECT * FROM trial_protocols WHERE protocol_id=?",
                                 (protocol_id,), "试验方案不存在")
            plot = self._one(connection, "SELECT * FROM plots WHERE plot_id=?", (plot_id,), "地块不存在")
            batch = self._one(connection, "SELECT * FROM seed_batches WHERE batch_id=?",
                              (seed_batch_id,), "种子批次不存在")
            site = self._site_row(connection, plot["site_id"])
            self._check_site_scope(actor, site)
            trial_id = self._identifier(trial_id, "trial_id")
            if batch["status"] != "active":
                raise ValidationError("已撤回的批次不能用于新试验")
            if batch["variety_id"] != protocol["variety_id"]:
                raise ValidationError("种子批次品种与试验方案不一致")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO trials(trial_id,protocol_id,plot_id,seed_batch_id,status,created_by,created_at) "
                        "VALUES(?,?,?,?,'planned',?,?)",
                        (trial_id, protocol_id, plot_id, seed_batch_id, actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("试验编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="trial.created",
                             resource_type="trial", resource_id=trial_id,
                             detail={"protocol_id": protocol_id, "plot_id": plot_id,
                                     "seed_batch_id": seed_batch_id}, occurred_at=self._now())
                return "trial", trial_id, {"trial_id": trial_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_trial", payload=payload, create=create)

    def record_farming_operation(self, *, request_id: str, actor_id: str, trial_id: str,
                                 op_type: str, performed_at: str,
                                 detail: dict[str, Any] | None = None) -> WriteReceipt:
        detail = self._object(detail, "detail")
        payload = {"actor_id": actor_id, "trial_id": trial_id, "op_type": op_type,
                   "performed_at": performed_at, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            trial, plot, site, protocol = self._trial_context(connection, trial_id)
            self._check_site_scope(actor, site)
            op_type = self._text(op_type, "op_type", 60)
            performed = self._instant(performed_at, "performed_at")

            def create() -> tuple[str, str, dict[str, Any]]:
                operation_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO farming_operations(operation_id,trial_id,op_type,performed_at,detail_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (operation_id, trial_id, op_type, performed, canonical_json(detail),
                         actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("相同农事操作已登记") from exc
                append_event(connection, actor_id=actor_id, action="farming_operation.recorded",
                             resource_type="farming_operation", resource_id=operation_id,
                             detail={"trial_id": trial_id, "op_type": op_type, "performed_at": performed},
                             occurred_at=self._now())
                deviation = None
                if op_type == "sowing":
                    window_start = _parse_instant(protocol["sowing_window_start"], "sowing_window_start")
                    window_end = _parse_instant(protocol["sowing_window_end"], "sowing_window_end")
                    moment = _parse_instant(performed, "performed_at")
                    if not window_start <= moment <= window_end:
                        if trial["status"] in ("planned", "active"):
                            connection.execute(
                                "UPDATE trials SET status='deviated', deviation_reason=? WHERE trial_id=?",
                                ("sowing_out_of_window", trial_id),
                            )
                            deviation = "sowing_out_of_window"
                            append_event(connection, actor_id=actor_id, action="trial.deviated",
                                         resource_type="trial", resource_id=trial_id,
                                         detail={"reason": "sowing_out_of_window", "performed_at": performed},
                                         occurred_at=self._now())
                    elif trial["status"] == "planned":
                        connection.execute("UPDATE trials SET status='active' WHERE trial_id=?", (trial_id,))
                response = {"operation_id": operation_id, "trial_id": trial_id}
                if deviation:
                    response["deviation"] = deviation
                return "farming_operation", operation_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="record_farming_operation", payload=payload, create=create)

    def record_weather_interval(self, *, request_id: str, actor_id: str, weather_id: str,
                                site_id: str, started_at: str, ended_at: str,
                                severity: str, summary: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "weather_id": weather_id, "site_id": site_id,
                   "started_at": started_at, "ended_at": ended_at, "severity": severity, "summary": summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            weather_id = self._identifier(weather_id, "weather_id")
            if severity not in SEVERITIES:
                raise ValidationError("severity 不在允许范围内")
            start, end = self._window(started_at, ended_at, "weather")
            summary = self._text(summary, "summary")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO weather_intervals(weather_id,site_id,started_at,ended_at,severity,summary,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (weather_id, site_id, start, end, severity, summary, actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("气象区间编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="weather.recorded",
                             resource_type="weather_interval", resource_id=weather_id,
                             detail={"site_id": site_id, "started_at": start, "ended_at": end,
                                     "severity": severity}, occurred_at=self._now())
                return "weather_interval", weather_id, {"weather_id": weather_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_weather_interval", payload=payload, create=create)

    def mark_trial_deviation(self, *, request_id: str, actor_id: str, trial_id: str,
                             reason: str, weather_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "trial_id": trial_id, "reason": reason, "weather_id": weather_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            trial, plot, site, protocol = self._trial_context(connection, trial_id)
            self._check_site_scope(actor, site)
            reason = self._text(reason, "reason")
            if weather_id is not None:
                weather = self._one(connection, "SELECT * FROM weather_intervals WHERE weather_id=?",
                                    (weather_id,), "气象区间不存在")
                if weather["site_id"] != site["site_id"]:
                    raise ValidationError("气象区间不属于试验所在场所")

            def create() -> tuple[str, str, dict[str, Any]]:
                if trial["status"] == "deviated":
                    raise ConflictError("试验已处于偏离状态")
                if trial["status"] == "excluded":
                    raise ConflictError("试验已被排除")
                connection.execute("UPDATE trials SET status='deviated', deviation_reason=? WHERE trial_id=?",
                                   (reason, trial_id))
                append_event(connection, actor_id=actor_id, action="trial.deviated",
                             resource_type="trial", resource_id=trial_id,
                             detail={"reason": reason, "weather_id": weather_id}, occurred_at=self._now())
                return "trial", trial_id, {"trial_id": trial_id, "status": "deviated"}

            return self._idempotent(connection, request_id=request_id,
                                    action="mark_trial_deviation", payload=payload, create=create)

    # ---------- 离线观测归并 ----------

    def ingest_observations(self, *, request_id: str, actor_id: str, device_id: str,
                            trial_id: str, records: list[dict[str, Any]]) -> tuple[WriteReceipt, dict[str, Any]]:
        if not isinstance(records, list) or not records:
            raise ValidationError("records 必须是非空数组")
        payload = {"actor_id": actor_id, "device_id": device_id, "trial_id": trial_id, "records": records}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            device_id = self._identifier(device_id, "device_id")
            trial, plot, site, protocol = self._trial_context(connection, trial_id)
            self._check_site_scope(actor, site)
            window_start = _parse_instant(protocol["observation_window_start"], "observation_window_start")
            window_end = _parse_instant(protocol["observation_window_end"], "observation_window_end")
            calibrations = [
                (_parse_instant(row["calibrated_at"], "calibrated_at"),
                 _parse_instant(row["valid_until"], "valid_until"))
                for row in connection.execute(
                    "SELECT calibrated_at, valid_until FROM device_calibrations WHERE device_id=?", (device_id,))
            ]
            normalized = []
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    raise ValidationError(f"records[{index}] 必须是对象")
                sequence = record.get("device_seq")
                if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                    raise ValidationError(f"records[{index}].device_seq 必须是非负整数")
                observed = _parse_instant(record.get("observed_at", ""), f"records[{index}].observed_at")
                metric = self._text(record.get("metric", ""), f"records[{index}].metric", 80)
                value = self._number(record.get("value"), f"records[{index}].value")
                normalized.append((sequence, observed, metric, value))
            normalized.sort(key=lambda item: item[0])

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT MAX(device_seq) AS max_seq FROM observations WHERE device_id=?", (device_id,)
                ).fetchone()
                max_seq = row["max_seq"] if row["max_seq"] is not None else -1
                accepted = pending = duplicates = 0
                items = []
                for sequence, observed, metric, value in normalized:
                    existing = connection.execute(
                        "SELECT observation_id FROM observations WHERE device_id=? AND device_seq=?",
                        (device_id, sequence),
                    ).fetchone()
                    if existing:
                        duplicates += 1
                        items.append({"device_seq": sequence, "status": "duplicate",
                                      "observation_id": existing["observation_id"]})
                        continue
                    status, reason = "accepted", None
                    if sequence <= max_seq:
                        status, reason = "pending", "out_of_order"
                    elif not window_start <= observed <= window_end:
                        status, reason = "pending", "out_of_window"
                    elif not any(start <= observed <= end for start, end in calibrations):
                        status, reason = "pending", "calibration_expired"
                    observation_id = uuid.uuid4().hex
                    content = {"device_id": device_id, "device_seq": sequence, "trial_id": trial_id,
                               "observed_at": _format_instant(observed), "metric": metric, "value": value}
                    connection.execute(
                        "INSERT INTO observations(observation_id,device_id,device_seq,trial_id,observed_at,metric,"
                        "value,status,pending_reason,content_hash,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (observation_id, device_id, sequence, trial_id, _format_instant(observed), metric,
                         value, status, reason, digest(content), actor_id, self._now()),
                    )
                    max_seq = max(max_seq, sequence)
                    if status == "accepted":
                        accepted += 1
                    else:
                        pending += 1
                    item = {"device_seq": sequence, "status": status, "observation_id": observation_id}
                    if reason:
                        item["reason"] = reason
                    items.append(item)
                append_event(connection, actor_id=actor_id, action="observations.ingested",
                             resource_type="trial", resource_id=trial_id,
                             detail={"device_id": device_id, "accepted": accepted,
                                     "pending": pending, "duplicates": duplicates},
                             occurred_at=self._now())
                response = {"trial_id": trial_id, "device_id": device_id, "accepted": accepted,
                            "pending": pending, "duplicates": duplicates, "items": items}
                return "trial", trial_id, response

            return self._rich_idempotent(connection, request_id=request_id,
                                         action="ingest_observations", payload=payload, create=create)

    def review_observation(self, *, request_id: str, actor_id: str, observation_id: str,
                           decision: str, note: str = "") -> WriteReceipt:
        if decision not in ("accept", "reject"):
            raise ValidationError("decision 必须是 accept 或 reject")
        payload = {"actor_id": actor_id, "observation_id": observation_id,
                   "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            observation = self._one(connection, "SELECT * FROM observations WHERE observation_id=?",
                                    (observation_id,), "观测记录不存在")
            note = self._text(note, "note", 500) if note else ""

            def create() -> tuple[str, str, dict[str, Any]]:
                if observation["status"] != "pending":
                    raise ConflictError("观测记录不在待审状态")
                new_status = "accepted" if decision == "accept" else "rejected"
                connection.execute(
                    "UPDATE observations SET status=?, reviewed_by=?, reviewed_at=? WHERE observation_id=?",
                    (new_status, actor_id, self._now(), observation_id),
                )
                append_event(connection, actor_id=actor_id, action="observation.reviewed",
                             resource_type="observation", resource_id=observation_id,
                             detail={"decision": decision, "note": note,
                                     "pending_reason": observation["pending_reason"]},
                             occurred_at=self._now())
                return "observation", observation_id, {"observation_id": observation_id, "status": new_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="review_observation", payload=payload, create=create)

    def list_pending_observations(self, trial_id: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT o.*, t.plot_id FROM observations o JOIN trials t ON o.trial_id=t.trial_id "
                 "WHERE o.status='pending'")
        parameters: list[Any] = []
        if trial_id:
            query += " AND o.trial_id=?"
            parameters.append(trial_id)
        query += " ORDER BY o.created_at, o.observation_id"
        return [{"observation_id": row["observation_id"], "trial_id": row["trial_id"],
                 "plot_id": row["plot_id"], "device_id": row["device_id"],
                 "device_seq": row["device_seq"], "observed_at": row["observed_at"],
                 "metric": row["metric"], "value": row["value"],
                 "pending_reason": row["pending_reason"]}
                for row in self.database.connection.execute(query, parameters)]

    # ---------- 灾害性气象豁免 ----------

    def grant_disaster_exemption(self, *, request_id: str, actor_id: str, trial_id: str,
                                 weather_id: str, reason: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "trial_id": trial_id, "weather_id": weather_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            trial, plot, site, protocol = self._trial_context(connection, trial_id)
            weather = self._one(connection, "SELECT * FROM weather_intervals WHERE weather_id=?",
                                (weather_id,), "气象区间不存在")
            reason = self._text(reason, "reason")
            if weather["severity"] != "disaster":
                raise ValidationError("只有灾害性气象才能用于豁免")
            if weather["site_id"] != site["site_id"]:
                raise ValidationError("气象区间不属于试验所在场所")

            def create() -> tuple[str, str, dict[str, Any]]:
                if trial["status"] != "deviated":
                    raise ValidationError("只有偏离方案的试验才能申请豁免")
                exemption_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO trial_exemptions(exemption_id,trial_id,weather_id,reason,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (exemption_id, trial_id, weather_id, reason, actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("该试验已存在同一气象事件的豁免") from exc
                effective = connection.execute(
                    "SELECT verdict_id FROM verdicts WHERE region_id=? AND variety_id=? AND status='effective'",
                    (protocol["region_id"], protocol["variety_id"]),
                ).fetchone()
                append_event(connection, actor_id=actor_id, action="trial.exemption_granted",
                             resource_type="trial", resource_id=trial_id,
                             detail={"exemption_id": exemption_id, "weather_id": weather_id, "reason": reason},
                             occurred_at=self._now())
                return "trial_exemption", exemption_id, {
                    "exemption_id": exemption_id, "trial_id": trial_id,
                    "successor_required_for": effective["verdict_id"] if effective else None}

            return self._rich_idempotent(connection, request_id=request_id,
                                         action="grant_disaster_exemption", payload=payload, create=create)

    # ---------- 试验集合评估与冻结 ----------

    def _evaluate(self, connection, protocol: sqlite3.Row) -> EvaluationResult:
        rules = connection.execute(
            "SELECT * FROM exclusion_rules WHERE protocol_id=? AND active=1 ORDER BY created_at, rule_id",
            (protocol["protocol_id"],),
        ).fetchall()
        valid: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        trials = connection.execute(
            "SELECT * FROM trials WHERE protocol_id=? ORDER BY created_at, trial_id",
            (protocol["protocol_id"],),
        ).fetchall()
        for trial in trials:
            reasons: list[str] = []
            batch = self._one(connection, "SELECT * FROM seed_batches WHERE batch_id=?",
                              (trial["seed_batch_id"],), "种子批次不存在")
            if batch["status"] == "withdrawn":
                reasons.append("seed_batch_withdrawn")
            if trial["status"] == "excluded":
                reasons.append("manually_excluded")
            accepted = connection.execute(
                "SELECT metric, value, content_hash FROM observations "
                "WHERE trial_id=? AND status='accepted' ORDER BY device_id, device_seq",
                (trial["trial_id"],),
            ).fetchall()
            if not accepted:
                reasons.append("no_accepted_observations")
            exempted = connection.execute(
                "SELECT 1 FROM trial_exemptions WHERE trial_id=? LIMIT 1", (trial["trial_id"],)
            ).fetchone() is not None
            for rule in rules:
                definition = json.loads(rule["definition_json"])
                if rule["rule_type"] == "protocol_deviation":
                    if trial["status"] == "deviated" and not exempted:
                        reasons.append(f"rule:{rule['rule_id']}:protocol_deviation")
                elif rule["rule_type"] == "min_observations":
                    if len(accepted) < int(definition["count"]):
                        reasons.append(f"rule:{rule['rule_id']}:min_observations")
                elif rule["rule_type"] == "metric_range":
                    values = [row["value"] for row in accepted if row["metric"] == definition["metric"]]
                    if values:
                        mean = sum(values) / len(values)
                        if not definition["min"] <= mean <= definition["max"]:
                            reasons.append(f"rule:{rule['rule_id']}:metric_range")
            if reasons:
                excluded.append({"trial_id": trial["trial_id"], "plot_id": trial["plot_id"],
                                 "reasons": reasons})
            else:
                valid.append({
                    "trial_id": trial["trial_id"], "plot_id": trial["plot_id"],
                    "seed_batch_id": trial["seed_batch_id"],
                    "accepted_observations": len(accepted),
                    "observation_hash": digest([row["content_hash"] for row in accepted]),
                })
        return EvaluationResult(protocol_id=protocol["protocol_id"], region_id=protocol["region_id"],
                                variety_id=protocol["variety_id"], valid=valid, excluded=excluded)

    def evaluate_trials(self, protocol_id: str) -> dict[str, Any]:
        """按当前数据评估试验集合（只读，不冻结）。"""

        with self.database.transaction() as connection:
            protocol = self._one(connection, "SELECT * FROM trial_protocols WHERE protocol_id=?",
                                 (protocol_id,), "试验方案不存在")
            result = self._evaluate(connection, protocol)
        return {"protocol_id": result.protocol_id, "region_id": result.region_id,
                "variety_id": result.variety_id, "valid": result.valid, "excluded": result.excluded}

    def freeze_analysis_version(self, *, request_id: str, actor_id: str,
                                protocol_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "protocol_id": protocol_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            protocol = self._one(connection, "SELECT * FROM trial_protocols WHERE protocol_id=?",
                                 (protocol_id,), "试验方案不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                evaluation = self._evaluate(connection, protocol)
                row = connection.execute(
                    "SELECT MAX(version_no) AS max_no FROM analysis_versions WHERE protocol_id=?",
                    (protocol["protocol_id"],),
                ).fetchone()
                version_no = (row["max_no"] or 0) + 1
                input_hash = digest({"protocol_id": protocol["protocol_id"],
                                     "valid": evaluation.valid, "excluded": evaluation.excluded})
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO analysis_versions(version_id,protocol_id,region_id,variety_id,version_no,"
                    "valid_trials_json,excluded_trials_json,input_hash,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'frozen',?,?)",
                    (version_id, protocol["protocol_id"], protocol["region_id"], protocol["variety_id"],
                     version_no, canonical_json(evaluation.valid), canonical_json(evaluation.excluded),
                     input_hash, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="analysis.frozen",
                             resource_type="analysis_version", resource_id=version_id,
                             detail={"protocol_id": protocol["protocol_id"], "version_no": version_no,
                                     "input_hash": input_hash, "valid_count": len(evaluation.valid),
                                     "excluded_count": len(evaluation.excluded)},
                             occurred_at=self._now())
                response = {"version_id": version_id, "version_no": version_no,
                            "valid_count": len(evaluation.valid),
                            "excluded_count": len(evaluation.excluded), "input_hash": input_hash}
                return "analysis_version", version_id, response

            return self._rich_idempotent(connection, request_id=request_id,
                                         action="freeze_analysis_version", payload=payload, create=create)

    # ---------- 区域裁定 ----------

    def create_verdict(self, *, request_id: str, actor_id: str, analysis_version_id: str,
                       decision: str, rationale: str,
                       supersedes_verdict_id: str | None = None,
                       successor_reason: str | None = None) -> tuple[WriteReceipt, dict[str, Any]]:
        if decision not in DECISIONS:
            raise ValidationError("decision 不在允许范围内")
        payload = {"actor_id": actor_id, "analysis_version_id": analysis_version_id,
                   "decision": decision, "rationale": rationale,
                   "supersedes_verdict_id": supersedes_verdict_id, "successor_reason": successor_reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            version = self._one(connection, "SELECT * FROM analysis_versions WHERE version_id=?",
                                (analysis_version_id,), "分析版本不存在")
            protocol = self._one(connection, "SELECT * FROM trial_protocols WHERE protocol_id=?",
                                 (version["protocol_id"],), "试验方案不存在")
            rationale = self._text(rationale, "rationale", 500)
            valid_count = len(json.loads(version["valid_trials_json"]))
            if decision == "recommended" and valid_count < protocol["min_valid_trials"]:
                raise ValidationError("有效试验数量不足，不能给出推荐结论")
            if decision == "limited" and valid_count < 1:
                raise ValidationError("没有有效试验，不能给出限制结论")

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = connection.execute(
                    "SELECT MAX(version_no) AS max_no FROM analysis_versions WHERE protocol_id=?",
                    (version["protocol_id"],),
                ).fetchone()["max_no"]
                if version["version_no"] != latest:
                    raise ConflictError("只能引用最新冻结的分析版本")
                current = connection.execute(
                    "SELECT * FROM verdicts WHERE region_id=? AND variety_id=? AND status='effective'",
                    (version["region_id"], version["variety_id"]),
                ).fetchone()
                if current is None:
                    if supersedes_verdict_id is not None or successor_reason is not None:
                        raise ValidationError("首个裁定不能声明后继关系")
                else:
                    if supersedes_verdict_id != current["verdict_id"]:
                        raise ConflictError("已存在生效裁定，必须以后继裁定取代")
                    if successor_reason not in SUCCESSOR_REASONS:
                        raise ValidationError("后继裁定必须给出合法原因")
                verdict_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO verdicts(verdict_id,region_id,variety_id,decision,analysis_version_id,"
                    "rationale,status,supersedes_verdict_id,successor_reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,'confirming',?,?,?,?)",
                    (verdict_id, version["region_id"], version["variety_id"], decision,
                     analysis_version_id, rationale, supersedes_verdict_id, successor_reason,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="verdict.created",
                             resource_type="verdict", resource_id=verdict_id,
                             detail={"region_id": version["region_id"], "variety_id": version["variety_id"],
                                     "decision": decision, "analysis_version_id": analysis_version_id,
                                     "supersedes_verdict_id": supersedes_verdict_id,
                                     "successor_reason": successor_reason},
                             occurred_at=self._now())
                response = {"verdict_id": verdict_id, "status": "confirming",
                            "region_id": version["region_id"], "variety_id": version["variety_id"]}
                return "verdict", verdict_id, response

            return self._rich_idempotent(connection, request_id=request_id,
                                         action="create_verdict", payload=payload, create=create)

    def confirm_verdict(self, *, request_id: str, actor_id: str,
                        verdict_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "verdict_id": verdict_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            verdict = self._one(connection, "SELECT * FROM verdicts WHERE verdict_id=?",
                                (verdict_id,), "裁定不存在")
            version = self._one(connection, "SELECT * FROM analysis_versions WHERE version_id=?",
                                (verdict["analysis_version_id"],), "分析版本不存在")
            protocol = self._one(connection, "SELECT * FROM trial_protocols WHERE protocol_id=?",
                                 (version["protocol_id"],), "试验方案不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if verdict["status"] == "superseded":
                    raise ConflictError("裁定已被取代，不能确认")
                try:
                    connection.execute(
                        "INSERT INTO verdict_confirmations(verdict_id,reviewer_id,request_id,confirmed_at) "
                        "VALUES(?,?,?,?)",
                        (verdict_id, actor_id, request_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("审查人已确认过该裁定") from exc
                confirmations = connection.execute(
                    "SELECT COUNT(*) AS count FROM verdict_confirmations WHERE verdict_id=?", (verdict_id,)
                ).fetchone()["count"]
                status = verdict["status"]
                superseded_id = None
                if status == "confirming" and confirmations >= protocol["confirmations_required"]:
                    current = connection.execute(
                        "SELECT * FROM verdicts WHERE region_id=? AND variety_id=? AND status='effective' "
                        "AND verdict_id!=?",
                        (verdict["region_id"], verdict["variety_id"], verdict_id),
                    ).fetchone()
                    if current is None:
                        if verdict["supersedes_verdict_id"] is not None:
                            raise ConflictError("前序裁定不是当前生效版本")
                    else:
                        if verdict["supersedes_verdict_id"] != current["verdict_id"]:
                            raise ConflictError("生效裁定已变化，需要重新创建后继裁定")
                        connection.execute(
                            "UPDATE verdicts SET status='superseded', superseded_at=? WHERE verdict_id=?",
                            (self._now(), current["verdict_id"]),
                        )
                        superseded_id = current["verdict_id"]
                        append_event(connection, actor_id=actor_id, action="verdict.superseded",
                                     resource_type="verdict", resource_id=superseded_id,
                                     detail={"superseded_by": verdict_id,
                                             "successor_reason": verdict["successor_reason"]},
                                     occurred_at=self._now())
                    try:
                        connection.execute(
                            "UPDATE verdicts SET status='effective', effective_at=? WHERE verdict_id=?",
                            (self._now(), verdict_id),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ConflictError("生效裁定并发变化，需要重新创建后继裁定") from exc
                    status = "effective"
                    append_event(connection, actor_id=actor_id, action="verdict.effective",
                                 resource_type="verdict", resource_id=verdict_id,
                                 detail={"region_id": verdict["region_id"],
                                         "variety_id": verdict["variety_id"],
                                         "confirmations": confirmations},
                                 occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="verdict.confirmed",
                             resource_type="verdict", resource_id=verdict_id,
                             detail={"reviewer_id": actor_id, "confirmations": confirmations,
                                     "status": status}, occurred_at=self._now())
                response = {"verdict_id": verdict_id, "status": status,
                            "confirmations": confirmations,
                            "superseded_verdict_id": superseded_id}
                return "verdict", verdict_id, response

            return self._rich_idempotent(connection, request_id=request_id,
                                         action="confirm_verdict", payload=payload, create=create)

    # ---------- 结论解释 ----------

    def _plot_brief(self, connection, plot_id: str) -> dict[str, Any]:
        plot = self._one(connection, "SELECT * FROM plots WHERE plot_id=?", (plot_id,), "地块不存在")
        return {"plot_id": plot["plot_id"], "site_id": plot["site_id"], "name": plot["name"],
                "soil_type": plot["soil_type"]}

    def _explain(self, connection, verdict_id: str) -> dict[str, Any]:
        verdict = self._one(connection, "SELECT * FROM verdicts WHERE verdict_id=?",
                            (verdict_id,), "裁定不存在")
        version = self._one(connection, "SELECT * FROM analysis_versions WHERE version_id=?",
                            (verdict["analysis_version_id"],), "分析版本不存在")
        protocol = self._one(connection, "SELECT * FROM trial_protocols WHERE protocol_id=?",
                             (version["protocol_id"],), "试验方案不存在")
        used = []
        for entry in json.loads(version["valid_trials_json"]):
            used.append({**entry, "plot": self._plot_brief(connection, entry["plot_id"])})
        excluded = []
        for entry in json.loads(version["excluded_trials_json"]):
            excluded.append({**entry, "plot": self._plot_brief(connection, entry["plot_id"])})
        confirmations = [
            {"reviewer_id": row["reviewer_id"], "confirmed_at": row["confirmed_at"]}
            for row in connection.execute(
                "SELECT reviewer_id, confirmed_at FROM verdict_confirmations WHERE verdict_id=? "
                "ORDER BY confirmed_at, reviewer_id", (verdict_id,))
        ]
        return {
            "verdict_id": verdict["verdict_id"],
            "region_id": verdict["region_id"],
            "variety_id": verdict["variety_id"],
            "decision": verdict["decision"],
            "rationale": verdict["rationale"],
            "status": verdict["status"],
            "successor_reason": verdict["successor_reason"],
            "supersedes_verdict_id": verdict["supersedes_verdict_id"],
            "created_by": verdict["created_by"],
            "created_at": verdict["created_at"],
            "effective_at": verdict["effective_at"],
            "analysis": {"version_id": version["version_id"], "version_no": version["version_no"],
                         "protocol_id": version["protocol_id"], "input_hash": version["input_hash"],
                         "frozen_at": version["created_at"]},
            "used_trials": used,
            "excluded_trials": excluded,
            "confirmations": confirmations,
            "confirmations_required": protocol["confirmations_required"],
        }

    def explain_verdict(self, verdict_id: str) -> dict[str, Any]:
        """解释一项裁定：结论、依据的冻结版本、使用与排除的地块。"""

        with self.database.transaction() as connection:
            return self._explain(connection, verdict_id)

    def explain_region(self, region_id: str, variety_id: str) -> dict[str, Any]:
        """解释某区域当前生效的品种裁定。"""

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT verdict_id FROM verdicts WHERE region_id=? AND variety_id=? AND status='effective'",
                (region_id, variety_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("该区域品种组合暂无生效裁定")
            return self._explain(connection, row["verdict_id"])

    def verdict_history(self, region_id: str, variety_id: str) -> list[dict[str, Any]]:
        """列出某区域品种组合的全部裁定版本，保留后继裁定链。"""

        rows = self.database.connection.execute(
            "SELECT * FROM verdicts WHERE region_id=? AND variety_id=? ORDER BY created_at, rowid",
            (region_id, variety_id),
        ).fetchall()
        return [{"verdict_id": row["verdict_id"], "decision": row["decision"], "status": row["status"],
                 "successor_reason": row["successor_reason"],
                 "supersedes_verdict_id": row["supersedes_verdict_id"],
                 "analysis_version_id": row["analysis_version_id"],
                 "created_at": row["created_at"], "effective_at": row["effective_at"],
                 "superseded_at": row["superseded_at"]} for row in rows]

    def trial_summary(self, trial_id: str) -> dict[str, Any]:
        """汇总单个试验的状态与观测计数，便于追溯。"""

        with self.database.transaction() as connection:
            trial, plot, site, protocol = self._trial_context(connection, trial_id)
            counts = {"accepted": 0, "pending": 0, "rejected": 0}
            for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM observations WHERE trial_id=? GROUP BY status",
                    (trial_id,)):
                counts[row["status"]] = row["count"]
            exempted = connection.execute(
                "SELECT 1 FROM trial_exemptions WHERE trial_id=? LIMIT 1", (trial_id,)
            ).fetchone() is not None
            return {"trial_id": trial["trial_id"], "status": trial["status"],
                    "deviation_reason": trial["deviation_reason"], "exempted": exempted,
                    "protocol_id": trial["protocol_id"], "region_id": protocol["region_id"],
                    "variety_id": protocol["variety_id"], "plot_id": plot["plot_id"],
                    "site_id": site["site_id"], "seed_batch_id": trial["seed_batch_id"],
                    "observations": counts}
