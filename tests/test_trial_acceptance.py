import unittest

from science_strategy_foundation.trial_acceptance import run


class TrialAcceptanceTest(unittest.TestCase):
    def test_offline_trial_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        # 离线记录按设备序列归并，重复上报不增加样本
        self.assertTrue(result["offline_duplicate_merged"])
        # 越序/超窗/校准失效进入待审而非丢弃，且均被处置
        self.assertTrue(result["pending_queued_then_resolved"])
        # 区域结论引用冻结分析：北部达标推荐、南部有效样本不足拒绝
        self.assertEqual("recommend", result["first_north_decision"])
        self.assertEqual("reject", result["first_south_decision"])
        # 后继裁定不改变已发布的冻结分析
        self.assertTrue(result["first_analysis_unchanged"])
        # 灾害性气象豁免不能悄悄改写，只能形成后继裁定
        self.assertTrue(result["silent_change_blocked"])
        self.assertEqual("disaster_exemption", result["second_south_amendment"])
        self.assertEqual([1, 2], result["south_version_chain"])
        # 并发确认后唯一生效，由两名审查人确认
        self.assertEqual(["rv-003", "rv-004"], result["confirmed_by"])


if __name__ == "__main__":
    unittest.main()
