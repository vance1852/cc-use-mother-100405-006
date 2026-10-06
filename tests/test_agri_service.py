import threading
import unittest
from datetime import datetime, timezone

from agri_trial_adjudication.service import AgriTrialService
from agri_trial_adjudication.storage import AgriDatabase
from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.errors import ConflictError, PermissionDenied, ValidationError


class AgriServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = AgriDatabase()
        self.service = AgriTrialService(
            self.database, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
        svc = self.service
        svc.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="o1", name="省农技推广总站")
        svc.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                           display_name="管理员", role="admin", organization_id="o1")
        svc.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                           display_name="基层农技员", role="operator", organization_id="o1")
        svc.register_actor(request_id="rev1", actor_id="a1", new_actor_id="r1",
                           display_name="审查人一", role="reviewer", organization_id="o1")
        svc.register_actor(request_id="rev2", actor_id="a1", new_actor_id="r2",
                           display_name="审查人二", role="reviewer", organization_id="o1")
        svc.register_site(request_id="site", actor_id="a1", site_id="s1",
                          organization_id="o1", name="旱作试验站", timezone_name="Asia/Shanghai")
        svc.register_seed_batch(request_id="batch", actor_id="op1", batch_id="b1",
                                variety_id="v1", origin="育种基地")
        svc.register_plot(request_id="plot", actor_id="op1", plot_id="p1", site_id="s1",
                          name="一号田", soil_type="壤土", area_hectares=0.5)
        svc.register_protocol(request_id="proto", actor_id="a1", protocol_id="pr1",
                              variety_id="v1", region_id="r-hexi",
                              sowing_window_start="2026-05-01T00:00:00Z",
                              sowing_window_end="2026-05-31T23:59:59Z",
                              observation_window_start="2026-06-01T00:00:00Z",
                              observation_window_end="2026-09-30T23:59:59Z",
                              min_valid_trials=1, confirmations_required=2)
        svc.register_exclusion_rule(request_id="rule", actor_id="a1", rule_id="ru1",
                                    protocol_id="pr1", rule_type="protocol_deviation")
        svc.register_device_calibration(request_id="cal", actor_id="op1", calibration_id="c1",
                                        device_id="d1", calibrated_at="2026-05-01T00:00:00Z",
                                        valid_until="2026-09-30T23:59:59Z")
        svc.create_trial(request_id="trial", actor_id="op1", trial_id="t1",
                         protocol_id="pr1", plot_id="p1", seed_batch_id="b1")

    def tearDown(self):
        self.database.close()

    def _record(self, seq, observed="2026-08-01T09:00:00Z", metric="yield", value=400.0):
        return {"device_seq": seq, "observed_at": observed, "metric": metric, "value": value}

    def _ingest(self, request_id, records, device="d1", trial="t1"):
        return self.service.ingest_observations(request_id=request_id, actor_id="op1",
                                                device_id=device, trial_id=trial, records=records)

    def _freeze(self, request_id="freeze"):
        return self.service.freeze_analysis_version(request_id=request_id, actor_id="r1",
                                                    protocol_id="pr1")

    def _publish_verdict(self, version_id, request_prefix="v", decision="recommended",
                         **kwargs):
        _, created = self.service.create_verdict(request_id=f"{request_prefix}-create",
                                                 actor_id="r1", analysis_version_id=version_id,
                                                 decision=decision, rationale="结论说明", **kwargs)
        verdict_id = created["verdict_id"]
        self.service.confirm_verdict(request_id=f"{request_prefix}-c1", actor_id="r1",
                                     verdict_id=verdict_id)
        receipt, result = self.service.confirm_verdict(request_id=f"{request_prefix}-c2",
                                                       actor_id="r2", verdict_id=verdict_id)
        return verdict_id, result

    # ---------- 离线归并 ----------

    def test_duplicate_upload_does_not_increase_samples(self):
        _, first = self._ingest("ing-1", [self._record(1), self._record(2)])
        _, second = self._ingest("ing-2", [self._record(1), self._record(2)])
        self.assertEqual(2, first["accepted"])
        self.assertEqual(0, second["accepted"])
        self.assertEqual(2, second["duplicates"])
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM observations WHERE status='accepted'").fetchone()["c"]
        self.assertEqual(2, count)

    def test_same_request_replays_ingest_response(self):
        receipt1, _ = self._ingest("ing-1", [self._record(1)])
        receipt2, response2 = self._ingest("ing-1", [self._record(1)])
        self.assertFalse(receipt1.replayed)
        self.assertTrue(receipt2.replayed)
        self.assertEqual(1, response2["accepted"])

    def test_out_of_order_record_goes_pending_not_discarded(self):
        self._ingest("ing-1", [self._record(1), self._record(2), self._record(5)])
        _, result = self._ingest("ing-2", [self._record(3)])
        self.assertEqual(1, result["pending"])
        self.assertEqual("out_of_order", result["items"][0]["reason"])
        pending = self.service.list_pending_observations("t1")
        self.assertEqual(1, len(pending))
        self.assertEqual(3, pending[0]["device_seq"])

    def test_out_of_window_record_goes_pending(self):
        _, result = self._ingest("ing-1", [self._record(1, observed="2026-10-05T09:00:00Z")])
        self.assertEqual("out_of_window", result["items"][0]["reason"])

    def test_calibration_expired_record_goes_pending(self):
        _, result = self._ingest("ing-1", [self._record(1)], device="d-uncalibrated")
        self.assertEqual("calibration_expired", result["items"][0]["reason"])
        self.service.register_device_calibration(
            request_id="cal-2", actor_id="op1", calibration_id="c2", device_id="d2",
            calibrated_at="2026-05-01T00:00:00Z", valid_until="2026-06-30T23:59:59Z")
        _, after = self._ingest("ing-2", [self._record(1)], device="d2")
        self.assertEqual("calibration_expired", after["items"][0]["reason"])

    def test_review_accepts_and_rejects_pending(self):
        _, result = self._ingest("ing-1", [self._record(1, observed="2026-10-05T09:00:00Z")])
        obs_id = result["items"][0]["observation_id"]
        receipt = self.service.review_observation(request_id="rev-1", actor_id="r1",
                                                  observation_id=obs_id, decision="accept")
        self.assertFalse(receipt.replayed)
        self.assertEqual(0, len(self.service.list_pending_observations("t1")))
        with self.assertRaises(ConflictError):
            self.service.review_observation(request_id="rev-2", actor_id="r1",
                                            observation_id=obs_id, decision="reject")

    def test_operator_cannot_review_observation(self):
        _, result = self._ingest("ing-1", [self._record(1, observed="2026-10-05T09:00:00Z")])
        with self.assertRaises(PermissionDenied):
            self.service.review_observation(request_id="rev-1", actor_id="op1",
                                            observation_id=result["items"][0]["observation_id"],
                                            decision="accept")

    # ---------- 偏离、豁免与排除 ----------

    def test_deviated_trial_excluded_without_exemption(self):
        self._ingest("ing-1", [self._record(1), self._record(2)])
        self.service.record_weather_interval(request_id="w1", actor_id="op1",
                                             weather_id="w-flood", site_id="s1",
                                             started_at="2026-07-01T00:00:00Z",
                                             ended_at="2026-07-05T00:00:00Z",
                                             severity="disaster", summary="连续暴雨")
        self.service.mark_trial_deviation(request_id="dev-1", actor_id="op1", trial_id="t1",
                                          reason="暴雨淹没", weather_id="w-flood")
        evaluation = self.service.evaluate_trials("pr1")
        self.assertEqual([], evaluation["valid"])
        self.assertEqual("t1", evaluation["excluded"][0]["trial_id"])
        self.assertIn("rule:ru1:protocol_deviation", evaluation["excluded"][0]["reasons"])

    def test_disaster_exemption_keeps_deviated_trial_valid(self):
        self._ingest("ing-1", [self._record(1), self._record(2)])
        self.service.record_weather_interval(request_id="w1", actor_id="op1",
                                             weather_id="w-flood", site_id="s1",
                                             started_at="2026-07-01T00:00:00Z",
                                             ended_at="2026-07-05T00:00:00Z",
                                             severity="disaster", summary="连续暴雨")
        self.service.mark_trial_deviation(request_id="dev-1", actor_id="op1", trial_id="t1",
                                          reason="暴雨淹没", weather_id="w-flood")
        _, exemption = self.service.grant_disaster_exemption(
            request_id="ex-1", actor_id="r1", trial_id="t1", weather_id="w-flood",
            reason="灾害性气象豁免")
        self.assertIsNone(exemption["successor_required_for"])
        evaluation = self.service.evaluate_trials("pr1")
        self.assertEqual(["t1"], [entry["trial_id"] for entry in evaluation["valid"]])
        self.assertEqual([], evaluation["excluded"])

    def test_normal_weather_cannot_support_exemption(self):
        self.service.record_weather_interval(request_id="w1", actor_id="op1",
                                             weather_id="w-normal", site_id="s1",
                                             started_at="2026-07-01T00:00:00Z",
                                             ended_at="2026-07-02T00:00:00Z",
                                             severity="normal", summary="阵雨")
        self.service.mark_trial_deviation(request_id="dev-1", actor_id="op1", trial_id="t1",
                                          reason="管理失误")
        with self.assertRaises(ValidationError):
            self.service.grant_disaster_exemption(request_id="ex-1", actor_id="r1",
                                                  trial_id="t1", weather_id="w-normal",
                                                  reason="尝试豁免")

    def test_withdrawn_batch_excludes_trials_and_blocks_new_ones(self):
        self._ingest("ing-1", [self._record(1)])
        _, withdrawal = self.service.withdraw_seed_batch(request_id="wd-1", actor_id="a1",
                                                         batch_id="b1", reason="携带病害")
        self.assertEqual([], withdrawal["successor_required_for"])
        evaluation = self.service.evaluate_trials("pr1")
        self.assertIn("seed_batch_withdrawn", evaluation["excluded"][0]["reasons"])
        with self.assertRaises(ValidationError):
            self.service.create_trial(request_id="trial-2", actor_id="op1", trial_id="t2",
                                      protocol_id="pr1", plot_id="p1", seed_batch_id="b1")

    def test_sowing_out_of_window_marks_deviation(self):
        receipt = self.service.record_farming_operation(
            request_id="sow-1", actor_id="op1", trial_id="t1", op_type="sowing",
            performed_at="2026-06-05T08:00:00Z")
        self.assertFalse(receipt.replayed)
        summary = self.service.trial_summary("t1")
        self.assertEqual("deviated", summary["status"])
        self.assertEqual("sowing_out_of_window", summary["deviation_reason"])

    # ---------- 冻结与裁定 ----------

    def test_verdict_requires_quorum(self):
        self._ingest("ing-1", [self._record(1)])
        _, frozen = self._freeze()
        _, created = self.service.create_verdict(request_id="v-create", actor_id="r1",
                                                 analysis_version_id=frozen["version_id"],
                                                 decision="recommended", rationale="达标")
        _, first = self.service.confirm_verdict(request_id="v-c1", actor_id="r1",
                                                verdict_id=created["verdict_id"])
        self.assertEqual("confirming", first["status"])
        _, second = self.service.confirm_verdict(request_id="v-c2", actor_id="r2",
                                                 verdict_id=created["verdict_id"])
        self.assertEqual("effective", second["status"])

    def test_same_reviewer_cannot_confirm_twice(self):
        self._ingest("ing-1", [self._record(1)])
        _, frozen = self._freeze()
        _, created = self.service.create_verdict(request_id="v-create", actor_id="r1",
                                                 analysis_version_id=frozen["version_id"],
                                                 decision="recommended", rationale="达标")
        self.service.confirm_verdict(request_id="v-c1", actor_id="r1",
                                     verdict_id=created["verdict_id"])
        with self.assertRaises(ConflictError):
            self.service.confirm_verdict(request_id="v-c2", actor_id="r1",
                                         verdict_id=created["verdict_id"])

    def test_new_verdict_must_succeed_effective_one(self):
        self._ingest("ing-1", [self._record(1)])
        _, frozen = self._freeze()
        verdict1, _ = self._publish_verdict(frozen["version_id"])
        _, frozen2 = self._freeze("freeze-2")
        with self.assertRaises(ConflictError):
            self.service.create_verdict(request_id="v2-create", actor_id="r1",
                                        analysis_version_id=frozen2["version_id"],
                                        decision="limited", rationale="试图平行发布")
        _, created = self.service.create_verdict(
            request_id="v3-create", actor_id="r1", analysis_version_id=frozen2["version_id"],
            decision="limited", rationale="迟到更正", supersedes_verdict_id=verdict1,
            successor_reason="late_correction")
        self.service.confirm_verdict(request_id="v3-c1", actor_id="r1",
                                     verdict_id=created["verdict_id"])
        _, result = self.service.confirm_verdict(request_id="v3-c2", actor_id="r2",
                                                 verdict_id=created["verdict_id"])
        self.assertEqual("effective", result["status"])
        self.assertEqual(verdict1, result["superseded_verdict_id"])
        history = self.service.verdict_history("r-hexi", "v1")
        self.assertEqual(["superseded", "effective"], [item["status"] for item in history])
        with self.assertRaises(ConflictError):
            self.service.confirm_verdict(request_id="v1-c3", actor_id="r2", verdict_id=verdict1)

    def test_successor_requires_legitimate_reason(self):
        self._ingest("ing-1", [self._record(1)])
        _, frozen = self._freeze()
        verdict1, _ = self._publish_verdict(frozen["version_id"])
        _, frozen2 = self._freeze("freeze-2")
        with self.assertRaises(ValidationError):
            self.service.create_verdict(request_id="v2-create", actor_id="r1",
                                        analysis_version_id=frozen2["version_id"],
                                        decision="limited", rationale="缺少后继原因",
                                        supersedes_verdict_id=verdict1)

    def test_verdict_must_reference_latest_frozen_version(self):
        self._ingest("ing-1", [self._record(1)])
        _, frozen1 = self._freeze()
        self._freeze("freeze-2")
        with self.assertRaises(ConflictError):
            self.service.create_verdict(request_id="v-create", actor_id="r1",
                                        analysis_version_id=frozen1["version_id"],
                                        decision="recommended", rationale="引用旧版本")

    def test_recommended_requires_min_valid_trials(self):
        _, frozen = self._freeze()
        with self.assertRaises(ValidationError):
            self.service.create_verdict(request_id="v-create", actor_id="r1",
                                        analysis_version_id=frozen["version_id"],
                                        decision="recommended", rationale="没有有效试验")
        _, created = self.service.create_verdict(request_id="v-create-2", actor_id="r1",
                                                 analysis_version_id=frozen["version_id"],
                                                 decision="rejected", rationale="无有效数据")
        self.assertEqual("confirming", created["status"])

    def test_concurrent_confirmations_keep_single_effective_version(self):
        self._ingest("ing-1", [self._record(1)])
        _, frozen = self._freeze()
        _, created = self.service.create_verdict(request_id="v-create", actor_id="r1",
                                                 analysis_version_id=frozen["version_id"],
                                                 decision="recommended", rationale="达标")
        verdict_id = created["verdict_id"]
        errors = []

        def confirm(reviewer, request_id):
            try:
                self.service.confirm_verdict(request_id=request_id, actor_id=reviewer,
                                             verdict_id=verdict_id)
            except Exception as exc:  # noqa: BLE001 - 收集并发异常用于断言
                errors.append(exc)

        threads = [threading.Thread(target=confirm, args=("r1", "cc-1")),
                   threading.Thread(target=confirm, args=("r2", "cc-2"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        effective = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM verdicts WHERE region_id='r-hexi' AND variety_id='v1' "
            "AND status='effective'").fetchone()["c"]
        self.assertEqual(1, effective)
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)

    # ---------- 解释与追溯 ----------

    def test_explanation_traces_used_and_excluded_plots(self):
        self.service.register_plot(request_id="plot-2", actor_id="op1", plot_id="p2",
                                   site_id="s1", name="二号田", soil_type="沙土",
                                   area_hectares=0.4)
        self.service.create_trial(request_id="trial-2", actor_id="op1", trial_id="t2",
                                  protocol_id="pr1", plot_id="p2", seed_batch_id="b1")
        self._ingest("ing-1", [self._record(1), self._record(2)])
        self._ingest("ing-2", [self._record(3), self._record(4)], trial="t2")
        self.service.mark_trial_deviation(request_id="dev-2", actor_id="op1", trial_id="t2",
                                          reason="暴雨淹没")
        _, frozen = self._freeze()
        verdict_id, _ = self._publish_verdict(frozen["version_id"])
        explanation = self.service.explain_region("r-hexi", "v1")
        self.assertEqual("recommended", explanation["decision"])
        self.assertEqual(["t1"], [entry["trial_id"] for entry in explanation["used_trials"]])
        self.assertEqual("p1", explanation["used_trials"][0]["plot"]["plot_id"])
        self.assertEqual(["t2"], [entry["trial_id"] for entry in explanation["excluded_trials"]])
        self.assertIn("rule:ru1:protocol_deviation",
                      explanation["excluded_trials"][0]["reasons"])
        self.assertEqual(2, len(explanation["confirmations"]))
        self.assertEqual(frozen["input_hash"], explanation["analysis"]["input_hash"])
        by_id = self.service.explain_verdict(verdict_id)
        self.assertEqual(explanation["verdict_id"], by_id["verdict_id"])

    def test_late_correction_forms_successor_verdict(self):
        self._ingest("ing-1", [self._record(1), self._record(5)])
        _, frozen1 = self._freeze()
        verdict1, _ = self._publish_verdict(frozen1["version_id"])
        _, late = self._ingest("ing-2", [self._record(3)])
        self.assertEqual("out_of_order", late["items"][0]["reason"])
        self.service.review_observation(request_id="rev-late", actor_id="r1",
                                        observation_id=late["items"][0]["observation_id"],
                                        decision="accept", note="迟到更正")
        _, frozen2 = self._freeze("freeze-2")
        self.assertNotEqual(frozen1["input_hash"], frozen2["input_hash"])
        verdict2, result = self._publish_verdict(
            frozen2["version_id"], request_prefix="v2", supersedes_verdict_id=verdict1,
            successor_reason="late_correction")
        self.assertEqual(verdict1, result["superseded_verdict_id"])
        final = self.service.explain_region("r-hexi", "v1")
        self.assertEqual(verdict2, final["verdict_id"])
        self.assertEqual("late_correction", final["successor_reason"])


if __name__ == "__main__":
    unittest.main()
