"""运行农业试验裁定服务的离线端到端验收。

场景：省级农技推广总站为耐旱品种登记多地点试验证据链；连续暴雨导致部分
试验田偏离方案，基层断网期间补录的观测按设备序列归并；专家组冻结分析
版本并发布区域裁定，随后品种批次撤回以后继裁定取代已发布结论。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from science_strategy_foundation.clock import FixedClock

from .service import AgriTrialService
from .storage import AgriDatabase


def run() -> dict[str, object]:
    """执行一条完整裁定链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = AgriDatabase(Path(directory) / "acceptance.sqlite3")
        service = AgriTrialService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))

        # 组织、操作者与场所
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-agri", name="省级农技推广总站")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="a-admin",
                               display_name="总站管理员", role="admin", organization_id="org-agri")
        service.register_actor(request_id="req-field", actor_id="a-admin", new_actor_id="a-field",
                               display_name="基层农技员", role="operator", organization_id="org-agri")
        service.register_actor(request_id="req-rev1", actor_id="a-admin", new_actor_id="a-rev-1",
                               display_name="专家组审查人一", role="reviewer", organization_id="org-agri")
        service.register_actor(request_id="req-rev2", actor_id="a-admin", new_actor_id="a-rev-2",
                               display_name="专家组审查人二", role="reviewer", organization_id="org-agri")
        service.register_site(request_id="req-site", actor_id="a-admin", site_id="site-north",
                              organization_id="org-agri", name="旱作试验站", timezone_name="Asia/Shanghai")

        # 证据链：种子批次、地块、试验方案、排除规则、设备校准
        service.register_seed_batch(request_id="req-batch", actor_id="a-field", batch_id="batch-h1",
                                    variety_id="variety-hd-01", origin="甘肃育种基地",
                                    metadata={"germination_rate": 0.93})
        for plot_id, name in (("plot-a", "一号试验田"), ("plot-b", "二号试验田"), ("plot-c", "三号试验田")):
            service.register_plot(request_id=f"req-{plot_id}", actor_id="a-field", plot_id=plot_id,
                                  site_id="site-north", name=name, soil_type="壤土",
                                  area_hectares=0.5, conditions={"irrigation": "rainfed"})
        service.register_protocol(request_id="req-proto", actor_id="a-admin", protocol_id="proto-hd-01",
                                  variety_id="variety-hd-01", region_id="region-hexi",
                                  sowing_window_start="2026-05-01T00:00:00Z",
                                  sowing_window_end="2026-05-31T23:59:59Z",
                                  observation_window_start="2026-06-01T00:00:00Z",
                                  observation_window_end="2026-09-30T23:59:59Z",
                                  min_valid_trials=2, confirmations_required=2)
        service.register_exclusion_rule(request_id="req-rule-dev", actor_id="a-admin", rule_id="rule-dev",
                                        protocol_id="proto-hd-01", rule_type="protocol_deviation")
        service.register_exclusion_rule(request_id="req-rule-min", actor_id="a-admin", rule_id="rule-min",
                                        protocol_id="proto-hd-01", rule_type="min_observations",
                                        definition={"count": 2})
        service.register_device_calibration(request_id="req-cal-1", actor_id="a-field",
                                            calibration_id="cal-1", device_id="dev-01",
                                            calibrated_at="2026-05-01T00:00:00Z",
                                            valid_until="2026-09-30T23:59:59Z")

        # 三处试验与播种农事操作
        for trial_id, plot_id in (("tr-a", "plot-a"), ("tr-b", "plot-b"), ("tr-c", "plot-c")):
            service.create_trial(request_id=f"req-{trial_id}", actor_id="a-field", trial_id=trial_id,
                                 protocol_id="proto-hd-01", plot_id=plot_id, seed_batch_id="batch-h1")
            service.record_farming_operation(request_id=f"req-sow-{trial_id}", actor_id="a-field",
                                             trial_id=trial_id, op_type="sowing",
                                             performed_at="2026-05-10T08:00:00Z",
                                             detail={"seeding_rate": "18kg/ha"})

        # 连续暴雨：二号田获灾害豁免，三号田无豁免
        service.record_weather_interval(request_id="req-weather", actor_id="a-field",
                                        weather_id="weather-flood", site_id="site-north",
                                        started_at="2026-07-01T00:00:00Z",
                                        ended_at="2026-07-05T23:59:59Z",
                                        severity="disaster", summary="连续暴雨，田间积水")
        service.mark_trial_deviation(request_id="req-dev-b", actor_id="a-field", trial_id="tr-b",
                                     reason="暴雨淹没，偏离原方案", weather_id="weather-flood")
        service.mark_trial_deviation(request_id="req-dev-c", actor_id="a-field", trial_id="tr-c",
                                     reason="暴雨冲毁畦面，偏离原方案", weather_id="weather-flood")
        _, exemption = service.grant_disaster_exemption(request_id="req-exempt-b", actor_id="a-rev-1",
                                                        trial_id="tr-b", weather_id="weather-flood",
                                                        reason="灾害性气象豁免，数据仍可用于分析")

        # 断网期间补录的观测按设备序列归并
        first_batch = [
            {"device_seq": 1, "observed_at": "2026-07-10T09:00:00Z", "metric": "plant_height_cm", "value": 62.5},
            {"device_seq": 2, "observed_at": "2026-07-20T09:00:00Z", "metric": "plant_height_cm", "value": 71.0},
            {"device_seq": 3, "observed_at": "2026-08-01T09:00:00Z", "metric": "yield_kg_per_mu", "value": 402.0},
        ]
        _, ingest1 = service.ingest_observations(request_id="req-ing-1", actor_id="a-field",
                                                 device_id="dev-01", trial_id="tr-a", records=first_batch)
        _, ingest_replay = service.ingest_observations(request_id="req-ing-2", actor_id="a-field",
                                                       device_id="dev-01", trial_id="tr-a", records=first_batch)
        # 越序：序列 5 先合并，迟到的序列 4 进入待审
        service.ingest_observations(request_id="req-ing-3", actor_id="a-field", device_id="dev-01",
                                    trial_id="tr-a", records=[
                {"device_seq": 5, "observed_at": "2026-08-10T09:00:00Z",
                 "metric": "yield_kg_per_mu", "value": 410.0}])
        _, ingest_late = service.ingest_observations(request_id="req-ing-4", actor_id="a-field",
                                                     device_id="dev-01", trial_id="tr-a", records=[
                {"device_seq": 4, "observed_at": "2026-08-05T09:00:00Z",
                 "metric": "yield_kg_per_mu", "value": 405.0}])
        late_id = ingest_late["items"][0]["observation_id"]
        service.review_observation(request_id="req-review-late", actor_id="a-rev-1",
                                   observation_id=late_id, decision="accept", note="迟到更正，予以采纳")
        # 超窗与校准失效：进入待审而非丢弃
        _, ingest_window = service.ingest_observations(request_id="req-ing-5", actor_id="a-field",
                                                       device_id="dev-01", trial_id="tr-a", records=[
                {"device_seq": 6, "observed_at": "2026-10-02T09:00:00Z",
                 "metric": "yield_kg_per_mu", "value": 398.0}])
        service.review_observation(request_id="req-review-window", actor_id="a-rev-1",
                                   observation_id=ingest_window["items"][0]["observation_id"],
                                   decision="reject", note="超出观测窗口")
        _, ingest_uncal = service.ingest_observations(request_id="req-ing-6", actor_id="a-field",
                                                      device_id="dev-02", trial_id="tr-a", records=[
                {"device_seq": 1, "observed_at": "2026-08-15T09:00:00Z",
                 "metric": "disease_index", "value": 3.0}])
        service.register_device_calibration(request_id="req-cal-2", actor_id="a-field",
                                            calibration_id="cal-2", device_id="dev-02",
                                            calibrated_at="2026-05-01T00:00:00Z",
                                            valid_until="2026-09-30T23:59:59Z")
        service.review_observation(request_id="req-review-cal", actor_id="a-rev-1",
                                   observation_id=ingest_uncal["items"][0]["observation_id"],
                                   decision="accept", note="补登校准后采纳")
        # 二号田（豁免）与三号田（无豁免）的观测
        service.ingest_observations(request_id="req-ing-7", actor_id="a-field", device_id="dev-01",
                                    trial_id="tr-b", records=[
                {"device_seq": 7, "observed_at": "2026-08-12T09:00:00Z", "metric": "yield_kg_per_mu", "value": 388.0},
                {"device_seq": 8, "observed_at": "2026-08-22T09:00:00Z", "metric": "yield_kg_per_mu", "value": 391.0}])
        service.ingest_observations(request_id="req-ing-8", actor_id="a-field", device_id="dev-01",
                                    trial_id="tr-c", records=[
                {"device_seq": 9, "observed_at": "2026-08-12T09:30:00Z", "metric": "yield_kg_per_mu", "value": 350.0},
                {"device_seq": 10, "observed_at": "2026-08-22T09:30:00Z", "metric": "yield_kg_per_mu", "value": 355.0}])

        # 冻结分析版本并发布区域裁定
        _, frozen1 = service.freeze_analysis_version(request_id="req-freeze-1", actor_id="a-rev-1",
                                                     protocol_id="proto-hd-01")
        _, created = service.create_verdict(request_id="req-verdict-1", actor_id="a-rev-1",
                                            analysis_version_id=frozen1["version_id"],
                                            decision="recommended",
                                            rationale="两个有效试验均达标，建议在区域推广")
        verdict1 = created["verdict_id"]
        service.confirm_verdict(request_id="req-confirm-1", actor_id="a-rev-1", verdict_id=verdict1)
        _, confirmed = service.confirm_verdict(request_id="req-confirm-2", actor_id="a-rev-2",
                                               verdict_id=verdict1)
        explanation = service.explain_region("region-hexi", "variety-hd-01")

        # 品种批次撤回：只能以后继裁定取代，已发布范围保留在历史中
        _, withdrawal = service.withdraw_seed_batch(request_id="req-withdraw", actor_id="a-admin",
                                                    batch_id="batch-h1", reason="复检发现批次携带病害")
        _, frozen2 = service.freeze_analysis_version(request_id="req-freeze-2", actor_id="a-rev-1",
                                                     protocol_id="proto-hd-01")
        _, successor = service.create_verdict(request_id="req-verdict-2", actor_id="a-rev-1",
                                              analysis_version_id=frozen2["version_id"],
                                              decision="rejected",
                                              rationale="批次撤回后无有效试验，撤回推荐",
                                              supersedes_verdict_id=verdict1,
                                              successor_reason="batch_withdrawal")
        verdict2 = successor["verdict_id"]
        service.confirm_verdict(request_id="req-confirm-3", actor_id="a-rev-1", verdict_id=verdict2)
        service.confirm_verdict(request_id="req-confirm-4", actor_id="a-rev-2", verdict_id=verdict2)
        final = service.explain_region("region-hexi", "variety-hd-01")
        history = service.verdict_history("region-hexi", "variety-hd-01")

        valid, event_count = service.verify_audit()
        summary_a = service.trial_summary("tr-a")
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "ingest_first_accepted": ingest1["accepted"],
            "ingest_replay_duplicates": ingest_replay["duplicates"],
            "late_pending_reason": ingest_late["items"][0].get("reason"),
            "window_pending_reason": ingest_window["items"][0].get("reason"),
            "uncal_pending_reason": ingest_uncal["items"][0].get("reason"),
            "tr_a_accepted_observations": summary_a["observations"]["accepted"],
            "exemption_successor_hint": exemption["successor_required_for"],
            "frozen_valid_count": frozen1["valid_count"],
            "frozen_excluded_count": frozen1["excluded_count"],
            "first_verdict_status": confirmed["status"],
            "first_decision": explanation["decision"],
            "first_used_plots": sorted(entry["plot_id"] for entry in explanation["used_trials"]),
            "first_excluded_plots": sorted(entry["plot_id"] for entry in explanation["excluded_trials"]),
            "withdrawal_successor_hint": withdrawal["successor_required_for"],
            "final_decision": final["decision"],
            "history": [(item["decision"], item["status"]) for item in history],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
