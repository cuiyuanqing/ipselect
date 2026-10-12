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


    def test_probe_returns_no_data_when_proxy_dead(self):
        """回归测试: 沙箱停止后端口不可达 → status='no_data', speed=0.
        v1.4.0 根因: sandbox.stop() 在 stage-3 finally 中提前杀死 mihomo,
        导致 stage 4 的 probe_throughput_speed 连接 127.0.0.1:19998 被拒绝,
        所有节点 speed=0.00MB/s。此测试记录该失败签名。"""
        result = probe_throughput_speed(
            ip="1.2.3.4", port=443, duration=0.5, parallel=1,
            max_retry=0, proxy_port=19999,  # 无进程监听此端口
        )
        self.assertEqual(result["status"], "no_data")
        self.assertEqual(result["speed"], 0.0)
        self.assertEqual(result["samples"], [])


if __name__ == '__main__':
    unittest.main()
