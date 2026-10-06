import unittest

from agri_trial_adjudication.acceptance import run


class AgriAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        # 重复上报不增加样本
        self.assertEqual(3, result["ingest_first_accepted"])
        self.assertEqual(3, result["ingest_replay_duplicates"])
        # 越序、超窗、校准失效进入待审而非丢弃
        self.assertEqual("out_of_order", result["late_pending_reason"])
        self.assertEqual("out_of_window", result["window_pending_reason"])
        self.assertEqual("calibration_expired", result["uncal_pending_reason"])
        # 待审采纳后计入样本
        self.assertEqual(6, result["tr_a_accepted_observations"])
        # 冻结版本：两个有效试验、一个被排除
        self.assertEqual(2, result["frozen_valid_count"])
        self.assertEqual(1, result["frozen_excluded_count"])
        # 首个裁定经并发确认生效，解释可追到地块
        self.assertEqual("effective", result["first_verdict_status"])
        self.assertEqual("recommended", result["first_decision"])
        self.assertEqual(["plot-a", "plot-b"], result["first_used_plots"])
        self.assertEqual(["plot-c"], result["first_excluded_plots"])
        # 批次撤回只能形成后继裁定，已发布范围保留在历史中
        self.assertEqual(1, len(result["withdrawal_successor_hint"]))
        self.assertEqual("rejected", result["final_decision"])
        self.assertEqual([("recommended", "superseded"), ("rejected", "effective")],
                         result["history"])


if __name__ == "__main__":
    unittest.main()
