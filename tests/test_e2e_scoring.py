import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import (
    calculate_e2e_score,
    format_node_line,
    probe_e2e_download_speed
)


class TestE2EScoring(unittest.TestCase):
    def test_calculate_e2e_score_excellent(self):
        # 极优指标：延迟 170ms, 抖动 8ms, 速度 2.2MB/s, 机房 LAX
        score = calculate_e2e_score(e2e_rtt=170.0, jitter=8.0, speed_mb_s=2.2, colo="LAX")
        self.assertGreaterEqual(score, 9.5)
        self.assertLessEqual(score, 10.0)

    def test_calculate_e2e_score_medium(self):
        # 中等指标：延迟 210ms, 抖动 20ms, 速度 1.5MB/s, 机房 SJC
        score = calculate_e2e_score(e2e_rtt=210.0, jitter=20.0, speed_mb_s=1.5, colo="SJC")
        self.assertGreaterEqual(score, 8.0)
        self.assertLessEqual(score, 9.5)

    def test_calculate_e2e_score_poor(self):
        # 较差指标：延迟 360ms, 抖动 80ms, 速度 0.3MB/s, 机房 OTHER
        score = calculate_e2e_score(e2e_rtt=360.0, jitter=80.0, speed_mb_s=0.3, colo="OTHER")
        self.assertLessEqual(score, 5.0)
        self.assertGreaterEqual(score, 1.0)

    def test_format_node_line(self):
        line = format_node_line(
            ip="104.16.222.118",
            port=8443,
            score=8.8,
            rank=1,
            tag="家庭",
            speed_mb_s=1.80,
            colo="LAX"
        )
        self.assertEqual(line, "104.16.222.118:8443#US-DaTree-8.8-01-家庭 1.80MB/s")

    def test_probe_e2e_download_speed_mock(self):
        speed = probe_e2e_download_speed(mock_speed=1.75)
        self.assertEqual(speed, 1.75)


if __name__ == '__main__':
    unittest.main()
