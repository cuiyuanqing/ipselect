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
        # 极优指标：4K 节点 speed=25MB/s, 延迟 170ms, 抖动 8ms, 机房 LAX
        score = calculate_e2e_score(e2e_rtt=170.0, jitter=8.0, speed_mb_s=25.0, colo="LAX")
        self.assertGreaterEqual(score, 9.5)
        self.assertLessEqual(score, 10.0)

    def test_calculate_e2e_score_medium(self):
        # 中等指标：1080p 节点 speed=10MB/s, 延迟 210ms, 抖动 20ms, 机房 SJC
        score = calculate_e2e_score(e2e_rtt=210.0, jitter=20.0, speed_mb_s=10.0, colo="SJC")
        self.assertGreaterEqual(score, 8.5)
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
        self.assertEqual(line, "104.16.222.118:8443#US-家庭-01-1.80MB/s")

    def test_probe_e2e_download_speed_mock(self):
        speed = probe_e2e_download_speed(mock_speed=1.75)
        self.assertEqual(speed, 1.75)

    def test_calculate_e2e_score_speed_4k(self):
        """4K 节点 speed=25MB/s 应接近满分 10"""
        score = calculate_e2e_score(e2e_rtt=170.0, jitter=10.0, speed_mb_s=25.0, colo="LAX")
        self.assertGreaterEqual(score, 9.0)

    def test_calculate_e2e_score_speed_above_saturation_old(self):
        """speed=15 MB/s 在新曲线下不再被封顶到 10（其它全 10 时总分为 9.5）"""
        score = calculate_e2e_score(e2e_rtt=170.0, jitter=10.0, speed_mb_s=15.0, colo="LAX")
        # 新曲线 speed 子分 = 7.5 + 5/15 * 2.5 = 8.33
        # 加权后 = 0.35*10 + 0.25*10 + 0.30*8.33 + 0.10*10 = 9.5
        self.assertGreaterEqual(score, 8.0)
        self.assertLessEqual(score, 9.5)

    def test_calculate_e2e_score_speed_threshold_boundaries(self):
        """边界 0.5/2/4/10/25 都应有单调递增分数"""
        prev = -1
        for sp in [0.5, 2.0, 4.0, 10.0, 25.0]:
            s = calculate_e2e_score(e2e_rtt=200.0, jitter=15.0, speed_mb_s=sp, colo="SJC")
            self.assertGreater(s, prev, f"speed={sp} 分数应高于前一个")
            prev = s


if __name__ == '__main__':
    unittest.main()
