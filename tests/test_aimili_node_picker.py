#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aimili 节点优选器实测测速与多轮补位单元测试
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# 将项目根目录加入模块搜索路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from aimili_node_picker import (
    AimiliClient,
    CandidateNode,
    IpIntelligenceManager,
    IpQuality,
    NodeExperience,
    NodeExperienceManager,
    PickerEngine,
    probe_real_download_speed,
)


class TestRealSpeedProbe(unittest.TestCase):
    """测试真实测速函数与换算逻辑"""

    def test_mock_speed_passthrough(self):
        speed = probe_real_download_speed(mock_speed=6.85)
        self.assertEqual(speed, 6.85)

    def test_speed_calculation_formula(self):
        # 1MB (1,048,576 字节) 在 1.0 秒内传输完成
        # 1048576 * 8 / (1.0 * 1e6) = 8.388608 -> 8.39 Mbps
        received_bytes = 1048576
        duration = 1.0
        mbps = round((received_bytes * 8.0) / (duration * 1e6), 2)
        self.assertEqual(mbps, 8.39)

        # 500KB (512,000 字节) 在 0.8 秒内完成
        # 512000 * 8 / (0.8 * 1e6) = 5.12 Mbps
        mbps2 = round((512000 * 8.0) / (0.8 * 1e6), 2)
        self.assertEqual(mbps2, 5.12)


