"""提供农业试验裁定服务不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from science_strategy_foundation.errors import DomainError, ValidationError

from .service import AgriTrialService
from .storage import AgriDatabase


def _receipt(status_receipt) -> tuple[int, dict[str, Any]]:
    return (200 if status_receipt.replayed else 201), status_receipt.__dict__


def _rich(receipt, response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), {**receipt.__dict__, "result": response}


def route(service: AgriTrialService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到农业试验裁定服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        # 基础登记能力（组织、操作者、场所）
        if method == "POST" and parsed.path == "/organizations":
            return _receipt(service.register_organization(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/actors":
            return _receipt(service.register_actor(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/sites":
            return _receipt(service.register_site(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        # 证据链登记
        if method == "POST" and parsed.path == "/seed-batches":
            return _receipt(service.register_seed_batch(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/seed-batch-withdrawals":
            return _rich(*service.withdraw_seed_batch(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/plots":
            return _receipt(service.register_plot(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/protocols":
            return _receipt(service.register_protocol(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/exclusion-rules":
            return _receipt(service.register_exclusion_rule(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/device-calibrations":
            return _receipt(service.register_device_calibration(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/trials":
            return _receipt(service.create_trial(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/farming-operations":
            return _receipt(service.record_farming_operation(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/weather-intervals":
            return _receipt(service.record_weather_interval(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/trial-deviations":
            return _receipt(service.mark_trial_deviation(actor_id=actor_id, **body))
        # 离线观测归并与待审
        if method == "POST" and parsed.path == "/observation-batches":
            return _rich(*service.ingest_observations(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/observation-reviews":
            return _receipt(service.review_observation(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/pending-observations":
            return 200, {"items": service.list_pending_observations(query.get("trial_id", [None])[0])}
        # 豁免、冻结与裁定
        if method == "POST" and parsed.path == "/disaster-exemptions":
            return _rich(*service.grant_disaster_exemption(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/analysis-versions":
            return _rich(*service.freeze_analysis_version(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/verdicts":
            return _rich(*service.create_verdict(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/verdict-confirmations":
            return _rich(*service.confirm_verdict(actor_id=actor_id, **body))
        # 结论解释与追溯
        if method == "GET" and parsed.path == "/trial-evaluation":
            protocol_id = query.get("protocol_id", [""])[0]
            if not protocol_id:
                raise ValidationError("protocol_id 不能为空")
            return 200, service.evaluate_trials(protocol_id)
        if method == "GET" and parsed.path == "/trial-summary":
            trial_id = query.get("trial_id", [""])[0]
            if not trial_id:
                raise ValidationError("trial_id 不能为空")
            return 200, service.trial_summary(trial_id)
        if method == "GET" and parsed.path == "/verdict-explanation":
            verdict_id = query.get("verdict_id", [""])[0]
            if not verdict_id:
                raise ValidationError("verdict_id 不能为空")
            return 200, service.explain_verdict(verdict_id)
        if method == "GET" and parsed.path == "/regional-verdict":
            region_id = query.get("region_id", [""])[0]
            variety_id = query.get("variety_id", [""])[0]
            if not region_id or not variety_id:
                raise ValidationError("region_id 和 variety_id 不能为空")
            return 200, service.explain_region(region_id, variety_id)
        if method == "GET" and parsed.path == "/verdict-history":
            region_id = query.get("region_id", [""])[0]
            variety_id = query.get("variety_id", [""])[0]
            if not region_id or not variety_id:
                raise ValidationError("region_id 和 variety_id 不能为空")
            return 200, {"items": service.verdict_history(region_id, variety_id)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: AgriTrialService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动多地点农业试验与推广裁定服务")
    parser.add_argument("--database", default="agri_trial.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = AgriDatabase(args.database)
    Handler.service = AgriTrialService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
