import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import probe_stability


class TestStabilityProbe(unittest.TestCase):
    def test_stability_perfect_six_rounds(self):
        # 模拟 6 轮全通，RTT 围绕 190ms
        mock_rtts = [190.0, 192.0, 188.0, 191.0, 189.0, 193.0]
        call_idx = 0

        def mock_probe(ip, port):
            nonlocal call_idx
            val = mock_rtts[call_idx % len(mock_rtts)]
            call_idx += 1
            return val, "SJC"

        res = probe_stability("104.16.1.1", 443, rounds=6, probe_func=mock_probe, sleep_interval=0.0)
        self.assertIsNotNone(res)
        self.assertEqual(res['loss_rate'], 0.0)
        self.assertEqual(res['success_rounds'], 6)
        self.assertEqual(res['colo'], "SJC")
        self.assertTrue(res['is_us_west'])
        self.assertAlmostEqual(res['median_rtt'], 190.5, delta=0.5)
        self.assertAlmostEqual(res['jitter'], 5.0, delta=0.1)

    def test_stability_one_loss_tolerated(self):
        # 模拟丢 1 包 (5 轮成功)，应该被允许但记录丢包率
        mock_responses = [200.0, None, 210.0, 205.0, 195.0, 202.0]
        call_idx = 0

        def mock_probe(ip, port):
            nonlocal call_idx
            val = mock_responses[call_idx]
            call_idx += 1
            return val, "LAX"

        res = probe_stability("104.16.1.2", 443, rounds=6, probe_func=mock_probe, sleep_interval=0.0)
        self.assertIsNotNone(res)
        self.assertEqual(res['success_rounds'], 5)
        self.assertAlmostEqual(res['loss_rate'], 1 / 6, delta=0.01)

    def test_stability_two_loss_rejected(self):
        # 模拟丢 2 包 (4 轮成功)，严格淘汰
        mock_responses = [200.0, None, None, 205.0, 195.0, 202.0]
        call_idx = 0

        def mock_probe(ip, port):
            nonlocal call_idx
            val = mock_responses[call_idx]
            call_idx += 1
            return val, "SJC"

        res = probe_stability("104.16.1.3", 443, rounds=6, probe_func=mock_probe, sleep_interval=0.0)
        self.assertIsNone(res)


if __name__ == '__main__':
    unittest.main()
