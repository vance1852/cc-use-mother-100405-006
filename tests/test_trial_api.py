import unittest

from science_strategy_foundation.api import route
from science_strategy_foundation.storage import Database
from science_strategy_foundation.trial_service import TrialService


WINDOWS = {
    "sow_from": "2026-04-01T00:00:00Z", "sow_to": "2026-04-30T23:59:59Z",
    "observe_from": "2026-05-01T00:00:00Z", "observe_to": "2026-09-30T23:59:59Z",
    "harvest_from": "2026-09-01T00:00:00Z", "harvest_to": "2026-10-15T23:59:59Z",
}
THRESHOLDS = {"min_effective_plots": 2, "yield_recommend": 1.0, "yield_restrict": 0.8,
              "disease_restrict": 0.2, "seedling_restrict": 0.7}
H = {"X-Actor-Id": "op1"}


class TrialApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = TrialService(self.database)

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="op1"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor})

    def _seed(self):
        self._call("POST", "/organizations",
                   {"request_id": "org", "organization_id": "o1", "name": "总站"}, "bootstrap")
        self._call("POST", "/actors",
                   {"request_id": "a1", "new_actor_id": "a1", "display_name": "管理员",
                    "role": "admin", "organization_id": "o1"}, "bootstrap")
        self._call("POST", "/actors",
                   {"request_id": "op", "new_actor_id": "op1", "display_name": "基层员",
                    "role": "operator", "organization_id": "o1"}, "a1")
        self._call("POST", "/actors",
                   {"request_id": "r1", "new_actor_id": "rv1", "display_name": "审查甲",
                    "role": "reviewer", "organization_id": "o1"}, "a1")
        self._call("POST", "/actors",
                   {"request_id": "r2", "new_actor_id": "rv2", "display_name": "审查乙",
                    "role": "reviewer", "organization_id": "o1"}, "a1")
        self._call("POST", "/sites",
                   {"request_id": "site", "site_id": "s1", "organization_id": "o1",
                    "name": "中心", "timezone_name": "Asia/Shanghai"})
        self._call("POST", "/seed-lots",
                   {"request_id": "lot", "lot_id": "lot1", "cultivar_id": "cv1",
                    "cultivar_name": "耐旱一号", "lot_code": "2026-A"})
        self._call("POST", "/protocols",
                   {"request_id": "pr", "protocol_id": "pr1", "cultivar_id": "cv1",
                    "season": "2026", "windows": WINDOWS, "thresholds": THRESHOLDS}, "rv1")
        for plot in ("p1", "p2"):
            self._call("POST", "/plots",
                       {"request_id": f"plot-{plot}", "plot_id": plot, "site_id": "s1",
                        "region_id": "north", "name": plot, "conditions": {"soil": "loam"}})
            self._call("POST", "/protocol-plots",
                       {"request_id": f"assign-{plot}", "protocol_id": "pr1", "plot_id": plot,
                        "lot_id": "lot1"})
            self._call("POST", "/farm-operations",
                       {"request_id": f"sow-{plot}", "protocol_id": "pr1", "plot_id": plot,
                        "operation_type": "sowing", "event_at": "2026-04-10T00:00:00Z",
                        "data": {"depth_cm": 3}})
            self._call("POST", "/devices",
                       {"request_id": f"dev-{plot}", "device_sn": f"sn-{plot}",
                        "device_type": "sensor", "plot_id": plot})
            self._call("POST", "/calibrations",
                       {"request_id": f"cal-{plot}", "calibration_id": f"cal-{plot}",
                        "device_sn": f"sn-{plot}", "valid_from": "2026-01-01T00:00:00Z"})
            for metric, value, at in (("yield", 1.1, "2026-09-10T00:00:00Z"),
                                      ("disease_rate", 0.05, "2026-08-10T00:00:00Z"),
                                      ("seedling_rate", 0.9, "2026-05-20T00:00:00Z")):
                self._call("POST", "/observations",
                           {"request_id": f"obs-{plot}-{metric}", "device_sn": f"sn-{plot}",
                            "plot_id": plot, "protocol_id": "pr1", "metric": metric,
                            "observed_at": at, "value": value})

    def test_full_ruling_flow_over_http_and_replay_status(self):
        self._seed()
        # 幂等重放返回 200
        status, replay = self._call("POST", "/observations",
                                    {"request_id": "obs-p1-yield", "device_sn": "sn-p1",
                                     "plot_id": "p1", "protocol_id": "pr1", "metric": "yield",
                                     "observed_at": "2026-09-10T00:00:00Z", "value": 1.1})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        status, frozen = self._call("POST", "/analyses",
                                    {"request_id": "an", "analysis_id": "an1", "cultivar_id": "cv1",
                                     "season": "2026"}, "rv1")
        self.assertEqual(201, status)
        self.assertTrue(frozen["effective_set_hash"])

        status, proposed = self._call("POST", "/rulings",
                                      {"request_id": "rul", "ruling_id": "rul1", "region_id": "north",
                                       "cultivar_id": "cv1", "analysis_id": "an1"}, "rv1")
        self.assertEqual(201, status)
        self.assertEqual(2, proposed["required_reviewers"])

        status, first = self._call("POST", "/rulings/rul1/confirm", {}, "rv1")
        self.assertEqual(200, status)
        self.assertFalse(first["effective"])
        status, second = self._call("POST", "/rulings/rul1/confirm", {}, "rv2")
        self.assertTrue(second["effective"])

        status, explanation = self._call("GET", "/regions/north/cultivars/cv1", None, "rv1")
        self.assertEqual(200, status)
        self.assertEqual("recommend", explanation["decision"])
        self.assertEqual("trial-rules-1", explanation["code_version"])
        self.assertEqual(2, len(explanation["plot_evidence"]))

    def test_pending_observation_listed_in_review_queue(self):
        self._seed()
        status, pending = self._call("POST", "/observations",
                                     {"request_id": "oow", "device_sn": "sn-p1", "plot_id": "p1",
                                      "protocol_id": "pr1", "metric": "yield",
                                      "observed_at": "2026-02-01T00:00:00Z", "value": 1.0})
        self.assertEqual(201, status)
        self.assertEqual("pending", pending["status"])
        status, reviews = self._call("GET", "/reviews", None, "rv1")
        self.assertEqual(200, status)
        self.assertTrue(any(item["observation_id"] == pending["observation_id"]
                            for item in reviews["items"]))

    def test_frozen_analysis_is_retrievable(self):
        self._seed()
        self._call("POST", "/analyses",
                   {"request_id": "an", "analysis_id": "an1", "cultivar_id": "cv1",
                    "season": "2026"}, "rv1")
        status, detail = self._call("GET", "/analyses/an1", None, "rv1")
        self.assertEqual(200, status)
        self.assertEqual(2, len(detail["plots"]))
        self.assertTrue(detail["plots"][0]["included_observation_ids"])


if __name__ == "__main__":
    unittest.main()
