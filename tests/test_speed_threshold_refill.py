import unittest
import sys
import os
import json
import tempfile
import shutil

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import (
    get_speed_sources_for_region_rr,
    probe_throughput_speed,
    run_filter_and_export,
    DEFAULT_REGION_QUOTAS,
    _SPEED_SOURCE_INDEX,
)


class TestSpeedThresholdAndRefill(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_refill_")
        _SPEED_SOURCE_INDEX.clear()

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)
        _SPEED_SOURCE_INDEX.clear()

    def test_round_robin_speed_source_rotation(self):
        """验证测速源按全局轮询指针轮转，首选测速源交替轮换"""
        srcs1 = get_speed_sources_for_region_rr("US")
        srcs2 = get_speed_sources_for_region_rr("US")
        srcs3 = get_speed_sources_for_region_rr("US")

        # 连续三次获取的首选测速源应当互不相同
        self.assertNotEqual(srcs1[0], srcs2[0])
        self.assertNotEqual(srcs2[0], srcs3[0])

    def test_speedtest_fast_fail_on_tunnel_break(self):
        """验证当代理断流/端口无法访问时，快速熔断标记 status='no_data', 不执行无效 failover 重试"""
        res = probe_throughput_speed(
            ip="1.2.3.4", port=443, duration=0.5, parallel=1,
            max_retry=2, proxy_port=19999,  # 无效端口模拟代理断流
        )
        self.assertEqual(res["status"], "no_data")
        self.assertEqual(res["speed"], 0.0)
        self.assertEqual(res["retries"], 0)  # 快速熔断，重试次数应为 0

    def test_min_speed_filtering_guarantee(self):
        """验证所有选出的节点速度严格 >= min_speed 门槛"""
        out_file = os.path.join(self.test_dir, "strict_speed.txt")
        detail_file = os.path.join(self.test_dir, "strict_speed_detail.json")

        # 构造一批候选节点
        mock_candidates = []
        for i in range(1, 10):
            mock_candidates.append((f"10.1.1.{i}", 443, 0.0, f"HK-Node-{i:02d}"))
        for i in range(1, 15):
            mock_candidates.append((f"10.2.2.{i}", 443, 0.0, f"US-Node-{i:02d}"))

        run_filter_and_export(
            tag="严格测速",
            output_file=out_file,
            mock_mode=True,
            mock_candidates=mock_candidates,
            min_speed=3.0,
            max_refill_rounds=1
        )

        self.assertTrue(os.path.exists(out_file))
        self.assertTrue(os.path.exists(detail_file))

        with open(detail_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["min_speed_threshold"], 3.0)
        for node in data["nodes"]:
            self.assertGreaterEqual(node["speed"], 3.0, f"节点 {node['ip']} 测速 {node['speed']} 低于门槛 3.0")

    def test_refill_round_execution_recorded_in_detail(self):
        """验证 detail.json 正确记录 refill_rounds_executed 与 min_speed_threshold 属性"""
        out_file = os.path.join(self.test_dir, "refill_test.txt")
        detail_file = os.path.join(self.test_dir, "refill_test_detail.json")

        mock_candidates = [
            ("10.1.1.1", 443, 0.0, "HK-01"),
            ("10.2.2.1", 443, 0.0, "US-01"),
        ]

        run_filter_and_export(
            tag="补位测试",
            output_file=out_file,
            mock_mode=True,
            mock_candidates=mock_candidates,
            min_speed=3.5,
            max_refill_rounds=2
        )

        with open(detail_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("min_speed_threshold", data)
        self.assertIn("refill_rounds_executed", data)
        self.assertEqual(data["min_speed_threshold"], 3.5)
        self.assertGreaterEqual(data["refill_rounds_executed"], 1)


if __name__ == '__main__':
    unittest.main()
