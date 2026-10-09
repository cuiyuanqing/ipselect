import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import calculate_speed_mb_s, probe_real_download_speed


class TestMicroSpeed(unittest.TestCase):
    def test_calculate_speed_mb_s(self):
        # 1.5MB (1572864 字节) 在 0.5 秒内下完 -> 3.0 MB/s
        speed = calculate_speed_mb_s(1572864, 0.5)
        self.assertAlmostEqual(speed, 3.0, delta=0.01)

    def test_calculate_speed_zero_duration(self):
        # 极端情况 duration <= 0，避免除以零
        speed = calculate_speed_mb_s(1024, 0.0)
        self.assertGreaterEqual(speed, 0.0)

    def test_calculate_speed_small_bytes(self):
        # 很少的数据 (如 100KB) 在 1 秒 -> 约 0.1 MB/s
        speed = calculate_speed_mb_s(102400, 1.0)
        self.assertAlmostEqual(speed, 0.1, delta=0.01)


if __name__ == '__main__':
    unittest.main()