class TestPickerEngineMultiRound(unittest.TestCase):
    """测试优选排程核心的多轮补足与实测过滤"""

    def setUp(self):
        self.mock_aimili = MagicMock(spec=AimiliClient)
        self.mock_aimili.logged_in = True
        self.mock_aimili.host = "127.0.0.1"
        self.mock_aimili.proxy_port = 7928
        self.mock_aimili.base_url = "http://127.0.0.1:8501"
        self.mock_aimili.login.return_value = True
        self.mock_aimili.get_current_favorites.return_value = []
        self.mock_aimili.set_active_node.return_value = True
        self.mock_aimili.toggle_favorite.return_value = True
        self.mock_aimili.refresh_nodes.return_value = True

        self.mock_inspector = MagicMock(spec=IpIntelligenceManager)
        self.mock_inspector.cache = {}
        self.mock_inspector._ping0_circuit_broken = False
        self.mock_inspector.reset_circuit_breaker.return_value = None

        self.mock_exp = MagicMock(spec=NodeExperienceManager)
        self.mock_exp.get_experience_bonus.return_value = 0.0
        exp_sample = NodeExperience(ip="test", consecutive_passes=1, reputation_score=80.0)
        self.mock_exp.get_or_create.return_value = exp_sample
        self.mock_exp.record_pass.return_value = exp_sample
        self.mock_exp.record_fail.return_value = exp_sample

        self.engine = PickerEngine(
            aimili=self.mock_aimili,
            inspector=self.mock_inspector,
            exp_mgr=self.mock_exp
        )

    def test_real_speed_filtering_rejects_under_5mbps(self):
        """测试实测速率低于 5 Mbps 的节点被淘汰，达到或超过 5 Mbps 的节点入选"""
        node_fast = CandidateNode(id="n1", ip="1.1.1.1", port=1194, country="JP", protocol="openvpn", latency_ms=40)
        node_slow = CandidateNode(id="n2", ip="2.2.2.2", port=1194, country="JP", protocol="openvpn", latency_ms=50)

        self.mock_aimili.fetch_scoped_nodes.return_value = [node_fast, node_slow]
        self.mock_inspector.inspect_ip.return_value = IpQuality(
            ip="test", is_residential=True, risk_score=10, ip_type="家庭宽带 IP", shared_users="1 - 10"
        )

        # 模拟 node_fast 实测 8.5 Mbps，node_slow 实测 3.2 Mbps
        speed_map = {"1.1.1.1": 8.5, "2.2.2.2": 3.2}
        self.engine.probe_speed_func = lambda proxy_port, proxy_host, max_duration: speed_map.get(
            self.mock_aimili.set_active_node.call_args[0][0].ip, 0.0
        )

        picked = self.engine.run_screening(
            country="JP",
            max_risk=15,
            min_speed_mbps=5.0,
            top_n=5,
            inherit_existing=False,
            max_rounds=1
        )

        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0].ip, "1.1.1.1")
        self.assertEqual(picked[0].speed_mbps, 8.5)

    def test_multi_round_refill_up_to_3_rounds(self):
        """测试节点不足 20 个时，自动进入下一轮补测，最多测 3 轮"""
        # 第一轮候选只有 2 个节点
        round1_nodes = [
            CandidateNode(id="r1_1", ip="10.0.1.1", port=1194, country="JP", protocol="openvpn", latency_ms=40),
            CandidateNode(id="r1_2", ip="10.0.1.2", port=1194, country="JP", protocol="openvpn", latency_ms=45),
        ]
        # 第二轮通过 refresh_nodes 后发现新节点
        round2_nodes = [
            CandidateNode(id="r1_1", ip="10.0.1.1", port=1194, country="JP", protocol="openvpn", latency_ms=40),
            CandidateNode(id="r1_2", ip="10.0.1.2", port=1194, country="JP", protocol="openvpn", latency_ms=45),
            CandidateNode(id="r2_1", ip="10.0.2.1", port=1194, country="JP", protocol="openvpn", latency_ms=35),
            CandidateNode(id="r2_2", ip="10.0.2.2", port=1194, country="JP", protocol="openvpn", latency_ms=38),
            CandidateNode(id="r2_3", ip="10.0.2.3", port=1194, country="JP", protocol="openvpn", latency_ms=42),
        ]

        self.mock_aimili.fetch_scoped_nodes.side_effect = [round1_nodes, round2_nodes]
        self.mock_inspector.inspect_ip.return_value = IpQuality(
            ip="test", is_residential=True, risk_score=5, ip_type="家庭宽带 IP", shared_users="1 - 10"
        )
        # 所有节点实测速率均达标 (6.0 Mbps)
        self.engine.probe_speed_func = lambda proxy_port, proxy_host, max_duration: 6.0

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(
                country="JP",
                max_risk=15,
                min_speed_mbps=5.0,
                top_n=4,  # 目标 4 个：第 1 轮得 2 个，第 2 轮补齐到 4 个
                inherit_existing=False,
                max_rounds=3
            )

        self.assertEqual(len(picked), 4)
        # 验证是否调用了 refresh_nodes
        self.mock_aimili.refresh_nodes.assert_called_once_with(country="JP")

    def test_stops_at_max_rounds_when_quota_unmet(self):
        """测试满 3 轮即便不足 20 个也不会无限循环，而是正常收工"""
        # 每轮都只有这 1 个达标节点
        single_node = [
            CandidateNode(id="n1", ip="10.0.0.1", port=1194, country="JP", protocol="openvpn", latency_ms=30)
        ]
        self.mock_aimili.fetch_scoped_nodes.return_value = single_node
        self.mock_inspector.inspect_ip.return_value = IpQuality(
            ip="test", is_residential=True, risk_score=5, ip_type="家庭宽带 IP", shared_users="1 - 10"
        )
        self.engine.probe_speed_func = lambda proxy_port, proxy_host, max_duration: 7.0

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(
                country="JP",
                max_risk=15,
                min_speed_mbps=5.0,
                top_n=20,  # 目标 20 个
                inherit_existing=False,
                max_rounds=3
            )

        self.assertEqual(len(picked), 1)
        # 应该只 refresh 了 2 次 (第 1 轮未触发，第 2 轮前 1 次，第 3 轮前 1 次)
        self.assertEqual(self.mock_aimili.refresh_nodes.call_count, 2)


    def test_retained_favorite_real_speed_eviction(self):
        """测试存量收藏节点在复检时，实测速度 < 5 Mbps 会被淘汰并移出收藏"""
        fav_node = CandidateNode(id="fav_1", ip="192.168.1.100", port=1194, country="JP", protocol="openvpn", latency_ms=45)
        fresh_node = CandidateNode(id="fresh_1", ip="192.168.1.200", port=1194, country="JP", protocol="openvpn", latency_ms=30)

        self.mock_aimili.get_current_favorites.return_value = ["fav_1"]
        self.mock_aimili.fetch_scoped_nodes.return_value = [fav_node, fresh_node]
        self.mock_inspector.inspect_ip.return_value = IpQuality(
            ip="test", is_residential=True, risk_score=5, ip_type="家庭宽带 IP", shared_users="1 - 10"
        )

        # fav_1 实测只有 2.5 Mbps (< 5.0)，fresh_1 实测 8.0 Mbps (>= 5.0)
        speed_map = {"192.168.1.100": 2.5, "192.168.1.200": 8.0}
        self.engine.probe_speed_func = lambda proxy_port, proxy_host, max_duration: speed_map.get(
            self.mock_aimili.set_active_node.call_args[0][0].ip, 0.0
        )

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(
                country="JP",
                max_risk=15,
                min_speed_mbps=5.0,
                top_n=1,
                inherit_existing=True,
                max_rounds=1
            )

        # 存量 fav_1 应被淘汰，新节点 fresh_1 应入选
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0].ip, "192.168.1.200")
        self.mock_aimili.remove_favorite_by_id.assert_called_with("fav_1", timeout=3)
        self.mock_exp.record_fail.assert_called()


if __name__ == "__main__":
    unittest.main()
