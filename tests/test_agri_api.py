import unittest

from agri_trial_adjudication.api import route
from agri_trial_adjudication.service import AgriTrialService
from agri_trial_adjudication.storage import AgriDatabase


class AgriApiTest(unittest.TestCase):
    def setUp(self):
        self.database = AgriDatabase()
        self.service = AgriTrialService(self.database)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "省农技推广总站"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "rev", "new_actor_id": "r1", "display_name": "审查人",
               "role": "reviewer", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "试验站", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "a1"})

    def tearDown(self):
        self.database.close()

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertTrue(payload["audit_valid"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_seed_batch_registration_and_replay(self):
        body = {"request_id": "b1", "batch_id": "batch-1", "variety_id": "v1",
                "origin": "育种基地"}
        status, first = route(self.service, "POST", "/seed-batches", body,
                              {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)
        status, second = route(self.service, "POST", "/seed-batches", body,
                               {"X-Actor-Id": "a1"})
        self.assertEqual(200, status)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["resource_id"], second["resource_id"])

    def test_missing_actor_is_rejected(self):
        status, payload = route(self.service, "POST", "/plots",
                                {"request_id": "p1", "plot_id": "p1", "site_id": "s1",
                                 "name": "一号田", "soil_type": "壤土", "area_hectares": 0.5},
                                {"X-Actor-Id": "ghost"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_regional_verdict_requires_parameters(self):
        status, payload = route(self.service, "GET", "/regional-verdict", None)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/protocols", {"request_id": "x"},
                                {"X-Actor-Id": "a1"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_full_chain_over_http(self):
        headers = {"X-Actor-Id": "a1"}
        route(self.service, "POST", "/seed-batches",
              {"request_id": "b1", "batch_id": "batch-1", "variety_id": "v1",
               "origin": "育种基地"}, headers)
        route(self.service, "POST", "/plots",
              {"request_id": "p1", "plot_id": "p1", "site_id": "s1", "name": "一号田",
               "soil_type": "壤土", "area_hectares": 0.5}, headers)
        route(self.service, "POST", "/protocols",
              {"request_id": "pr1", "protocol_id": "pr1", "variety_id": "v1",
               "region_id": "r1", "sowing_window_start": "2026-05-01T00:00:00Z",
               "sowing_window_end": "2026-05-31T23:59:59Z",
               "observation_window_start": "2026-06-01T00:00:00Z",
               "observation_window_end": "2026-09-30T23:59:59Z",
               "min_valid_trials": 1, "confirmations_required": 1}, headers)
        route(self.service, "POST", "/device-calibrations",
              {"request_id": "c1", "calibration_id": "c1", "device_id": "d1",
               "calibrated_at": "2026-05-01T00:00:00Z",
               "valid_until": "2026-09-30T23:59:59Z"}, headers)
        route(self.service, "POST", "/trials",
              {"request_id": "t1", "trial_id": "t1", "protocol_id": "pr1",
               "plot_id": "p1", "seed_batch_id": "batch-1"}, headers)
        status, ingest = route(self.service, "POST", "/observation-batches",
                               {"request_id": "ing-1", "device_id": "d1", "trial_id": "t1",
                                "records": [{"device_seq": 1,
                                             "observed_at": "2026-08-01T09:00:00Z",
                                             "metric": "yield", "value": 400.0}]}, headers)
        self.assertEqual(201, status)
        self.assertEqual(1, ingest["result"]["accepted"])
        status, frozen = route(self.service, "POST", "/analysis-versions",
                               {"request_id": "f1", "protocol_id": "pr1"},
                               {"X-Actor-Id": "r1"})
        self.assertEqual(201, status)
        version_id = frozen["result"]["version_id"]
        status, verdict = route(self.service, "POST", "/verdicts",
                                {"request_id": "v1", "analysis_version_id": version_id,
                                 "decision": "recommended", "rationale": "达标"},
                                {"X-Actor-Id": "r1"})
        self.assertEqual(201, status)
        verdict_id = verdict["result"]["verdict_id"]
        status, confirmed = route(self.service, "POST", "/verdict-confirmations",
                                  {"request_id": "cf1", "verdict_id": verdict_id},
                                  {"X-Actor-Id": "r1"})
        self.assertEqual("effective", confirmed["result"]["status"])
        status, explanation = route(self.service, "GET",
                                    "/regional-verdict?region_id=r1&variety_id=v1", None)
        self.assertEqual(200, status)
        self.assertEqual("recommended", explanation["decision"])
        self.assertEqual(["t1"], [entry["trial_id"] for entry in explanation["used_trials"]])


if __name__ == "__main__":
    unittest.main()
