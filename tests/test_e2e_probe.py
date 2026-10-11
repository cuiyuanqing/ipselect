import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import probe_e2e_delay, probe_ip_sb_exit, evaluate_e2e_candidate


class TestE2EProbe(unittest.TestCase):
    def test_probe_e2e_delay_mock(self):
        # 模拟 API 返回正常延迟
        delay = probe_e2e_delay("node_01", "http://www.gstatic.com/generate_204", mock_delay=185)
        self.assertEqual(delay, 185)

        # 模拟 API 超时失败
        delay_fail = probe_e2e_delay("node_01", "http://www.gstatic.com/generate_204", mock_delay=None)
        self.assertIsNone(delay_fail)

    def test_probe_ip_sb_exit_mock_us(self):
        # 模拟返回正常美国出口 IP
        is_us, country, ip = probe_ip_sb_exit(mock_exit={"country_code": "US", "ip": "104.28.19.5"})
        self.assertTrue(is_us)
        self.assertEqual(country, "US")
        self.assertEqual(ip, "104.28.19.5")

    def test_probe_ip_sb_exit_mock_non_us(self):
        # 模拟出口漂移到非美国 (如 HK/SG)，应判定失败
        is_us, country, ip = probe_ip_sb_exit(mock_exit={"country_code": "HK", "ip": "1.2.3.4"})
        self.assertFalse(is_us)
        self.assertEqual(country, "HK")

    def test_evaluate_e2e_candidate_success(self):
        mock_data = {
            "google_delays": [210, 205, 215],
            "youtube_delay": 220,
            "ip_sb_exit": {"country_code": "US", "ip": "104.28.20.1"}
        }
        res = evaluate_e2e_candidate("node_01", "104.16.222.118", 8443, "LAX", mock_data=mock_data)
        self.assertIsNotNone(res)
        self.assertEqual(res["ip"], "104.16.222.118")
        self.assertAlmostEqual(res["e2e_avg_rtt"], 210.0, places=1)
        self.assertEqual(res["e2e_jitter"], 10)
        self.assertEqual(res["exit_country"], "US")

    def test_evaluate_e2e_candidate_disqualified_google_loss(self):
        # Google 连续丢包 (成功数 < 2)
        mock_data = {
            "google_delays": [None, None, 210],
            "youtube_delay": 220,
            "ip_sb_exit": {"country_code": "US", "ip": "104.28.20.1"}
        }
        res = evaluate_e2e_candidate("node_01", "104.16.222.118", 8443, "LAX", mock_data=mock_data)
        self.assertIsNone(res)

    def test_evaluate_e2e_candidate_disqualified_youtube_failed(self):
        # YouTube 连通失败
        mock_data = {
            "google_delays": [210, 205, 215],
            "youtube_delay": None,
            "ip_sb_exit": {"country_code": "US", "ip": "104.28.20.1"}
        }
        res = evaluate_e2e_candidate("node_01", "104.16.222.118", 8443, "LAX", mock_data=mock_data)
        self.assertIsNone(res)

    def test_evaluate_e2e_candidate_disqualified_non_us_exit(self):
        # 出口非 US
        mock_data = {
            "google_delays": [210, 205, 215],
            "youtube_delay": 220,
            "ip_sb_exit": {"country_code": "JP", "ip": "104.28.20.1"}
        }
        res = evaluate_e2e_candidate("node_01", "104.16.222.118", 8443, "LAX", mock_data=mock_data)
        self.assertIsNone(res)


if __name__ == '__main__':
    unittest.main()
