import threading
import unittest
from datetime import datetime, timezone

from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from science_strategy_foundation.storage import Database
from science_strategy_foundation.trial_service import TrialService

WINDOWS = {
    "sow_from": "2026-04-01T00:00:00Z", "sow_to": "2026-04-30T23:59:59Z",
    "observe_from": "2026-05-01T00:00:00Z", "observe_to": "2026-09-30T23:59:59Z",
    "harvest_from": "2026-09-01T00:00:00Z", "harvest_to": "2026-10-15T23:59:59Z",
}
THRESHOLDS = {"min_effective_plots": 2, "yield_recommend": 1.0, "yield_restrict": 0.8,
              "disease_restrict": 0.2, "seedling_restrict": 0.7}


class TrialTestCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = TrialService(self.database,
                                    FixedClock(datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)))
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="省农技推广总站")
        s.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                         display_name="基层员", role="operator", organization_id="o1")
        for index, name in enumerate(["甲", "乙", "丙", "丁"], start=1):
            s.register_actor(request_id=f"rv{index}", actor_id="a1", new_actor_id=f"rv{index}",
                             display_name=f"审查{name}", role="reviewer", organization_id="o1")
        s.register_site(request_id="site", actor_id="op1", site_id="s1",
                        organization_id="o1", name="试验中心", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------------ helpers

    def _plot(self, request_id, plot_id, region, conditions=None):
        return self.service.register_plot(
            request_id=request_id, actor_id="op1", plot_id=plot_id, site_id="s1",
            region_id=region, name=plot_id, conditions=conditions or {"soil": "loam"})

    def _lot(self, lot_id="lot1", cultivar="cv1"):
        return self.service.register_seed_lot(
            request_id=f"lot-{lot_id}", actor_id="op1", lot_id=lot_id, cultivar_id=cultivar,
            cultivar_name="耐旱一号", lot_code="2026-A")

    def _protocol(self, protocol_id="pr1", cultivar="cv1", required_reviewers=2):
        return self.service.register_protocol(
            request_id=f"proto-{protocol_id}", actor_id="rv1", protocol_id=protocol_id,
            cultivar_id=cultivar, season="2026", windows=WINDOWS, thresholds=THRESHOLDS,
            required_reviewers=required_reviewers)

    def _sow_and_device(self, plot_id, protocol_id="pr1", valid_to=None):
        self.service.record_operation(
            request_id=f"sow-{plot_id}", actor_id="op1", protocol_id=protocol_id,
            plot_id=plot_id, operation_type="sowing", event_at="2026-04-10T00:00:00Z",
            data={"depth_cm": 3})
        self.service.register_device(
            request_id=f"dev-{plot_id}", actor_id="op1", device_sn=f"sn-{plot_id}",
            device_type="sensor", plot_id=plot_id)
        self.service.record_calibration(
            request_id=f"cal-{plot_id}", actor_id="op1", calibration_id=f"cal-{plot_id}",
            device_sn=f"sn-{plot_id}", valid_from="2026-01-01T00:00:00Z", valid_to=valid_to)

    def _metrics(self, plot_id, yield_value, disease=0.05, seedling=0.9, protocol_id="pr1", tag=""):
        for metric, value, at in (("yield", yield_value, "2026-09-10T00:00:00Z"),
                                  ("disease_rate", disease, "2026-08-10T00:00:00Z"),
                                  ("seedling_rate", seedling, "2026-05-20T00:00:00Z")):
            self.service.ingest_observation(
                request_id=f"obs-{plot_id}-{metric}-{tag}", actor_id="op1",
                device_sn=f"sn-{plot_id}", plot_id=plot_id, protocol_id=protocol_id,
                metric=metric, observed_at=at, value=value)

    def _good_trial(self, region="north", plots=("p1", "p2"), yields=(1.1, 1.05)):
        self._lot()
        self._protocol()
        for plot_id, yield_value in zip(plots, yields):
            self._plot(f"plot-{plot_id}", plot_id, region)
            self.service.assign_plot(
                request_id=f"assign-{plot_id}", actor_id="op1", protocol_id="pr1",
                plot_id=plot_id, lot_id="lot1")
            self._sow_and_device(plot_id)
            self._metrics(plot_id, yield_value)

    def _freeze_propose_effective(self, region="north", analysis_id="an1", ruling_id="rul1"):
        self.service.freeze_analysis(request_id=f"freeze-{analysis_id}", actor_id="rv1",
                                     analysis_id=analysis_id, cultivar_id="cv1", season="2026")
        self.service.propose_ruling(request_id=f"propose-{ruling_id}", actor_id="rv1",
                                    ruling_id=ruling_id, region_id=region, cultivar_id="cv1",
                                    analysis_id=analysis_id)
        self.service.confirm_ruling(actor_id="rv1", ruling_id=ruling_id)
        return self.service.confirm_ruling(actor_id="rv2", ruling_id=ruling_id)

    # ------------------------------------------------------------------ 证据链与结论

    def test_good_trial_recommends_and_explains_down_to_observations(self):
        self._good_trial()
        result = self._freeze_propose_effective()
        self.assertTrue(result["effective"])
        explanation = self.service.explain_region("north", "cv1")
        self.assertEqual("recommend", explanation["decision"])
        self.assertEqual("an1", explanation["analysis_id"])
        self.assertEqual("trial-rules-1", explanation["code_version"])
        self.assertEqual(2, len([p for p in explanation["plot_evidence"] if p["used"]]))
        for plot in explanation["plot_evidence"]:
            self.assertIn("included_observations", plot)
            self.assertTrue(plot["included_observations"])
        self.assertEqual({"rv1", "rv2"}, set(explanation["confirmed_by"]))

    def test_restrict_decision_when_yield_between_thresholds(self):
        self._good_trial(region="mid", plots=("p3", "p4"), yields=(0.85, 0.95))
        self._freeze_propose_effective(region="mid", analysis_id="an-mid", ruling_id="rul-mid")
        self.assertEqual("restrict", self.service.explain_region("mid", "cv1")["decision"])

    def test_reject_when_too_few_effective_plots(self):
        self._lot()
        self._protocol()
        # 只有一个有效地块，低于 min_effective_plots=2
        self._plot("plot-only", "p9", "sparse")
        self.service.assign_plot(request_id="assign-only", actor_id="op1",
                                 protocol_id="pr1", plot_id="p9", lot_id="lot1")
        self._sow_and_device("p9")
        self._metrics("p9", 1.2)
        self.service.freeze_analysis(request_id="fr", actor_id="rv1", analysis_id="an9",
                                     cultivar_id="cv1", season="2026")
        self.service.propose_ruling(request_id="pr", actor_id="rv1", ruling_id="rul9",
                                    region_id="sparse", cultivar_id="cv1", analysis_id="an9")
        self.service.confirm_ruling(actor_id="rv1", ruling_id="rul9")
        self.service.confirm_ruling(actor_id="rv2", ruling_id="rul9")
        self.assertEqual("reject", self.service.explain_region("sparse", "cv1")["decision"])

    # ------------------------------------------------------------------ 离线归并与待审

    def test_duplicate_upload_by_device_serial_does_not_add_sample(self):
        self._good_trial()
        before = self.service.ingest_observation(
            request_id="dup-1", actor_id="op1", device_sn="sn-p1", plot_id="p1",
            protocol_id="pr1", metric="yield", observed_at="2026-09-11T00:00:00Z", value=1.1)
        after = self.service.ingest_observation(
            request_id="dup-2", actor_id="op1", device_sn="sn-p1", plot_id="p1",
            protocol_id="pr1", metric="yield", observed_at="2026-09-11T00:00:00Z", value=1.1)
        self.assertFalse(before["duplicate"])
        self.assertTrue(after["duplicate"])
        self.assertEqual(before["observation_id"], after["observation_id"])
        count = self.database.connection.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]
        # 每个地块 3 个指标 × 2 地块 + 1 个新时间点 = 7，重复上报不新增
        self.assertEqual(7, count)

    def test_duplicate_with_different_value_is_conflict_not_silent_overwrite(self):
        self._good_trial()
        with self.assertRaises(ConflictError):
            self.service.ingest_observation(
                request_id="dup-changed", actor_id="op1", device_sn="sn-p1", plot_id="p1",
                protocol_id="pr1", metric="yield", observed_at="2026-09-10T00:00:00Z", value=9.9)

    def test_out_of_window_out_of_order_and_calibration_go_to_pending(self):
        self._lot()
        self._protocol()
        self._plot("plot-p", "pend", "north")
        self.service.assign_plot(request_id="assign-p", actor_id="op1", protocol_id="pr1",
                                 plot_id="pend", lot_id="lot1")
        # 校准在 6 月失效
        self._sow_and_device("pend", valid_to="2026-06-01T00:00:00Z")
        # 先上报 9 月的有效观测，再上报更早的同指标 -> 越序
        first = self.service.ingest_observation(
            request_id="o-late", actor_id="op1", device_sn="sn-pend", plot_id="pend",
            protocol_id="pr1", metric="yield", observed_at="2026-09-10T00:00:00Z", value=1.0)
        self.assertIn("calibration_expired", first["pending_reasons"])
        second = self.service.ingest_observation(
            request_id="o-early", actor_id="op1", device_sn="sn-pend", plot_id="pend",
            protocol_id="pr1", metric="yield", observed_at="2026-08-01T00:00:00Z", value=1.0)
        self.assertIn("calibration_expired", second["pending_reasons"])
        # 超窗观测
        oow = self.service.ingest_observation(
            request_id="o-oow", actor_id="op1", device_sn="sn-pend", plot_id="pend",
            protocol_id="pr1", metric="yield", observed_at="2026-03-01T00:00:00Z", value=1.0)
        self.assertIn("out_of_window", oow["pending_reasons"])
        # 数据没有被丢弃，全部进入待审队列
        open_reviews = self.service.list_open_reviews()["items"]
        self.assertEqual(3, len(open_reviews))
        reasons = {item["reason"] for item in open_reviews}
        self.assertTrue(any("calibration_expired" in r for r in reasons))
        self.assertTrue(any("out_of_window" in r for r in reasons))

    def test_accepted_pending_observation_becomes_effective(self):
        self._lot()
        self._protocol()
        self._plot("plot-q", "pqq", "north")
        self.service.assign_plot(request_id="assign-q", actor_id="op1", protocol_id="pr1",
                                 plot_id="pqq", lot_id="lot1")
        self._sow_and_device("pqq", valid_to="2026-06-01T00:00:00Z")
        pending = self.service.ingest_observation(
            request_id="o-p", actor_id="op1", device_sn="sn-pqq", plot_id="pqq",
            protocol_id="pr1", metric="yield", observed_at="2026-09-10T00:00:00Z", value=1.0)
        review_id = pending["review_id"]
        resolved = self.service.resolve_review(request_id="rr", actor_id="rv1",
                                               review_id=review_id, resolution="accept",
                                               note="补校准证书已核实")
        self.assertEqual("valid", resolved["observation_status"])
        self.assertEqual(0, len(self.service.list_open_reviews()["items"]))

    def test_rejected_pending_observation_is_excluded_from_analysis(self):
        self._good_trial()
        # 给 p2 增加一条越序观测，审查拒绝后不得进入有效集合
        pending = self.service.ingest_observation(
            request_id="o-rej", actor_id="op1", device_sn="sn-p2", plot_id="p2",
            protocol_id="pr1", metric="yield", observed_at="2026-03-15T00:00:00Z", value=5.0)
        self.assertEqual("pending", pending["status"])
        self.service.resolve_review(request_id="rr-rej", actor_id="rv1",
                                    review_id=pending["review_id"], resolution="reject")
        self.service.freeze_analysis(request_id="fr2", actor_id="rv1", analysis_id="an2",
                                     cultivar_id="cv1", season="2026")
        detail = self.service.get_analysis("an2")
        p2 = next(plot for plot in detail["plots"] if plot["plot_id"] == "p2")
        self.assertTrue(all(item["reason"] == "review_rejected" for item in p2["excluded"]))

    def test_exclusion_rule_removes_matching_observation(self):
        self._good_trial()
        # p1 再上报一条病株率 0.5（在观测窗口内、校准有效，直接成为有效观测）
        self.service.ingest_observation(
            request_id="o-high-disease", actor_id="op1", device_sn="sn-p1", plot_id="p1",
            protocol_id="pr1", metric="disease_rate", observed_at="2026-09-15T00:00:00Z", value=0.5)
        self.service.register_exclusion_rule(
            request_id="rule-high", actor_id="rv1", rule_id="rule-dis", name="高病株率",
            rule_type="static", target_kind="observation",
            expression={"all": [{"op": "==", "field": "metric", "value": "disease_rate"},
                                 {"op": ">", "field": "value", "value": 0.3}]})
        self.service.freeze_analysis(request_id="fr3", actor_id="rv1", analysis_id="an3",
                                     cultivar_id="cv1", season="2026")
        p1 = next(plot for plot in self.service.get_analysis("an3")["plots"] if plot["plot_id"] == "p1")
        self.assertTrue(any(item.get("rule_id") == "rule-dis" for item in p1["excluded"]))

    # ------------------------------------------------------------------ 冻结与后继裁定

    def test_disaster_weather_excludes_plot_and_successor_requires_amendment(self):
        self._good_trial()
        self._freeze_propose_effective()
        first = self.service.explain_region("north", "cv1")
        self.assertEqual("recommend", first["decision"])
        first_hash = self.service.get_analysis("an1")["effective_set_hash"]

        # 灾害性暴雨：补充登记后，地块偏离原方案；新冻结分析结论改变
        self.service.record_weather(
            request_id="w-storm", actor_id="op1", weather_id="w1", region_id="north",
            started_at="2026-09-05T00:00:00Z", ended_at="2026-09-12T00:00:00Z",
            severity="storm", disaster=True)
        self.service.freeze_analysis(request_id="fr-am", actor_id="rv1", analysis_id="an-am",
                                     cultivar_id="cv1", season="2026")
        # 不声明修订类型 -> 拒绝，已发布范围不能被悄悄改写
        with self.assertRaises(ConflictError):
            self.service.propose_ruling(
                request_id="rul-silent", actor_id="rv1", ruling_id="rul-silent",
                region_id="north", cultivar_id="cv1", analysis_id="an-am")
        # 声明灾害豁免后继裁定
        self.service.propose_ruling(
            request_id="rul-am", actor_id="rv1", ruling_id="rul-am", region_id="north",
            cultivar_id="cv1", analysis_id="an-am", amendment_kind="disaster_exemption",
            amendment_reason="灌浆期连续暴雨，申请灾害豁免重裁")
        self.service.confirm_ruling(actor_id="rv1", ruling_id="rul-am")
        self.service.confirm_ruling(actor_id="rv2", ruling_id="rul-am")
        explanation = self.service.explain_region("north", "cv1")
        self.assertEqual("rul-am", explanation["ruling_id"])
        self.assertEqual("disaster_exemption", explanation["amendment"]["kind"])
        self.assertEqual(2, len(explanation["version_chain"]))
        self.assertEqual("effective", explanation["version_chain"][-1]["status"])
        self.assertEqual("superseded", explanation["version_chain"][0]["status"])
        # 旧冻结分析保持不变
        self.assertEqual(first_hash, self.service.get_analysis("an1")["effective_set_hash"])
        chain = self.service.list_rulings("north", "cv1")["items"]
        self.assertEqual(["rul1", "rul-am"], [item["ruling_id"] for item in chain])

    def test_lot_withdrawal_only_takes_effect_through_successor_ruling(self):
        self._good_trial()
        self._freeze_propose_effective()
        # 已发布的推荐仍然有效；撤回批次后必须形成后继裁定
        self.assertEqual("recommend", self.service.explain_region("north", "cv1")["decision"])
        self.service.withdraw_seed_lot(request_id="wd", actor_id="rv1", lot_id="lot1",
                                       reason="发芽率复检不达标")
        self.service.freeze_analysis(request_id="fr-wd", actor_id="rv1", analysis_id="an-wd",
                                     cultivar_id="cv1", season="2026")
        detail = self.service.get_analysis("an-wd")
        self.assertTrue(any(any("lot_withdrawn" in reason for reason in plot["reasons"])
                            for plot in detail["plots"]))
        self.service.propose_ruling(
            request_id="rul-wd", actor_id="rv1", ruling_id="rul-wd", region_id="north",
            cultivar_id="cv1", analysis_id="an-wd", amendment_kind="lot_withdrawal",
            amendment_reason="批次 lot1 撤回")
        self.service.confirm_ruling(actor_id="rv1", ruling_id="rul-wd")
        self.service.confirm_ruling(actor_id="rv2", ruling_id="rul-wd")
        explanation = self.service.explain_region("north", "cv1")
        self.assertEqual("reject", explanation["decision"])
        self.assertEqual("lot_withdrawal", explanation["amendment"]["kind"])

    # ------------------------------------------------------------------ 并发与权限

    def test_late_correction_forms_successor_ruling(self):
        self._good_trial()
        self._freeze_propose_effective()
        self.assertEqual("recommend", self.service.explain_region("north", "cv1")["decision"])

        # 发布后一条迟到的高病株率观察到达（时间在窗口内、校准有效、不越序）
        self.service.ingest_observation(
            request_id="o-late-disease", actor_id="op1", device_sn="sn-p1", plot_id="p1",
            protocol_id="pr1", metric="disease_rate", observed_at="2026-09-20T00:00:00Z",
            value=0.6)
        self.service.freeze_analysis(request_id="fr-lc", actor_id="rv1", analysis_id="an-lc",
                                     cultivar_id="cv1", season="2026")
        # 迟到更正必须声明修订类型
        self.service.propose_ruling(
            request_id="rul-lc", actor_id="rv1", ruling_id="rul-lc", region_id="north",
            cultivar_id="cv1", analysis_id="an-lc", amendment_kind="late_correction",
            amendment_reason="p1 病害数据迟到更正，平均病株率超阈值")
        self.service.confirm_ruling(actor_id="rv1", ruling_id="rul-lc")
        self.service.confirm_ruling(actor_id="rv2", ruling_id="rul-lc")
        explanation = self.service.explain_region("north", "cv1")
        self.assertEqual("rul-lc", explanation["ruling_id"])
        self.assertEqual("late_correction", explanation["amendment"]["kind"])
        self.assertEqual("reject", explanation["decision"])
        self.assertEqual([1, 2], [item["version"] for item in explanation["version_chain"]])

    def test_concurrent_repeated_confirmations_keep_single_effective(self):
        self._good_trial()
        self.service.freeze_analysis(request_id="fr-c", actor_id="rv1", analysis_id="an-c",
                                     cultivar_id="cv1", season="2026")
        self.service.propose_ruling(request_id="rul-c", actor_id="rv1", ruling_id="rul-c",
                                    region_id="north", cultivar_id="cv1", analysis_id="an-c")

        def confirm(reviewer):
            try:
                for _ in range(10):
                    self.service.confirm_ruling(actor_id=reviewer, ruling_id="rul-c")
            except ConflictError:
                # 裁定已生效后重复确认报冲突属于预期，关键是只有一个生效版本
                pass

        threads = [threading.Thread(target=confirm, args=(name,))
                   for name in ("rv1", "rv2", "rv3")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        effective = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM region_rulings WHERE region_id='north' "
            "AND cultivar_id='cv1' AND status='effective'").fetchone()["c"]
        self.assertEqual(1, effective)
        confirmations = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM ruling_confirms WHERE ruling_id='rul-c'").fetchone()["c"]
        self.assertEqual(2, confirmations)

    def test_two_concurrent_rulings_for_same_key_end_with_one_effective(self):
        self._good_trial()
        # v1 生效
        self._freeze_propose_effective()
        # 形成 v2 候选（灾害豁免），与 v1 由不同审查人并发确认
        self.service.record_weather(
            request_id="w2", actor_id="op1", weather_id="w2", region_id="north",
            started_at="2026-09-05T00:00:00Z", ended_at="2026-09-12T00:00:00Z",
            severity="storm", disaster=True)
        self.service.freeze_analysis(request_id="fr2c", actor_id="rv1", analysis_id="an2c",
                                     cultivar_id="cv1", season="2026")
        self.service.propose_ruling(
            request_id="rul2c", actor_id="rv1", ruling_id="rul2c", region_id="north",
            cultivar_id="cv1", analysis_id="an2c", amendment_kind="disaster_exemption",
            amendment_reason="暴雨重裁")

        def confirm(ruling, reviewers):
            for reviewer in reviewers:
                try:
                    self.service.confirm_ruling(actor_id=reviewer, ruling_id=ruling)
                except ConflictError:
                    # 竞态下裁定已生效/被接替，后续确认报冲突属于预期
                    pass

        t1 = threading.Thread(target=confirm, args=("rul1", ["rv1", "rv2"]))
        t2 = threading.Thread(target=confirm, args=("rul2c", ["rv3", "rv4"]))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        effective = self.database.connection.execute(
            "SELECT ruling_id FROM region_rulings WHERE region_id='north' AND cultivar_id='cv1' "
            "AND status='effective'").fetchall()
        self.assertEqual(1, len(effective))

    def test_operator_cannot_confirm_ruling(self):
        self._good_trial()
        self.service.freeze_analysis(request_id="fr-p", actor_id="rv1", analysis_id="an-p",
                                     cultivar_id="cv1", season="2026")
        self.service.propose_ruling(request_id="rul-p", actor_id="rv1", ruling_id="rul-p",
                                    region_id="north", cultivar_id="cv1", analysis_id="an-p")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_ruling(actor_id="op1", ruling_id="rul-p")

    def test_audit_chain_covers_trial_events(self):
        self._good_trial()
        self._freeze_propose_effective()
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        actions = {event["action"] for event in self.service.audit_events()}
        self.assertIn("seed_lot.registered", actions)
        self.assertIn("observation.ingested", actions)
        self.assertIn("analysis.frozen", actions)
        self.assertIn("ruling.effective", actions)
        explanation = self.service.explain_region("north", "cv1")
        # 解释引用冻结分析版本与有效集合哈希
        self.assertTrue(explanation["effective_set_hash"])


if __name__ == "__main__":
    unittest.main()
