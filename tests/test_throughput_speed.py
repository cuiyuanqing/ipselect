import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import probe_throughput_speed


class TestThroughputSpeed(unittest.TestCase):
    def test_mock_speed_returns_sample(self):
        """mock_speed 模式应直接返回单样本 [value]，speed 等于该值"""
        result = probe_throughput_speed(
            ip="104.16.222.118", port=8443, mock_speed=12.5,
            duration=5, parallel=3, max_retry=1,
            max_bytes_per_ip=80 * 1024 * 1024, sources=("speed.cloudflare.com",)
        )
        self.assertEqual(result["speed"], 12.5)
        self.assertEqual(result["samples"], [12.5])

    def test_mock_speed_two_rounds_returns_median(self):
        """mock_speed 与 samples 都给定，应返回中位 (samples 优先，mock_speed 被忽略)"""
        result = probe_throughput_speed(
            ip="104.16.222.118", port=8443, mock_speed=8.0, mock_samples=(5.0, 9.0),
            duration=5, parallel=3, max_retry=1,
            max_bytes_per_ip=80 * 1024 * 1024, sources=("speed.cloudflare.com",)
        )
        # samples=(5.0, 9.0) 的真中位是 statistics.median -> 7.0
        self.assertEqual(result["speed"], 7.0)
        self.assertEqual(len(result["samples"]), 2)

    def test_returns_dict_shape(self):
        """返回字典应包含 speed, samples, p90, stddev, retries, bytes_downloaded, status (共 7 字段)"""
        result = probe_throughput_speed(
            ip="104.16.222.118", port=8443, mock_speed=7.0,
            duration=5, parallel=3, max_retry=1,
            max_bytes_per_ip=80 * 1024 * 1024, sources=("speed.cloudflare.com",)
        )
        for key in ("speed", "samples", "p90", "stddev", "retries", "bytes_downloaded", "status"):
            self.assertIn(key, result)


if __name__ == '__main__':
    unittest.main()
