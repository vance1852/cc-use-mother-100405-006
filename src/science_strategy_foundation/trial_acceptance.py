"""多地点农业试验与推广裁定的离线端到端验收。

故事线：连续暴雨使部分试验田偏离原方案，基层人员断网期间补录播种与病害观察，
同一品种在汇总表中同时出现达标与无效结论。验收核对：
- 离线记录按设备序列归并，重复上报不增加样本；
- 越序、超窗、校准失效的数据进入待审而非丢弃；
- 区域结论引用冻结的分析版本与有效试验集合；
- 灾害性气象豁免只能形成后继裁定，不悄悄改变已发布范围；
- 多名审查人并发确认后只有唯一生效版本。
"""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError
from .storage import Database
from .trial_service import TrialService

WINDOWS = {
    "sow_from": "2026-04-01T00:00:00Z", "sow_to": "2026-04-30T23:59:59Z",
    "observe_from": "2026-05-01T00:00:00Z", "observe_to": "2026-09-30T23:59:59Z",
    "harvest_from": "2026-09-01T00:00:00Z", "harvest_to": "2026-10-15T23:59:59Z",
}
THRESHOLDS = {"min_effective_plots": 2, "yield_recommend": 1.0, "yield_restrict": 0.8,
              "disease_restrict": 0.2, "seedling_restrict": 0.7}


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "trial_acceptance.sqlite3")
        service = TrialService(database, FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)))

        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="org-001", name="省级农技推广总站")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="op", actor_id="admin-001", new_actor_id="op-001",
                               display_name="基层农技员", role="operator", organization_id="org-001")
        for request_id, actor_id, name in (
                ("rv1", "rv-001", "专家甲"), ("rv2", "rv-002", "专家乙"),
                ("rv3", "rv-003", "专家丙"), ("rv4", "rv-004", "专家丁")):
            service.register_actor(request_id=request_id, actor_id="admin-001", new_actor_id=actor_id,
                                   display_name=name, role="reviewer", organization_id="org-001")
        service.register_site(request_id="site", actor_id="op-001", site_id="site-001",
                              organization_id="org-001", name="丘陵雨养试验点",
                              timezone_name="Asia/Shanghai")

        # 种子批次、方案、四个地块（两个区域）
        service.register_seed_lot(request_id="lot", actor_id="op-001", lot_id="lot-2026a",
                                  cultivar_id="cv-dry1", cultivar_name="耐旱一号", lot_code="2026-A")
        service.register_protocol(request_id="proto", actor_id="rv-001", protocol_id="pr-001",
                                  cultivar_id="cv-dry1", season="2026", windows=WINDOWS,
                                  thresholds=THRESHOLDS, required_reviewers=2)
        plots = [("plot-n1", "north"), ("plot-n2", "north"),
                 ("plot-s1", "south"), ("plot-s2", "south")]
        for plot_id, region in plots:
            service.register_plot(request_id=f"reg-{plot_id}", actor_id="op-001", plot_id=plot_id,
                                  site_id="site-001", region_id=region, name=plot_id,
                                  conditions={"soil": "loam", "rainfed": True})
            service.assign_plot(request_id=f"assign-{plot_id}", actor_id="op-001",
                                protocol_id="pr-001", plot_id=plot_id, lot_id="lot-2026a")
            service.record_operation(request_id=f"sow-{plot_id}", actor_id="op-001",
                                     protocol_id="pr-001", plot_id=plot_id, operation_type="sowing",
                                     event_at="2026-04-10T00:00:00Z", data={"depth_cm": 3})
            service.register_device(request_id=f"dev-{plot_id}", actor_id="op-001",
                                    device_sn=f"sn-{plot_id}", device_type="sensor", plot_id=plot_id)

        # n1/n2/s1 校准覆盖全程；s2 校准在 6 月失效（暴雨后仪器送检未归）
        for plot_id, _ in plots:
            valid_to = "2026-06-01T00:00:00Z" if plot_id == "plot-s2" else None
            service.record_calibration(request_id=f"cal-{plot_id}", actor_id="op-001",
                                       calibration_id=f"cal-{plot_id}", device_sn=f"sn-{plot_id}",
                                       valid_from="2026-01-01T00:00:00Z", valid_to=valid_to)

        # 正常地块观测
        healthy = {"plot-n1": (1.12, 0.04, 0.92), "plot-n2": (1.05, 0.06, 0.88),
                   "plot-s1": (1.08, 0.05, 0.90)}
        for plot_id, (yield_v, disease_v, seed_v) in healthy.items():
            for metric, value, at in (("yield", yield_v, "2026-09-10T00:00:00Z"),
                                      ("disease_rate", disease_v, "2026-08-10T00:00:00Z"),
                                      ("seedling_rate", seed_v, "2026-05-20T00:00:00Z")):
                service.ingest_observation(request_id=f"obs-{plot_id}-{metric}", actor_id="op-001",
                                           device_sn=f"sn-{plot_id}", plot_id=plot_id,
                                           protocol_id="pr-001", metric=metric, observed_at=at,
                                           value=value)

        # s2：断网期间补录的病害观察（离线、校准失效 -> 待审）；同一条被基层端重复上报两次
        offline_once = service.ingest_observation(
            request_id="offline-s2-1", actor_id="op-001", device_sn="sn-plot-s2",
            plot_id="plot-s2", protocol_id="pr-001", metric="disease_rate",
            observed_at="2026-08-20T00:00:00Z", value=0.31, offline=True,
            client_recorded_at="2026-08-20T00:00:00Z")
        offline_twice = service.ingest_observation(
            request_id="offline-s2-2", actor_id="op-001", device_sn="sn-plot-s2",
            plot_id="plot-s2", protocol_id="pr-001", metric="disease_rate",
            observed_at="2026-08-20T00:00:00Z", value=0.31, offline=True,
            client_recorded_at="2026-08-20T00:00:00Z")
        # 一条超窗的产量观察
        out_of_window = service.ingest_observation(
            request_id="oow-s2", actor_id="op-001", device_sn="sn-plot-s2",
            plot_id="plot-s2", protocol_id="pr-001", metric="yield",
            observed_at="2026-03-05T00:00:00Z", value=2.0, offline=True,
            client_recorded_at="2026-03-05T00:00:00Z")

        # 首次冻结与发布：north 达标推荐；south 因 s2 数据全部待审、有效样本不足而拒绝
        def publish(ruling_id, reviewers):
            def tap(reviewer):
                try:
                    service.confirm_ruling(actor_id=reviewer, ruling_id=ruling_id)
                except ConflictError:
                    # 并发重复确认或裁定已生效后的点击，属于预期竞态
                    pass

            threads = [threading.Thread(target=tap, args=(reviewer,)) for reviewer in reviewers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        service.freeze_analysis(request_id="freeze-v1-north", actor_id="rv-001",
                                analysis_id="an-v1-north", cultivar_id="cv-dry1", season="2026")
        service.freeze_analysis(request_id="freeze-v1-south", actor_id="rv-001",
                                analysis_id="an-v1-south", cultivar_id="cv-dry1", season="2026")
        service.propose_ruling(request_id="propose-north-v1", actor_id="rv-001",
                               ruling_id="rul-north-v1", region_id="north",
                               cultivar_id="cv-dry1", analysis_id="an-v1-north")
        service.propose_ruling(request_id="propose-south-v1", actor_id="rv-001",
                               ruling_id="rul-south-v1", region_id="south",
                               cultivar_id="cv-dry1", analysis_id="an-v1-south")
        # 并发确认：同一名审查人重复点击 + 两名审查人同时确认
        publish("rul-north-v1", ["rv-001", "rv-001", "rv-002"])
        publish("rul-south-v1", ["rv-001", "rv-002"])

        first_north = service.explain_region("north", "cv-dry1")
        first_south = service.explain_region("south", "cv-dry1")
        # 记录已发布分析的有效集合哈希，后继裁定后必须保持不变
        frozen_south_v1_hash = service.get_analysis("an-v1-south")["effective_set_hash"]
        open_before_resolution = len(service.list_open_reviews()["items"])

        # 灾害性连续暴雨补登记（与 s2 生育区间重叠）
        service.record_weather(request_id="storm", actor_id="op-001", weather_id="w-storm",
                               region_id="south", started_at="2026-08-15T00:00:00Z",
                               ended_at="2026-08-25T00:00:00Z", severity="rainstorm", disaster=True)

        # 待审处置：离线病害观察经专家核实补校准后接受；超窗产量拒绝
        service.resolve_review(request_id="accept-offline", actor_id="rv-001",
                               review_id=offline_once["review_id"], resolution="accept",
                               note="仪器补校准证书核实，断网补录有效")
        service.resolve_review(request_id="reject-oow", actor_id="rv-001",
                               review_id=out_of_window["review_id"], resolution="reject",
                               note="播种前观测，超出方案窗口")

        # 新冻结分析：s1 受灾害天气偏离、s2 高病株率/校准失效，south 有效样本不足
        service.register_exclusion_rule(
            request_id="rule-disease", actor_id="rv-001", rule_id="rule-disease",
            name="高病株率剔除", rule_type="static", target_kind="observation",
            expression={"all": [{"op": "==", "field": "metric", "value": "disease_rate"},
                                 {"op": ">", "field": "value", "value": 0.3}]})
        service.freeze_analysis(request_id="freeze-v2-south", actor_id="rv-001",
                                analysis_id="an-v2-south", cultivar_id="cv-dry1", season="2026")

        # 不声明修订类型直接重裁 -> 必须被拒绝（不能悄悄改变已发布范围）
        silent_change_blocked = False
        try:
            service.propose_ruling(request_id="rul-silent", actor_id="rv-001",
                                   ruling_id="rul-south-silent", region_id="south",
                                   cultivar_id="cv-dry1", analysis_id="an-v2-south")
        except ConflictError:
            silent_change_blocked = True

        # 灾害豁免后继裁定
        service.propose_ruling(request_id="propose-south-v2", actor_id="rv-001",
                               ruling_id="rul-south-v2", region_id="south",
                               cultivar_id="cv-dry1", analysis_id="an-v2-south",
                               amendment_kind="disaster_exemption",
                               amendment_reason="8 月下旬连续暴雨，S2 仪器校准失效，申请灾害豁免重裁")
        publish("rul-south-v2", ["rv-003", "rv-004"])
        second_south = service.explain_region("south", "cv-dry1")

        audit_valid, audit_events = service.verify_audit()
        open_after_resolution = len(service.list_open_reviews()["items"])
        # 后继裁定后，首次冻结分析的有效集合哈希必须保持不变
        south_v1_hash_unchanged = (
            service.get_analysis("an-v1-south")["effective_set_hash"] == frozen_south_v1_hash)
        database.close()

        return {
            "status": "ok",
            "audit_valid": audit_valid,
            "audit_events": audit_events,
            "offline_duplicate_merged": bool(offline_twice["duplicate"])
            and offline_twice["observation_id"] == offline_once["observation_id"],
            # 两条问题数据都先进入待审队列（不丢弃），随后均被处置完毕
            "pending_queued_then_resolved": open_before_resolution == 2
            and open_after_resolution == 0,
            "first_north_decision": first_north["decision"],
            "first_south_decision": first_south["decision"],
            "first_analysis_unchanged": south_v1_hash_unchanged,
            "silent_change_blocked": silent_change_blocked,
            "second_south_decision": second_south["decision"],
            "second_south_amendment": second_south["amendment"]["kind"],
            "south_version_chain": [item["version"] for item in second_south["version_chain"]],
            "south_effective_plots": len(second_south["plot_evidence"]),
            "confirmed_by": second_south["confirmed_by"],
        }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = {
        "status": "ok", "audit_valid": True, "offline_duplicate_merged": True,
        "pending_queued_then_resolved": True, "first_north_decision": "recommend",
        "first_south_decision": "reject", "first_analysis_unchanged": True,
        "silent_change_blocked": True, "second_south_decision": "reject",
        "second_south_amendment": "disaster_exemption",
    }
    ok = all(result[key] == value for key, value in expected.items())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
