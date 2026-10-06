"""多地点农业试验与推广裁定服务。

在基础协作服务之上建立可验证证据链：
种子批次 → 地块条件 → 试验方案 → 农事操作 → 气象区间 → 设备校准 → 排除规则
→ 离线观测归并/待审 → 冻结分析版本 → 区域×品种版本化裁定 → 多审查人并发确认。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService
from . import trial_domain as rules


AMENDMENT_KINDS = frozenset({"late_correction", "disaster_exemption", "lot_withdrawal"})


class TrialService(DomainService):
    """实现试验登记、观测归并、分析冻结与推广裁定。"""

    # ------------------------------------------------------------------ 通用辅助

    def _idem(self, *, request_id: str, action: str, payload: dict[str, Any],
              create) -> dict[str, Any]:
        """幂等执行 create()，成功时返回的 dict 会被完整存档以便重放。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
            ).fetchone()
            if row:
                if row["action"] != action or row["payload_hash"] != payload_hash:
                    raise ConflictError("request_id 已被不同内容使用")
                response = json.loads(row["response_json"])
                response["replayed"] = True
                return response
            response = create(connection)
            response.setdefault("request_id", request_id)
            response["replayed"] = False
            connection.execute(
                "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
                "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (request_id, action, payload_hash, response["resource_type"],
                 response["resource_id"], canonical_json(response), self._now()),
            )
            return response

    def _must_exist(self, connection, table: str, key_column: str, key_value: str, message: str):
        row = connection.execute(
            f"SELECT * FROM {table} WHERE {key_column}=?", (key_value,)
        ).fetchone()
        if row is None:
            raise NotFoundError(message)
        return row

    def _instant(self, value: str, field: str) -> str:
        try:
            rules.parse_instant(value)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{field} 必须是带时区的 ISO-8601 时间") from exc
        return value

    def _load_json(self, connection, row) -> dict[str, Any]:
        return json.loads(row["payload_json"])

    # ------------------------------------------------------------------ 种子批次

    def register_seed_lot(self, *, request_id: str, actor_id: str, lot_id: str,
                          cultivar_id: str, cultivar_name: str, lot_code: str,
                          attributes: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "lot_id": lot_id, "cultivar_id": cultivar_id,
                   "lot_code": lot_code, "attributes": attributes or {}}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            lot_id_v = self._identifier(lot_id, "lot_id")
            cultivar_id_v = self._identifier(cultivar_id, "cultivar_id")
            lot_code_v = self._text(lot_code, "lot_code", 80)
            cultivar_name_v = self._text(cultivar_name, "cultivar_name")
            data = {"cultivar_id": cultivar_id_v, "cultivar_name": cultivar_name_v,
                    "lot_code": lot_code_v, "attributes": attributes or {}}
            try:
                connection.execute(
                    "INSERT INTO seed_lots(lot_id,cultivar_id,cultivar_name,lot_code,organization_id,"
                    "status,payload_json,created_by,created_at) VALUES(?,?,?,?,?,'active',?,?,?)",
                    (lot_id_v, cultivar_id_v, cultivar_name_v, lot_code_v, actor.organization_id,
                     canonical_json(data), actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("种子批次已存在") from exc
            append_event(connection, actor_id=actor_id, action="seed_lot.registered",
                         resource_type="seed_lot", resource_id=lot_id_v,
                         detail={"cultivar_id": cultivar_id_v, "lot_code": lot_code_v},
                         occurred_at=self._now())
            return {"resource_type": "seed_lot", "resource_id": lot_id_v, "lot_id": lot_id_v,
                    "status": "active"}

        return self._idem(request_id=request_id, action="register_seed_lot",
                          payload=payload, create=create)

    def withdraw_seed_lot(self, *, request_id: str, actor_id: str, lot_id: str,
                          reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "lot_id": lot_id, "reason": reason}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            lot = self._must_exist(connection, "seed_lots", "lot_id", lot_id, "种子批次不存在")
            if lot["status"] == "withdrawn":
                raise ConflictError("种子批次已撤回，不能重复撤回")
            connection.execute(
                "UPDATE seed_lots SET status='withdrawn', withdrawn_at=? WHERE lot_id=?",
                (self._now(), lot_id),
            )
            append_event(connection, actor_id=actor_id, action="seed_lot.withdrawn",
                         resource_type="seed_lot", resource_id=lot_id,
                         detail={"reason": reason, "cultivar_id": lot["cultivar_id"]},
                         occurred_at=self._now())
            return {"resource_type": "seed_lot", "resource_id": lot_id, "lot_id": lot_id,
                    "status": "withdrawn"}

        return self._idem(request_id=request_id, action="withdraw_seed_lot",
                          payload=payload, create=create)

    # ------------------------------------------------------------------ 地块与方案

    def register_plot(self, *, request_id: str, actor_id: str, plot_id: str, site_id: str,
                      region_id: str, name: str, conditions: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(conditions, dict) or not conditions:
            raise ValidationError("conditions 必须是非空对象（地块立地条件）")
        payload = {"actor_id": actor_id, "plot_id": plot_id, "site_id": site_id,
                   "region_id": region_id, "conditions": conditions}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plot_id_v = self._identifier(plot_id, "plot_id")
            region_id_v = self._identifier(region_id, "region_id")
            site = self._must_exist(connection, "sites", "site_id", site_id, "场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所登记地块")
            name_v = self._text(name, "name")
            conditions_hash = digest(conditions)
            try:
                connection.execute(
                    "INSERT INTO plots(plot_id,site_id,region_id,name,conditions_json,payload_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (plot_id_v, site_id, region_id_v, name_v, canonical_json(conditions),
                     conditions_hash, actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("地块编号已存在") from exc
            append_event(connection, actor_id=actor_id, action="plot.registered",
                         resource_type="plot", resource_id=plot_id_v,
                         detail={"site_id": site_id, "region_id": region_id_v,
                                 "conditions_hash": conditions_hash},
                         occurred_at=self._now())
            return {"resource_type": "plot", "resource_id": plot_id_v, "plot_id": plot_id_v}

        return self._idem(request_id=request_id, action="register_plot",
                          payload=payload, create=create)

    def register_protocol(self, *, request_id: str, actor_id: str, protocol_id: str,
                          cultivar_id: str, season: str, windows: dict[str, str],
                          thresholds: dict[str, float], required_reviewers: int = 2,
                          late_grace_minutes: int = 43200, code_version: str = "trial-rules-1") -> dict[str, Any]:
        self._validate_windows(windows)
        self._validate_thresholds(thresholds)
        if int(required_reviewers) < 1:
            raise ValidationError("required_reviewers 至少为 1")
        payload = {"actor_id": actor_id, "protocol_id": protocol_id, "cultivar_id": cultivar_id,
                   "season": season, "windows": windows, "thresholds": thresholds}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            protocol_id_v = self._identifier(protocol_id, "protocol_id")
            cultivar_id_v = self._identifier(cultivar_id, "cultivar_id")
            season_v = self._text(season, "season", 40)
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS next_version FROM trial_protocols "
                "WHERE cultivar_id=? AND season=?", (cultivar_id_v, season_v)
            ).fetchone()
            version = version_row["next_version"]
            data = {"cultivar_id": cultivar_id_v, "season": season_v, "windows": windows,
                    "thresholds": thresholds, "required_reviewers": int(required_reviewers),
                    "late_grace_minutes": int(late_grace_minutes), "code_version": code_version}
            try:
                connection.execute(
                    "INSERT INTO trial_protocols(protocol_id,cultivar_id,season,version,payload_json,"
                    "payload_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (protocol_id_v, cultivar_id_v, season_v, version, canonical_json(data),
                     digest(data), actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("试验方案编号已存在") from exc
            append_event(connection, actor_id=actor_id, action="protocol.registered",
                         resource_type="trial_protocol", resource_id=protocol_id_v,
                         detail={"cultivar_id": cultivar_id_v, "season": season_v, "version": version},
                         occurred_at=self._now())
            return {"resource_type": "trial_protocol", "resource_id": protocol_id_v,
                    "protocol_id": protocol_id_v, "version": version}

        return self._idem(request_id=request_id, action="register_protocol",
                          payload=payload, create=create)

    def _validate_windows(self, windows: dict[str, str]) -> None:
        if not isinstance(windows, dict) or not windows:
            raise ValidationError("windows 必须是非空对象")
        for key, value in windows.items():
            if value is not None:
                self._instant(value, f"windows.{key}")

    def _validate_thresholds(self, thresholds: dict[str, float]) -> None:
        required = {"min_effective_plots", "yield_recommend", "yield_restrict",
                    "disease_restrict", "seedling_restrict"}
        if not isinstance(thresholds, dict) or not required.issubset(thresholds):
            raise ValidationError(f"thresholds 必须包含 {sorted(required)}")
        for key in required:
            if not isinstance(thresholds[key], (int, float)):
                raise ValidationError(f"thresholds.{key} 必须是数值")

    def assign_plot(self, *, request_id: str, actor_id: str, protocol_id: str,
                    plot_id: str, lot_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "protocol_id": protocol_id, "plot_id": plot_id,
                   "lot_id": lot_id}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._must_exist(connection, "trial_protocols", "protocol_id", protocol_id, "试验方案不存在")
            plot = self._must_exist(connection, "plots", "plot_id", plot_id, "地块不存在")
            if lot_id is not None:
                lot = self._must_exist(connection, "seed_lots", "lot_id", lot_id, "种子批次不存在")
                protocol = connection.execute(
                    "SELECT * FROM trial_protocols WHERE protocol_id=?", (protocol_id,)
                ).fetchone()
                if lot["cultivar_id"] != json.loads(protocol["payload_json"])["cultivar_id"]:
                    raise ValidationError("种子批次品种与方案品种不一致")
            link_id = uuid.uuid4().hex
            try:
                connection.execute(
                    "INSERT INTO protocol_plots(link_id,protocol_id,plot_id,lot_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (link_id, protocol_id, plot_id, lot_id, actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("该地块已承接此方案") from exc
            append_event(connection, actor_id=actor_id, action="protocol_plot.assigned",
                         resource_type="protocol_plot", resource_id=link_id,
                         detail={"protocol_id": protocol_id, "plot_id": plot_id,
                                 "region_id": plot["region_id"], "lot_id": lot_id},
                         occurred_at=self._now())
            return {"resource_type": "protocol_plot", "resource_id": link_id, "link_id": link_id}

        return self._idem(request_id=request_id, action="assign_plot",
                          payload=payload, create=create)

    # ------------------------------------------------------------------ 农事/气象/设备/规则

    def record_operation(self, *, request_id: str, actor_id: str, protocol_id: str,
                         plot_id: str, operation_type: str, event_at: str,
                         data: dict[str, Any], offline: bool = False) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValidationError("data 必须是对象")
        self._instant(event_at, "event_at")
        payload = {"actor_id": actor_id, "protocol_id": protocol_id, "plot_id": plot_id,
                   "operation_type": operation_type, "event_at": event_at, "data": data,
                   "offline": offline}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._must_assigned(connection, protocol_id, plot_id)
            operation_type_v = self._text(operation_type, "operation_type", 40)
            operation_id = uuid.uuid4().hex
            data_hash = digest(data)
            try:
                connection.execute(
                    "INSERT INTO farm_operations(operation_id,protocol_id,plot_id,operation_type,"
                    "event_at,payload_json,payload_hash,recorded_by,recorded_at,offline) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (operation_id, protocol_id, plot_id, operation_type_v, event_at,
                     canonical_json(data), data_hash, actor_id, self._now(), 1 if offline else 0),
                )
            except Exception as exc:
                raise ConflictError("同一地块同一时间的同类农事操作已存在且内容不同") from exc
            append_event(connection, actor_id=actor_id, action="farm_operation.recorded",
                         resource_type="farm_operation", resource_id=operation_id,
                         detail={"protocol_id": protocol_id, "plot_id": plot_id,
                                 "operation_type": operation_type_v, "event_at": event_at,
                                 "offline": offline, "payload_hash": data_hash},
                         occurred_at=self._now())
            return {"resource_type": "farm_operation", "resource_id": operation_id,
                    "operation_id": operation_id, "offline": offline}

        return self._idem(request_id=request_id, action="record_operation",
                          payload=payload, create=create)

    def _must_assigned(self, connection, protocol_id: str, plot_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM protocol_plots WHERE protocol_id=? AND plot_id=?",
            (protocol_id, plot_id),
        ).fetchone()
        if row is None:
            raise ValidationError("地块未承接该试验方案")

    def record_weather(self, *, request_id: str, actor_id: str, weather_id: str,
                       region_id: str, started_at: str, ended_at: str,
                       severity: str, disaster: bool = False,
                       data: dict[str, Any] | None = None) -> dict[str, Any]:
        self._instant(started_at, "started_at")
        self._instant(ended_at, "ended_at")
        if rules.parse_instant(ended_at) < rules.parse_instant(started_at):
            raise ValidationError("ended_at 不能早于 started_at")
        payload = {"actor_id": actor_id, "weather_id": weather_id, "region_id": region_id,
                   "started_at": started_at, "ended_at": ended_at, "disaster": disaster}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            weather_id_v = self._identifier(weather_id, "weather_id")
            region_id_v = self._identifier(region_id, "region_id")
            severity_v = self._text(severity, "severity", 40)
            body = data or {}
            try:
                connection.execute(
                    "INSERT INTO weather_intervals(weather_id,region_id,started_at,ended_at,severity,"
                    "disaster,payload_json,payload_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (weather_id_v, region_id_v, started_at, ended_at, severity_v, 1 if disaster else 0,
                     canonical_json(body), digest(body), actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("气象区间编号已存在") from exc
            append_event(connection, actor_id=actor_id, action="weather.recorded",
                         resource_type="weather_interval", resource_id=weather_id_v,
                         detail={"region_id": region_id_v, "severity": severity_v,
                                 "disaster": disaster, "started_at": started_at, "ended_at": ended_at},
                         occurred_at=self._now())
            return {"resource_type": "weather_interval", "resource_id": weather_id_v,
                    "weather_id": weather_id_v, "disaster": disaster}

        return self._idem(request_id=request_id, action="record_weather",
                          payload=payload, create=create)

    def register_device(self, *, request_id: str, actor_id: str, device_sn: str,
                        device_type: str, plot_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "device_sn": device_sn, "device_type": device_type,
                   "plot_id": plot_id}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            device_sn_v = self._identifier(device_sn, "device_sn")
            device_type_v = self._text(device_type, "device_type", 40)
            if plot_id is not None:
                self._must_exist(connection, "plots", "plot_id", plot_id, "地块不存在")
            try:
                connection.execute(
                    "INSERT INTO devices(device_sn,device_type,plot_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (device_sn_v, device_type_v, plot_id, actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("设备序列号已登记") from exc
            append_event(connection, actor_id=actor_id, action="device.registered",
                         resource_type="device", resource_id=device_sn_v,
                         detail={"device_type": device_type_v, "plot_id": plot_id},
                         occurred_at=self._now())
            return {"resource_type": "device", "resource_id": device_sn_v,
                    "device_sn": device_sn_v}

        return self._idem(request_id=request_id, action="register_device",
                          payload=payload, create=create)

    def record_calibration(self, *, request_id: str, actor_id: str, calibration_id: str,
                           device_sn: str, valid_from: str, valid_to: str | None = None,
                           data: dict[str, Any] | None = None) -> dict[str, Any]:
        self._instant(valid_from, "valid_from")
        if valid_to is not None:
            self._instant(valid_to, "valid_to")
            if rules.parse_instant(valid_to) <= rules.parse_instant(valid_from):
                raise ValidationError("valid_to 必须晚于 valid_from")
        payload = {"actor_id": actor_id, "calibration_id": calibration_id,
                   "device_sn": device_sn, "valid_from": valid_from, "valid_to": valid_to}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._must_exist(connection, "devices", "device_sn", device_sn, "设备不存在")
            calibration_id_v = self._identifier(calibration_id, "calibration_id")
            body = data or {}
            connection.execute(
                "INSERT INTO device_calibrations(calibration_id,device_sn,valid_from,valid_to,"
                "payload_json,payload_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (calibration_id_v, device_sn, valid_from, valid_to, canonical_json(body),
                 digest(body), actor_id, self._now()),
            )
            append_event(connection, actor_id=actor_id, action="calibration.recorded",
                         resource_type="device_calibration", resource_id=calibration_id_v,
                         detail={"device_sn": device_sn, "valid_from": valid_from,
                                 "valid_to": valid_to, "payload_hash": digest(body)},
                         occurred_at=self._now())
            return {"resource_type": "device_calibration", "resource_id": calibration_id_v,
                    "calibration_id": calibration_id_v}

        return self._idem(request_id=request_id, action="record_calibration",
                          payload=payload, create=create)

    def register_exclusion_rule(self, *, request_id: str, actor_id: str, rule_id: str,
                                name: str, rule_type: str, target_kind: str,
                                expression: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(expression, dict):
            raise ValidationError("expression 必须是 JSON 谓词树对象")
        if rule_type != "static" or target_kind not in ("observation", "plot"):
            raise ValidationError("rule_type 仅支持 static，target_kind 仅支持 observation/plot")
        try:
            rules.evaluate_predicate(expression, {})
        except ValueError as exc:
            raise ValidationError(f"表达式无效: {exc}") from exc
        payload = {"actor_id": actor_id, "rule_id": rule_id, "expression": expression}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            rule_id_v = self._identifier(rule_id, "rule_id")
            name_v = self._text(name, "name")
            expression_text = canonical_json(expression)
            try:
                connection.execute(
                    "INSERT INTO exclusion_rules(rule_id,name,rule_type,target_kind,expression,"
                    "active,created_by,created_at) VALUES(?,?,?,?,?,1,?,?)",
                    (rule_id_v, name_v, rule_type, target_kind, expression_text,
                     actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("排除规则编号已存在") from exc
            append_event(connection, actor_id=actor_id, action="exclusion_rule.registered",
                         resource_type="exclusion_rule", resource_id=rule_id_v,
                         detail={"name": name_v, "target_kind": target_kind,
                                 "expression_hash": digest(expression)},
                         occurred_at=self._now())
            return {"resource_type": "exclusion_rule", "resource_id": rule_id_v,
                    "rule_id": rule_id_v}

        return self._idem(request_id=request_id, action="register_exclusion_rule",
                          payload=payload, create=create)

    # ------------------------------------------------------------------ 观测接入

    def ingest_observation(self, *, request_id: str, actor_id: str, device_sn: str,
                           plot_id: str, protocol_id: str, metric: str, observed_at: str,
                           value: float | int | None = None, value_text: str | None = None,
                           offline: bool = False, client_recorded_at: str | None = None) -> dict[str, Any]:
        self._instant(observed_at, "observed_at")
        if client_recorded_at is not None:
            self._instant(client_recorded_at, "client_recorded_at")
        if value is None and value_text is None:
            raise ValidationError("value 与 value_text 至少提供一个")
        payload = {"actor_id": actor_id, "device_sn": device_sn, "plot_id": plot_id,
                   "protocol_id": protocol_id, "metric": metric, "observed_at": observed_at,
                   "value": value, "value_text": value_text, "offline": offline,
                   "client_recorded_at": client_recorded_at}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            device = self._must_exist(connection, "devices", "device_sn", device_sn, "设备不存在")
            self._must_exist(connection, "plots", "plot_id", plot_id, "地块不存在")
            protocol_row = self._must_exist(connection, "trial_protocols", "protocol_id",
                                            protocol_id, "试验方案不存在")
            self._must_assigned(connection, protocol_id, plot_id)
            metric_v = self._text(metric, "metric", 60)
            observed_dt = rules.parse_instant(observed_at)
            protocol = json.loads(protocol_row["payload_json"])
            data = {"value": value, "value_text": value_text, "metric": metric_v,
                    "observed_at": observed_at, "device_type": device["device_type"],
                    "offline": offline}
            data_hash = digest(data)

            # 归并：同设备序列 + 观测时间 + 指标视为同一样本，重复上报不增加样本
            duplicate = connection.execute(
                "SELECT * FROM observations WHERE device_sn=? AND observed_at=? AND metric=?",
                (device_sn, observed_at, metric_v),
            ).fetchone()
            if duplicate is not None:
                if duplicate["payload_hash"] != data_hash:
                    raise ConflictError("同一设备时间点指标已上报不同内容，需走迟到更正流程")
                return {"resource_type": "observation", "resource_id": duplicate["observation_id"],
                        "observation_id": duplicate["observation_id"],
                        "status": duplicate["status"], "duplicate": True,
                        "duplicate_of": duplicate["observation_id"],
                        "pending_reasons": [duplicate["pending_reason"]] if duplicate["pending_reason"] else []}

            pending_reasons: list[str] = []

            # 1) 校准失效
            calibrations = [dict(row) for row in connection.execute(
                "SELECT * FROM device_calibrations WHERE device_sn=?", (device_sn,))]
            if rules.calibration_active(calibrations, observed_dt) is None:
                pending_reasons.append(rules.PENDING_CALIBRATION_EXPIRED)

            # 2) 超窗
            window_key = rules.WINDOW_BY_METRIC.get(metric_v)
            if window_key is not None and not rules.in_window(protocol["windows"], window_key, observed_dt):
                pending_reasons.append(rules.PENDING_OUT_OF_WINDOW)

            # 3) 越序：早于同地块同指标已有有效观测（按解析后的时间比较，避免文本排序误差）
            prior_rows = connection.execute(
                "SELECT observed_at FROM observations WHERE plot_id=? AND metric=? AND status='valid'",
                (plot_id, metric_v),
            ).fetchall()
            if any(observed_dt < rules.parse_instant(row["observed_at"]) for row in prior_rows):
                pending_reasons.append(rules.PENDING_OUT_OF_ORDER)

            # 4) 离线迟到宽限（按设备本地记录时间衡量）
            if offline and client_recorded_at is not None:
                grace_minutes = int(protocol.get("late_grace_minutes", 43200))
                recorded_dt = rules.parse_instant(client_recorded_at)
                if (rules.parse_instant(self._now()) - recorded_dt).total_seconds() > grace_minutes * 60:
                    pending_reasons.append(rules.PENDING_OFFLINE_LATE)

            status = "pending" if pending_reasons else "valid"
            observation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO observations(observation_id,device_sn,plot_id,protocol_id,metric,"
                "observed_at,value,value_text,payload_json,payload_hash,reported_by,reported_at,"
                "observed_origin,status,pending_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (observation_id, device_sn, plot_id, protocol_id, metric_v, observed_at,
                 value, value_text, canonical_json(data), data_hash, actor_id, self._now(),
                 "offline" if offline else "online", status,
                 ",".join(pending_reasons) if pending_reasons else None),
            )
            review_id = None
            if pending_reasons:
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_queue(review_id,observation_id,reason,detail_json,status,"
                    "created_by,created_at) VALUES(?,?,?,?,'open',?,?)",
                    (review_id, observation_id, ",".join(pending_reasons),
                     canonical_json({"device_sn": device_sn, "metric": metric_v,
                                     "observed_at": observed_at, "offline": offline}),
                     actor_id, self._now()),
                )
            append_event(connection, actor_id=actor_id, action="observation.ingested",
                         resource_type="observation", resource_id=observation_id,
                         detail={"plot_id": plot_id, "protocol_id": protocol_id, "metric": metric_v,
                                 "observed_at": observed_at, "offline": offline,
                                 "status": status, "pending_reasons": pending_reasons,
                                 "payload_hash": data_hash},
                         occurred_at=self._now())
            return {"resource_type": "observation", "resource_id": observation_id,
                    "observation_id": observation_id, "status": status, "duplicate": False,
                    "pending_reasons": pending_reasons, "review_id": review_id}

        return self._idem(request_id=request_id, action="ingest_observation",
                          payload=payload, create=create)

    def resolve_review(self, *, request_id: str, actor_id: str, review_id: str,
                       resolution: str, note: str = "") -> dict[str, Any]:
        if resolution not in ("accept", "reject"):
            raise ValidationError("resolution 仅支持 accept/reject")
        payload = {"actor_id": actor_id, "review_id": review_id, "resolution": resolution,
                   "note": note}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            review = self._must_exist(connection, "review_queue", "review_id", review_id,
                                      "待审事项不存在")
            if review["status"] != "open":
                raise ConflictError("待审事项已处理")
            observation_id = review["observation_id"]
            new_status = "valid" if resolution == "accept" else "excluded"
            connection.execute(
                "UPDATE observations SET status=? WHERE observation_id=?",
                (new_status, observation_id),
            )
            connection.execute(
                "UPDATE review_queue SET status=?, resolved_by=?, resolved_at=? WHERE review_id=?",
                ("resolved" if resolution == "accept" else "rejected",
                 actor_id, self._now(), review_id),
            )
            append_event(connection, actor_id=actor_id, action="review.resolved",
                         resource_type="review_queue", resource_id=review_id,
                         detail={"observation_id": observation_id, "resolution": resolution,
                                 "note": note, "original_reason": review["reason"]},
                         occurred_at=self._now())
            return {"resource_type": "review_queue", "resource_id": review_id,
                    "review_id": review_id, "observation_id": observation_id,
                    "observation_status": new_status}

        return self._idem(request_id=request_id, action="resolve_review",
                          payload=payload, create=create)

    # ------------------------------------------------------------------ 分析冻结

    def freeze_analysis(self, *, request_id: str, actor_id: str, analysis_id: str,
                        cultivar_id: str, season: str,
                        parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "analysis_id": analysis_id,
                   "cultivar_id": cultivar_id, "season": season, "parameters": parameters}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            analysis_id_v = self._identifier(analysis_id, "analysis_id")
            if connection.execute("SELECT 1 FROM analysis_versions WHERE analysis_id=?",
                                  (analysis_id_v,)).fetchone():
                raise ConflictError("分析版本编号已存在")
            protocols = connection.execute(
                "SELECT * FROM trial_protocols WHERE cultivar_id=? AND season=?",
                (cultivar_id, season),
            ).fetchall()
            if not protocols:
                raise NotFoundError("该品种季节没有试验方案")
            # 同一品种季节取最新版本方案作为阈值/窗口基准
            protocol_row = max(protocols, key=lambda row: row["version"])
            protocol = json.loads(protocol_row["payload_json"])
            all_protocol_ids = [row["protocol_id"] for row in protocols]
            placeholders = ",".join("?" for _ in all_protocol_ids)
            thresholds = {**protocol["thresholds"], **(parameters or {}).get("thresholds", {})}
            params = {"thresholds": thresholds,
                      "required_reviewers": protocol["required_reviewers"],
                      "protocol_id": protocol_row["protocol_id"],
                      "protocol_version": protocol_row["version"],
                      "all_protocol_ids": all_protocol_ids,
                      "extra": parameters or {}}
            code_version = (parameters or {}).get("code_version", protocol["code_version"])

            # 地块可能承接该品种季节的任一版本方案，按地块去重
            plot_rows = connection.execute(
                f"SELECT p.*, pp.lot_id, pp.protocol_id AS linked_protocol FROM protocol_plots pp "
                f"JOIN plots p ON pp.plot_id=p.plot_id WHERE pp.protocol_id IN ({placeholders})",
                all_protocol_ids,
            ).fetchall()
            seen_plots: set[str] = set()
            deduped_plots = []
            for plot_row in plot_rows:
                if plot_row["plot_id"] in seen_plots:
                    continue
                seen_plots.add(plot_row["plot_id"])
                deduped_plots.append(plot_row)
            plot_rows = deduped_plots
            rule_rows = connection.execute(
                "SELECT * FROM exclusion_rules WHERE active=1"
            ).fetchall()
            rules_by_target: dict[str, list] = {"observation": [], "plot": []}
            for rule_row in rule_rows:
                rules_by_target[rule_row["target_kind"]].append(
                    {"rule_id": rule_row["rule_id"], "name": rule_row["name"],
                     "expression": json.loads(rule_row["expression"])})

            snapshot = []
            for plot_row in plot_rows:
                snapshot.append(self._evaluate_plot(connection, plot_row,
                                                    all_protocol_ids, protocol,
                                                    thresholds, rules_by_target))

            effective_material = [
                {"plot_id": item["plot_id"], "effective": item["effective"],
                 "included": item["included_observation_ids"],
                 "excluded": item["excluded"]}
                for item in snapshot
            ]
            effective_set_hash = digest(effective_material)
            summary = self._build_summary(snapshot, thresholds)
            connection.execute(
                "INSERT INTO analysis_versions(analysis_id,cultivar_id,season,code_version,"
                "parameters_json,effective_set_hash,summary_json,frozen_at,created_by) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (analysis_id_v, cultivar_id, season, code_version, canonical_json(params),
                 effective_set_hash, canonical_json(summary), self._now(), actor_id),
            )
            for item in snapshot:
                connection.execute(
                    "INSERT INTO analysis_plot_effectiveness(analysis_id,plot_id,effective,"
                    "reasons_json,included_observation_ids_json,excluded_observation_ids_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (analysis_id_v, item["plot_id"], 1 if item["effective"] else 0,
                     canonical_json(item["reasons"]),
                     canonical_json(item["included_observation_ids"]),
                     canonical_json(item["excluded"])),
                )
            append_event(connection, actor_id=actor_id, action="analysis.frozen",
                         resource_type="analysis_version", resource_id=analysis_id_v,
                         detail={"cultivar_id": cultivar_id, "season": season,
                                 "code_version": code_version,
                                 "effective_set_hash": effective_set_hash,
                                 "plot_count": len(snapshot),
                                 "effective_plot_count": sum(1 for s in snapshot if s["effective"])},
                         occurred_at=self._now())
            return {"resource_type": "analysis_version", "resource_id": analysis_id_v,
                    "analysis_id": analysis_id_v, "effective_set_hash": effective_set_hash,
                    "code_version": code_version, "plot_count": len(snapshot),
                    "summary": summary}

        return self._idem(request_id=request_id, action="freeze_analysis",
                          payload=payload, create=create)

    def _evaluate_plot(self, connection, plot_row, all_protocol_ids, protocol,
                       thresholds, rules_by_target) -> dict[str, Any]:
        plot_id = plot_row["plot_id"]
        region_id = plot_row["region_id"]
        conditions = json.loads(plot_row["conditions_json"])
        reasons: list[str] = []
        included: list[str] = []
        excluded: list[dict[str, Any]] = []

        placeholders = ",".join("?" for _ in all_protocol_ids)
        observations = connection.execute(
            f"SELECT * FROM observations WHERE plot_id=? AND protocol_id IN ({placeholders})",
            [plot_id, *all_protocol_ids],
        ).fetchall()

        open_reviews = {
            row["observation_id"]: row["reason"] for row in connection.execute(
                "SELECT observation_id, reason FROM review_queue WHERE status='open'")
        }

        for obs in observations:
            obs_id = obs["observation_id"]
            if obs["status"] == "excluded":
                excluded.append({"observation_id": obs_id, "reason": "review_rejected",
                                 "metric": obs["metric"], "observed_at": obs["observed_at"]})
                continue
            if obs["status"] == "pending" or obs_id in open_reviews:
                open_reason = open_reviews.get(obs_id)
                excluded.append({"observation_id": obs_id,
                                 "reason": f"pending_review:{obs['pending_reason'] or open_reason or ''}",
                                 "metric": obs["metric"], "observed_at": obs["observed_at"]})
                continue
            context = {"value": obs["value"], "metric": obs["metric"],
                       "observed_at": obs["observed_at"], "plot": conditions,
                       **json.loads(obs["payload_json"])}
            matched_rule = None
            for rule in rules_by_target["observation"]:
                try:
                    if rules.evaluate_predicate(rule["expression"], context):
                        matched_rule = rule
                        break
                except ValueError:
                    matched_rule = None
            if matched_rule:
                excluded.append({"observation_id": obs_id, "reason": "rule_excluded",
                                 "rule_id": matched_rule["rule_id"], "rule_name": matched_rule["name"],
                                 "metric": obs["metric"], "observed_at": obs["observed_at"]})
                continue
            included.append(obs_id)

        # 地块级排除规则
        plot_context = {"plot": conditions, "region_id": region_id,
                        "included_count": len(included), "excluded_count": len(excluded)}
        for rule in rules_by_target["plot"]:
            try:
                if rules.evaluate_predicate(rule["expression"], plot_context):
                    reasons.append(f"rule_excluded:{rule['rule_id']}({rule['name']})")
            except ValueError:
                pass

        # 灾害性气象：与地块生育区间重叠即偏离原方案
        span = self._plot_growing_span(connection, plot_id, protocol)
        if span is not None:
            weather_rows = connection.execute(
                "SELECT * FROM weather_intervals WHERE region_id=? AND disaster=1", (region_id,)
            ).fetchall()
            overlaps = rules.weather_overlap([dict(row) for row in weather_rows], span[0], span[1])
            if overlaps:
                reasons.append("disaster_weather:" + ",".join(row["weather_id"] for row in overlaps))

        # 农事越窗（如播种不在方案窗口），仅检查本地块承接本品种季节方案的操作
        linked_protocol = plot_row["linked_protocol"]
        for operation in connection.execute(
                "SELECT * FROM farm_operations WHERE plot_id=? AND protocol_id=? AND operation_type='sowing'",
                (plot_id, linked_protocol)):
            window_key = rules.WINDOW_BY_OPERATION.get("sowing")
            if not rules.in_window(protocol["windows"], window_key,
                                   rules.parse_instant(operation["event_at"])):
                reasons.append(f"operation_out_of_window:sowing:{operation['event_at']}")

        # 种子批次撤回
        if plot_row["lot_id"]:
            lot = connection.execute("SELECT * FROM seed_lots WHERE lot_id=?",
                                     (plot_row["lot_id"],)).fetchone()
            if lot is not None and lot["status"] == "withdrawn":
                reasons.append(f"lot_withdrawn:{lot['lot_id']}")

        if not included:
            reasons.append("no_valid_observations")

        return {"plot_id": plot_id, "region_id": region_id, "lot_id": plot_row["lot_id"],
                "effective": not reasons, "reasons": reasons,
                "included_observation_ids": included, "excluded": excluded,
                "conditions_hash": digest(conditions)}

    def _plot_growing_span(self, connection, plot_id: str, protocol: dict[str, Any]):
        sowing = connection.execute(
            "SELECT event_at FROM farm_operations WHERE plot_id=? AND operation_type='sowing' "
            "ORDER BY event_at LIMIT 1", (plot_id,)).fetchone()
        windows = protocol["windows"]
        start_text = sowing["event_at"] if sowing else windows.get("observe_from") or windows.get("sow_from")
        end_text = (windows.get("harvest_to") or windows.get("observe_to"))
        observations = connection.execute(
            "SELECT MIN(observed_at) AS min_at, MAX(observed_at) AS max_at FROM observations WHERE plot_id=?",
            (plot_id,)).fetchone()
        if start_text is None:
            start_text = observations["min_at"]
        if end_text is None:
            end_text = observations["max_at"]
        if start_text is None or end_text is None:
            return None
        start_dt = rules.parse_instant(start_text)
        end_dt = rules.parse_instant(end_text)
        if end_dt < start_dt:
            end_dt = start_dt
        return start_dt, end_dt

    def _build_summary(self, snapshot: list[dict[str, Any]], thresholds: dict[str, Any]) -> dict[str, Any]:
        return {"regions": sorted({item["region_id"] for item in snapshot}),
                "plot_count": len(snapshot),
                "effective_plot_count": sum(1 for item in snapshot if item["effective"])}

    def get_analysis(self, analysis_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM analysis_versions WHERE analysis_id=?", (analysis_id,)).fetchone()
        if row is None:
            raise NotFoundError("分析版本不存在")
        plots = []
        for plot_row in self.database.connection.execute(
                "SELECT * FROM analysis_plot_effectiveness WHERE analysis_id=? ORDER BY plot_id",
                (analysis_id,)):
            plots.append({"plot_id": plot_row["plot_id"],
                          "effective": bool(plot_row["effective"]),
                          "reasons": json.loads(plot_row["reasons_json"]),
                          "included_observation_ids": json.loads(plot_row["included_observation_ids_json"]),
                          "excluded": json.loads(plot_row["excluded_observation_ids_json"])})
        return {"analysis_id": analysis_id, "cultivar_id": row["cultivar_id"],
                "season": row["season"], "code_version": row["code_version"],
                "parameters": json.loads(row["parameters_json"]),
                "effective_set_hash": row["effective_set_hash"],
                "summary": json.loads(row["summary_json"]),
                "frozen_at": row["frozen_at"], "created_by": row["created_by"], "plots": plots}

    # ------------------------------------------------------------------ 裁定与并发确认

    def _analysis_region_basis(self, connection, analysis_row, region_id: str) -> dict[str, Any]:
        analysis_id = analysis_row["analysis_id"]
        params = json.loads(analysis_row["parameters_json"])
        thresholds = params["thresholds"]
        plot_rows = connection.execute(
            "SELECT ape.*, p.region_id FROM analysis_plot_effectiveness ape "
            "JOIN plots p ON p.plot_id=ape.plot_id WHERE ape.analysis_id=? AND p.region_id=? ORDER BY ape.plot_id",
            (analysis_id, region_id)).fetchall()
        if not plot_rows:
            raise NotFoundError("该区域在冻结分析中没有试验地块")
        effective_plots: list[dict[str, Any]] = []
        excluded_plots: list[dict[str, Any]] = []
        metric_values: dict[str, list[float]] = {}
        for plot_row in plot_rows:
            excluded = json.loads(plot_row["excluded_observation_ids_json"])
            included_ids = json.loads(plot_row["included_observation_ids_json"])
            if plot_row["effective"]:
                included_observations = []
                for obs_id in included_ids:
                    obs = connection.execute(
                        "SELECT metric,observed_at,value,value_text,device_sn,status "
                        "FROM observations WHERE observation_id=?", (obs_id,)).fetchone()
                    if obs is None:
                        continue
                    included_observations.append(
                        {"observation_id": obs_id, "metric": obs["metric"],
                         "observed_at": obs["observed_at"], "value": obs["value"],
                         "value_text": obs["value_text"], "device_sn": obs["device_sn"]})
                    if obs["value"] is not None:
                        metric_values.setdefault(obs["metric"], []).append(obs["value"])
                effective_plots.append({"plot_id": plot_row["plot_id"],
                                        "included_observations": included_observations})
            else:
                excluded_plots.append({"plot_id": plot_row["plot_id"],
                                       "reasons": json.loads(plot_row["reasons_json"]),
                                       "excluded_observations": excluded})
        means = {metric: sum(values) / len(values) for metric, values in metric_values.items() if values}
        decision, decision_reasons = rules.compute_decision(
            effective_plot_count=len(effective_plots),
            excluded_plot_count=len(excluded_plots),
            mean_yield=means.get("yield"),
            mean_disease=means.get("disease_rate"),
            mean_seedling=means.get("seedling_rate"),
            thresholds=thresholds)
        return {"region_id": region_id, "decision": decision, "reasons": decision_reasons,
                "effective_plots": effective_plots, "excluded_plots": excluded_plots,
                "metrics": means, "thresholds": thresholds}

    def propose_ruling(self, *, request_id: str, actor_id: str, ruling_id: str,
                       region_id: str, cultivar_id: str, analysis_id: str,
                       amendment_kind: str | None = None,
                       amendment_reason: str | None = None) -> dict[str, Any]:
        if amendment_kind is not None and amendment_kind not in AMENDMENT_KINDS:
            raise ValidationError(f"amendment_kind 仅支持 {sorted(AMENDMENT_KINDS)}")
        payload = {"actor_id": actor_id, "ruling_id": ruling_id, "region_id": region_id,
                   "cultivar_id": cultivar_id, "analysis_id": analysis_id,
                   "amendment_kind": amendment_kind}

        def create(connection):
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            ruling_id_v = self._identifier(ruling_id, "ruling_id")
            analysis_row = self._must_exist(connection, "analysis_versions", "analysis_id",
                                            analysis_id, "分析版本不存在")
            cultivar_id_v = self._identifier(cultivar_id, "cultivar_id")
            if analysis_row["cultivar_id"] != cultivar_id_v:
                raise ValidationError("分析版本的品种与裁定品种不一致")
            region_id_v = self._identifier(region_id, "region_id")
            basis = self._analysis_region_basis(connection, analysis_row, region_id_v)
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS next_version FROM region_rulings "
                "WHERE region_id=? AND cultivar_id=?", (region_id_v, cultivar_id_v)).fetchone()
            version = version_row["next_version"]
            if version > 1 and amendment_kind is None:
                raise ConflictError("已存在历史裁定时，后继裁定必须声明 amendment_kind（迟到更正/灾害豁免/批次撤回）")
            if version == 1 and amendment_kind is not None:
                raise ValidationError("首次裁定不能是修订类型")
            basis_full = {"analysis_id": analysis_id,
                          "effective_set_hash": analysis_row["effective_set_hash"],
                          "code_version": analysis_row["code_version"],
                          "frozen_at": analysis_row["frozen_at"], **basis}
            basis_hash = digest(basis_full)
            connection.execute(
                "INSERT INTO region_rulings(ruling_id,region_id,cultivar_id,decision,version,"
                "analysis_id,basis_json,basis_hash,status,amendment_kind,amendment_reason,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,'proposed',?,?,?,?)",
                (ruling_id_v, region_id_v, cultivar_id_v, basis["decision"], version, analysis_id,
                 canonical_json(basis_full), basis_hash, amendment_kind, amendment_reason,
                 actor_id, self._now()),
            )
            append_event(connection, actor_id=actor_id, action="ruling.proposed",
                         resource_type="region_ruling", resource_id=ruling_id_v,
                         detail={"region_id": region_id_v, "cultivar_id": cultivar_id_v,
                                 "decision": basis["decision"], "version": version,
                                 "analysis_id": analysis_id, "basis_hash": basis_hash,
                                 "amendment_kind": amendment_kind},
                         occurred_at=self._now())
            required = json.loads(analysis_row["parameters_json"])["required_reviewers"]
            return {"resource_type": "region_ruling", "resource_id": ruling_id_v,
                    "ruling_id": ruling_id_v, "version": version,
                    "decision": basis["decision"], "basis_hash": basis_hash,
                    "status": "proposed", "required_reviewers": required}

        return self._idem(request_id=request_id, action="propose_ruling",
                          payload=payload, create=create)

    def confirm_ruling(self, *, actor_id: str, ruling_id: str) -> dict[str, Any]:
        """审查人确认。并发调用下保证同一 区域×品种 只有一个生效版本。"""

        # 先读出所属 区域×品种，对该键加锁，防止同键的不同裁定并发同时生效
        probe = self.database.connection.execute(
            "SELECT region_id, cultivar_id FROM region_rulings WHERE ruling_id=?",
            (ruling_id,)).fetchone()
        if probe is None:
            raise NotFoundError("裁定不存在")
        lock_key = f"ruling:{probe['region_id']}:{probe['cultivar_id']}"
        with self.database.key_lock(lock_key):
            with self.database.transaction(immediate=True) as connection:
                actor = self._actor(connection, actor_id)
                self._require(actor, "admin", "reviewer")
                ruling = self._must_exist(connection, "region_rulings", "ruling_id",
                                          ruling_id, "裁定不存在")
                if ruling["status"] != "proposed":
                    raise ConflictError(f"裁定当前状态为 {ruling['status']}，不能再确认")
                already = connection.execute(
                    "SELECT 1 FROM ruling_confirms WHERE ruling_id=? AND reviewer_id=?",
                    (ruling_id, actor_id)).fetchone()
                if already is None:
                    connection.execute(
                        "INSERT INTO ruling_confirms(ruling_id,reviewer_id,confirmed_at) VALUES(?,?,?)",
                        (ruling_id, actor_id, self._now()),
                    )
                    append_event(connection, actor_id=actor_id, action="ruling.confirmed",
                                 resource_type="region_ruling", resource_id=ruling_id,
                                 detail={"version": ruling["version"],
                                         "region_id": ruling["region_id"],
                                         "cultivar_id": ruling["cultivar_id"]},
                                 occurred_at=self._now())
                count_row = connection.execute(
                    "SELECT COUNT(*) AS count FROM ruling_confirms WHERE ruling_id=?",
                    (ruling_id,)).fetchone()
                required = json.loads(connection.execute(
                    "SELECT parameters_json FROM analysis_versions WHERE analysis_id=?",
                    (ruling["analysis_id"],)).fetchone()["parameters_json"])["required_reviewers"]
                effective = False
                if count_row["count"] >= required:
                    # 先让同键旧生效版本让位（保留记录、仅标记被接替），再置新版本生效，
                    # 以满足“同一 区域×品种 至多一个生效版本”的唯一约束。
                    connection.execute(
                        "UPDATE region_rulings SET status='superseded', superseded_by=? "
                        "WHERE region_id=? AND cultivar_id=? AND status='effective' AND ruling_id<>?",
                        (ruling_id, ruling["region_id"], ruling["cultivar_id"], ruling_id),
                    )
                    connection.execute(
                        "UPDATE region_rulings SET status='effective', effective_at=? WHERE ruling_id=?",
                        (self._now(), ruling_id),
                    )
                    effective = True
                    append_event(connection, actor_id=actor_id, action="ruling.effective",
                                 resource_type="region_ruling", resource_id=ruling_id,
                                 detail={"region_id": ruling["region_id"],
                                         "cultivar_id": ruling["cultivar_id"],
                                         "version": ruling["version"],
                                         "decision": ruling["decision"],
                                         "analysis_id": ruling["analysis_id"]},
                                 occurred_at=self._now())
                return {"ruling_id": ruling_id, "confirmations": count_row["count"],
                        "required_reviewers": required, "already_confirmed": already is not None,
                        "status": "effective" if effective else "proposed", "effective": effective}

    def get_ruling(self, ruling_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM region_rulings WHERE ruling_id=?", (ruling_id,)).fetchone()
        if row is None:
            raise NotFoundError("裁定不存在")
        confirms = [dict(r) for r in self.database.connection.execute(
            "SELECT reviewer_id,confirmed_at FROM ruling_confirms WHERE ruling_id=? ORDER BY confirmed_at",
            (ruling_id,))]
        return self._ruling_payload(row, confirms)

    def _ruling_payload(self, row, confirms: list[dict[str, Any]]) -> dict[str, Any]:
        return {"ruling_id": row["ruling_id"], "region_id": row["region_id"],
                "cultivar_id": row["cultivar_id"], "decision": row["decision"],
                "version": row["version"], "analysis_id": row["analysis_id"],
                "basis": json.loads(row["basis_json"]), "basis_hash": row["basis_hash"],
                "status": row["status"], "superseded_by": row["superseded_by"],
                "amendment_kind": row["amendment_kind"],
                "amendment_reason": row["amendment_reason"],
                "created_by": row["created_by"], "created_at": row["created_at"],
                "effective_at": row["effective_at"], "confirmations": confirms}

    def list_rulings(self, region_id: str, cultivar_id: str) -> dict[str, Any]:
        rows = self.database.connection.execute(
            "SELECT * FROM region_rulings WHERE region_id=? AND cultivar_id=? ORDER BY version",
            (region_id, cultivar_id)).fetchall()
        return {"items": [self._ruling_payload(row, [
            dict(r) for r in self.database.connection.execute(
                "SELECT reviewer_id,confirmed_at FROM ruling_confirms WHERE ruling_id=? ORDER BY confirmed_at",
                (row["ruling_id"],))]) for row in rows]}

    def explain_region(self, region_id: str, cultivar_id: str) -> dict[str, Any]:
        """解释某区域为何推荐/限制/拒绝某品种，一直追到使用与排除的地块和观测。"""

        row = self.database.connection.execute(
            "SELECT * FROM region_rulings WHERE region_id=? AND cultivar_id=? AND status='effective'",
            (region_id, cultivar_id)).fetchone()
        if row is None:
            raise NotFoundError("该区域品种组合当前没有生效裁定")
        ruling = self._ruling_payload(row, [dict(r) for r in self.database.connection.execute(
            "SELECT reviewer_id,confirmed_at FROM ruling_confirms WHERE ruling_id=? ORDER BY confirmed_at",
            (row["ruling_id"],))])
        chain = [dict(r) for r in self.database.connection.execute(
            "SELECT ruling_id,version,decision,status,amendment_kind,amendment_reason,"
            "analysis_id,created_at,effective_at FROM region_rulings "
            "WHERE region_id=? AND cultivar_id=? ORDER BY version", (region_id, cultivar_id))]
        plot_evidence = []
        for plot in ruling["basis"]["effective_plots"]:
            plot_evidence.append({"plot_id": plot["plot_id"], "used": True,
                                  "included_observations": plot["included_observations"]})
        for plot in ruling["basis"]["excluded_plots"]:
            plot_evidence.append({"plot_id": plot["plot_id"], "used": False,
                                  "reasons": plot["reasons"],
                                  "excluded_observations": plot["excluded_observations"]})
        return {"region_id": region_id, "cultivar_id": cultivar_id,
                "decision": ruling["decision"],
                "ruling_id": ruling["ruling_id"], "ruling_version": ruling["version"],
                "effective_at": ruling["effective_at"],
                "analysis_id": ruling["analysis_id"],
                "code_version": ruling["basis"]["code_version"],
                "frozen_at": ruling["basis"]["frozen_at"],
                "effective_set_hash": ruling["basis"]["effective_set_hash"],
                "reasons": ruling["basis"]["reasons"],
                "metrics": ruling["basis"]["metrics"],
                "thresholds": ruling["basis"]["thresholds"],
                "plot_evidence": plot_evidence,
                "amendment": {"kind": ruling["amendment_kind"], "reason": ruling["amendment_reason"]},
                "version_chain": chain,
                "confirmed_by": [item["reviewer_id"] for item in ruling["confirmations"]]}

    def list_open_reviews(self) -> dict[str, Any]:
        rows = self.database.connection.execute(
            "SELECT r.*, o.plot_id, o.metric, o.observed_at FROM review_queue r "
            "JOIN observations o ON o.observation_id=r.observation_id "
            "WHERE r.status='open' ORDER BY r.created_at").fetchall()
        return {"items": [{"review_id": row["review_id"], "observation_id": row["observation_id"],
                           "reason": row["reason"], "plot_id": row["plot_id"], "metric": row["metric"],
                           "observed_at": row["observed_at"], "created_at": row["created_at"]}
                          for row in rows]}
