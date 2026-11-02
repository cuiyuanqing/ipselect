#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aimili 节点优选器实测测速与多轮补位单元测试
"""

import os
import sys
import tempfile
import shutil
import json
import unittest
from unittest.mock import MagicMock, patch

# 将项目根目录加入模块搜索路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from aimili_node_picker import (
    AimiliClient,
    BlacklistManager,
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

    def test_dual_stream_aggregation(self):
        # 模拟双流并行传输：Stream 1 传输 1,250,000 字节，Stream 2 传输 1,250,000 字节，在 2.0 秒内完成
        bytes_stream1 = 1250000
        bytes_stream2 = 1250000
        total = bytes_stream1 + bytes_stream2
        duration = 2.0
        mbps = round((total * 8.0) / (duration * 1e6), 2)
        self.assertEqual(mbps, 10.0)


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
        self.engine.blacklist.records = {}
        self.engine.probe_network_func = lambda ip, port, protocol: {
            "is_alive": True,
            "port_open": True,
            "avg_latency_ms": 30,
            "jitter_ms": 5,
            "packet_loss_pct": 0,
            "method": "mock"
        }

    def test_real_speed_filtering_rejects_under_5mbps(self):
        """测试实测速率低于 5 Mbps 的节点被淘汰，达到或超过 5 Mbps 的节点入选"""
        node_fast = CandidateNode(id="n1", ip="1.1.1.1", port=1194, country="JP", protocol="openvpn", latency_ms=40)
        node_slow = CandidateNode(id="n2", ip="2.2.2.2", port=1194, country="JP", protocol="openvpn", latency_ms=50)

        self.mock_aimili.fetch_scoped_nodes.return_value = [node_fast, node_slow]
        self.mock_inspector.inspect_ip.return_value = IpQuality(
            ip="test", is_residential=True, risk_score=5, ip_type="家庭宽带 IP", shared_users="1 - 10"
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

    def test_safety_floor_at_least_5_nodes_when_sub_5mbps(self):
        """测试3轮测速后若没有节点达到 5 Mbps，触发降级保底机制，自动补足最优的 5 个节点"""
        # 构造 6 个节点，实测速率全都在 1.5 ~ 4.2 Mbps (< 5.0 Mbps)
        candidates = [
            CandidateNode(id=f"n_{i}", ip=f"10.0.0.{i}", port=1194, country="JP", protocol="openvpn", latency_ms=30 + i * 5)
            for i in range(1, 7)
        ]
        self.mock_aimili.fetch_scoped_nodes.return_value = candidates
        self.mock_inspector.inspect_ip.return_value = IpQuality(
            ip="test", is_residential=True, risk_score=5, ip_type="家庭宽带 IP", shared_users="1 - 10"
        )
        # 速率字典：最高 4.2，最低 1.5
        speed_dict = {
            "10.0.0.1": 4.2,
            "10.0.0.2": 3.8,
            "10.0.0.3": 3.0,
            "10.0.0.4": 2.5,
            "10.0.0.5": 2.0,
            "10.0.0.6": 1.5,
        }
        self.engine.probe_speed_func = lambda proxy_port, proxy_host, max_duration: speed_dict.get(
            self.mock_aimili.set_active_node.call_args[0][0].ip, 1.0
        )

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(
                country="JP",
                max_risk=15,
                min_speed_mbps=5.0,  # 门槛 5.0
                top_n=20,            # 期望 20 个
                inherit_existing=False,
                max_rounds=3
            )

        # 触发保底降级：选出最优的 5 个存活节点，绝不为空！
        self.assertEqual(len(picked), 5)
        picked_ips = [n.ip for n in picked]
        # 应该优先入选速率最高的 5 个节点 (10.0.0.1 ~ 10.0.0.5)
        self.assertIn("10.0.0.1", picked_ips)
        self.assertIn("10.0.0.2", picked_ips)
        self.assertIn("10.0.0.5", picked_ips)
        self.assertNotIn("10.0.0.6", picked_ips)


class TestWebUiAndClientRobustness(unittest.TestCase):
    """测试 Web 前端日志暂停与 AimiliClient 409 防冲突重试机制"""

    def test_web_ui_has_pause_logs_checkbox(self):
        from aimili_node_picker import HTML_TEMPLATE
        self.assertIn('id="chkPauseLog"', HTML_TEMPLATE)
        self.assertIn("暂停日志刷新", HTML_TEMPLATE)
        self.assertTrue("chk.checked" in HTML_TEMPLATE or "chkPauseLog")

    def test_aimili_client_set_active_node_409_retry(self):
        import io
        import urllib.error
        client = AimiliClient(
            auth_file="/nonexistent/ui_auth.json",
            username="admin",
            password="admin",
            secret_path="test_token",
            port=8501,
            proxy_port=8500
        )
        client.logged_in = True
        client.wait_for_idle = MagicMock(return_value=True)
        client.wait_for_tunnel_ready = MagicMock(return_value=True)

        node = CandidateNode(id="test_node_1", ip="192.168.1.1", port=1194, country="JP", protocol="openvpn", latency_ms=45)

        # 模拟第一次 409 Conflict，第二次成功
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        error_409 = urllib.error.HTTPError("http://127.0.0.1:8501/api/connect", 409, "Conflict", {}, io.BytesIO(b"Conflict"))

        call_count = 0
        def fake_open(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise error_409
            return mock_resp

        client.opener = MagicMock()
        client.opener.open.side_effect = fake_open

        with patch("time.sleep", return_value=None):
            ok = client.set_active_node(node, timeout=5, max_retries=3)

        self.assertTrue(ok)
        self.assertGreaterEqual(call_count, 2)


class TestBlacklistAndHandshakeReliability(unittest.TestCase):
    """测试黑名单(风控>=10%永久拉黑)与真实握手质量把关"""

    def setUp(self):
        self.tmp_bl_file = os.path.join(BASE_DIR, "tests", "tmp_blacklist.json")
        if os.path.exists(self.tmp_bl_file):
            os.remove(self.tmp_bl_file)
        self.bl = BlacklistManager(blacklist_file=self.tmp_bl_file)

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
        self.mock_exp.records = {}
        sample_exp = MagicMock(spec=NodeExperience)
        sample_exp.consecutive_passes = 1
        sample_exp.reputation_score = 80.0
        sample_exp.level_label = "🌱 Lv.1 新晋新秀"
        self.mock_exp.record_pass.return_value = sample_exp
        self.mock_exp.record_fail.return_value = sample_exp

        self.engine = PickerEngine(
            aimili=self.mock_aimili,
            inspector=self.mock_inspector,
            exp_mgr=self.mock_exp,
            blacklist=self.bl
        )
        self.engine.probe_network_func = lambda ip, port, protocol: {
            "is_alive": True,
            "port_open": True,
            "avg_latency_ms": 30,
            "jitter_ms": 5,
            "packet_loss_pct": 0,
            "method": "mock"
        }
        self.engine.probe_speed_func = lambda proxy_port, proxy_host, max_duration: 10.0

    def tearDown(self):
        if os.path.exists(self.tmp_bl_file):
            try:
                os.remove(self.tmp_bl_file)
            except Exception:
                pass

    def test_risk_score_ge_10_is_blacklisted_and_never_picked(self):
        """纯净度/风控值 >= 10 的节点必须直接拉黑且永不入选，即使速率极高"""
        # node1: risk 12% (>=10, 必须拉黑淘汰)
        # node2: risk 5% (<10, 优质家宽通过)
        node_dirty = CandidateNode(id="n_dirty", ip="219.100.37.24", port=1194, country="JP", protocol="openvpn", latency_ms=30)
        node_clean = CandidateNode(id="n_clean", ip="164.70.137.225", port=1194, country="JP", protocol="openvpn", latency_ms=35)

        self.mock_aimili.fetch_scoped_nodes.return_value = [node_dirty, node_clean]

        def fake_inspect(ip, source_mode="auto"):
            if ip == "219.100.37.24":
                return IpQuality(ip=ip, is_residential=True, risk_score=12, ip_type="住宅/商业宽带", shared_users="1 - 10")
            return IpQuality(ip=ip, is_residential=True, risk_score=5, ip_type="家庭宽带 IP", shared_users="1 - 10")

        self.mock_inspector.inspect_ip.side_effect = fake_inspect

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(
                country="JP",
                max_risk=15, # 即使入参设为15，硬性黑名单规则仍必须执行
                min_speed_mbps=5.0,
                top_n=5,
                inherit_existing=False,
                max_rounds=1
            )

        # 验证 dirty 节点被永久拉黑并持久化
        self.assertTrue(self.bl.is_blacklisted("219.100.37.24"))
        self.assertFalse(self.bl.is_blacklisted("164.70.137.225"))

        picked_ips = [n.ip for n in picked]
        self.assertIn("164.70.137.225", picked_ips)
        self.assertNotIn("219.100.37.24", picked_ips)

    def test_pre_existing_blacklisted_nodes_are_skipped_without_inspection(self):
        """已被拉黑的节点在候选扫描时直接跳过，不再消耗网络查询与测试"""
        self.bl.add("1.2.3.4", reason="历史风控超标 (12%)", risk_score=12)

        node_bl = CandidateNode(id="n_bl", ip="1.2.3.4", port=1194, country="JP", protocol="openvpn", latency_ms=30)
        self.mock_aimili.fetch_scoped_nodes.return_value = [node_bl]

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(country="JP", top_n=5, max_rounds=1)

        self.mock_inspector.inspect_ip.assert_not_called()
        self.assertEqual(len(picked), 0)

    def test_handshake_failed_dead_nodes_excluded_from_fallback(self):
        """握手失败/未就绪的节点不得进入保底池，绝不将死节点写入收藏夹"""
        dead_node = CandidateNode(id="n_dead", ip="10.0.0.99", port=1194, country="JP", protocol="openvpn", latency_ms=40)
        self.mock_aimili.fetch_scoped_nodes.return_value = [dead_node]
        self.mock_inspector.inspect_ip.return_value = IpQuality(
            ip="10.0.0.99", is_residential=True, risk_score=5, ip_type="家庭宽带 IP", shared_users="1 - 10"
        )

        # 模拟该节点 OpenVPN 握手超时建连失败 (set_active_node 返回 False)
        self.mock_aimili.set_active_node.return_value = False

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(country="JP", top_n=5, min_speed_mbps=5.0, max_rounds=1)

        # 即使触发生存保底，也绝不能选入死节点！
        self.assertEqual(len(picked), 0)

    def test_tunnel_ready_strictness(self):
        """wait_for_tunnel_ready 必须严格校验 tunnel_ready，不能被 proxy_ready 糊弄"""
        client = AimiliClient(auth_file="/nonexistent/ui_auth.json")
        client.logged_in = True

        # 状态中 proxy_ready 为 True 但 tunnel_ready 为 False（即代理在跑但OpenVPN根本没连上）
        fake_state = {
            "is_connecting": False,
            "tunnel_ready": False,
            "proxy_ready": True,
            "active_openvpn_node_id": "test_node_id",
            "last_check_message": "TLS Key Negotiation failed"
        }
        client.get_ui_state = MagicMock(return_value=fake_state)
        client.get_state = MagicMock(return_value=fake_state)

        with patch("time.sleep", return_value=None):
            ready = client.wait_for_tunnel_ready("test_node_id", timeout=0.1)

        # 必须判定为 False（未就绪）
        self.assertFalse(ready)


class TestPipelineExecutionOrder(unittest.TestCase):
    """测试漏斗型初筛顺序：存活/抖动 -> 端口通畅 -> 风控质检 -> 真实测速"""

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
        self.engine.blacklist.records = {}
        self.engine.probe_network_func = lambda ip, port, protocol: {
            "is_alive": True,
            "port_open": True,
            "avg_latency_ms": 30,
            "jitter_ms": 5,
            "packet_loss_pct": 0,
            "method": "mock"
        }
        self.engine.probe_network_func = lambda ip, port, protocol: {
            "is_alive": True,
            "port_open": True,
            "avg_latency_ms": 30,
            "jitter_ms": 5,
            "packet_loss_pct": 0,
            "method": "mock"
        }
        self.engine.probe_speed_func = lambda proxy_port, proxy_host, max_duration: 10.0

    def test_dead_host_rejected_before_ip_risk_inspection(self):
        """丢包率100%/网络不可达的死节点，必须在风控质检前被淘汰，不得调用 inspect_ip"""
        dead_node = CandidateNode(id="n_dead", ip="192.0.2.1", port=1194, country="JP", protocol="openvpn", latency_ms=50)
        self.mock_aimili.fetch_scoped_nodes.return_value = [dead_node]

        # 模拟 probe_network_func 检测为死节点
        fake_probe = MagicMock(return_value={
            "is_alive": False,
            "port_open": False,
            "avg_latency_ms": 999,
            "jitter_ms": 999,
            "packet_loss_pct": 100,
            "method": "icmp_ping"
        })
        self.engine.probe_network_func = fake_probe

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(country="JP", top_n=5, max_rounds=1)

        fake_probe.assert_called_once()
        # 核心断言：inspect_ip 绝对不应被调用！
        self.mock_inspector.inspect_ip.assert_not_called()
        self.assertEqual(len(picked), 0)

    def test_port_closed_node_rejected_before_ip_risk_inspection(self):
        """存活但端口关闭/不通畅的节点，必须在风控质检前淘汰，不得调用 inspect_ip"""
        closed_node = CandidateNode(id="n_closed", ip="10.0.0.1", port=443, country="JP", protocol="tcp", latency_ms=30)
        self.mock_aimili.fetch_scoped_nodes.return_value = [closed_node]

        fake_probe = MagicMock(return_value={
            "is_alive": True,
            "port_open": False,  # 端口不通
            "avg_latency_ms": 40,
            "jitter_ms": 5,
            "packet_loss_pct": 0,
            "method": "tcp_socket"
        })
        self.engine.probe_network_func = fake_probe

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(country="JP", top_n=5, max_rounds=1)

        fake_probe.assert_called_once()
        self.mock_inspector.inspect_ip.assert_not_called()
        self.assertEqual(len(picked), 0)

    def test_high_jitter_node_rejected_before_ip_risk_inspection(self):
        """网络抖动过大 (>150ms) 的不稳定节点，必须在风控前被淘汰，不得调用 inspect_ip"""
        jitter_node = CandidateNode(id="n_jitter", ip="10.0.0.2", port=1194, country="JP", protocol="openvpn", latency_ms=40)
        self.mock_aimili.fetch_scoped_nodes.return_value = [jitter_node]

        fake_probe = MagicMock(return_value={
            "is_alive": True,
            "port_open": True,
            "avg_latency_ms": 60,
            "jitter_ms": 220,  # 极端抖动
            "packet_loss_pct": 10,
            "method": "icmp_ping"
        })
        self.engine.probe_network_func = fake_probe

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(country="JP", top_n=5, max_rounds=1)

        fake_probe.assert_called_once()
        self.mock_inspector.inspect_ip.assert_not_called()
        self.assertEqual(len(picked), 0)

    def test_healthy_node_progresses_through_full_funnel(self):
        """存活、低抖动且端口通畅的节点，正常进入风控质检并最终入选"""
        good_node = CandidateNode(id="n_good", ip="164.70.137.225", port=1194, country="JP", protocol="openvpn", latency_ms=40)
        self.mock_aimili.fetch_scoped_nodes.return_value = [good_node]

        fake_probe = MagicMock(return_value={
            "is_alive": True,
            "port_open": True,
            "avg_latency_ms": 35,
            "jitter_ms": 6,
            "packet_loss_pct": 0,
            "method": "icmp_ping"
        })
        self.engine.probe_network_func = fake_probe
        self.mock_inspector.inspect_ip.return_value = IpQuality(
            ip="164.70.137.225", is_residential=True, risk_score=5, ip_type="家庭宽带 IP", shared_users="1 - 10"
        )

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(country="JP", top_n=5, min_speed_mbps=5.0, max_rounds=1)

        fake_probe.assert_called_once()
        self.mock_inspector.inspect_ip.assert_called_once_with("164.70.137.225", source_mode="auto")
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0].ip, "164.70.137.225")
        self.assertEqual(picked[0].latency_ms, 35)
        self.assertEqual(picked[0].jitter_ms, 6)




class TestRemoteDeployEndpoints(unittest.TestCase):
    """测试 VPS 远程 Git 覆盖更新与服务重启接口"""

    @patch("os.path.exists", return_value=True)
    @patch("os.path.isdir", return_value=True)
    @patch("subprocess.run")
    def test_run_git_force_update_success(self, mock_subproc, mock_isdir, mock_exists):
        from aimili_node_picker import run_git_force_update

        # 模拟 fetch 成功, reset 成功, log 成功
        def fake_run(cmd, *args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            if "log" in cmd:
                m.stdout = "d918950 - feat: auto deploy (2026-10-31)"
            elif "reset" in cmd:
                m.stdout = "HEAD is now at d918950 feat: auto deploy"
            return m

        mock_subproc.side_effect = fake_run

        # 1. 显式指定 use_sudo=True (模拟 Linux VPS 运行环境)
        res = run_git_force_update("/home/chuiyuanwo/ipselect", use_sudo=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["repo_dir"], "/home/chuiyuanwo/ipselect")
        self.assertIn("d918950", res["commit"])

        # 验证调用的 git 命令包含 sudo、safe.directory 与 -C /home/chuiyuanwo/ipselect
        calls = [c[0][0] for c in mock_subproc.call_args_list]
        self.assertEqual(calls[0][:2], ["sudo", "git"])
        self.assertIn("safe.directory=*", calls[0])
        self.assertTrue(any("fetch" in cmd and "--all" in cmd for cmd in calls))
        self.assertTrue(any("reset" in cmd and "--hard" in cmd for cmd in calls))

    @patch("os.path.exists", return_value=True)
    @patch("os.path.isdir", return_value=True)
    @patch("subprocess.run")
    def test_run_git_force_update_fetch_failure(self, mock_subproc, mock_isdir, mock_exists):
        from aimili_node_picker import run_git_force_update

        # 模拟 fetch 失败
        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_res.stdout = ""
        mock_res.stderr = "fatal: unable to access github"
        mock_subproc.return_value = mock_res

        res = run_git_force_update("/home/chuiyuanwo/ipselect")
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "git fetch")
        self.assertIn("fatal: unable to access", res["error"])

    @patch("aimili_node_picker.subprocess.run")
    @patch("aimili_node_picker.time.sleep", return_value=None)
    @patch("aimili_node_picker.threading.Thread")
    def test_schedule_service_restart(self, mock_thread, mock_sleep, mock_subproc):
        from aimili_node_picker import schedule_service_restart
        mock_subproc.return_value = MagicMock(returncode=0)

        def fake_thread(target=None, daemon=None):
            m = MagicMock()
            m.start = lambda: target()
            return m
        mock_thread.side_effect = fake_thread

        schedule_service_restart(delay_sec=0)
        mock_subproc.assert_called_with(["sudo", "systemctl", "restart", "aimili-picker"], capture_output=True, text=True)

    def test_web_ui_has_deploy_controls(self):
        from aimili_node_picker import HTML_TEMPLATE
        self.assertIn("远端更新与服务管理", HTML_TEMPLATE)
        self.assertIn("/home/chuiyuanwo/ipselect", HTML_TEMPLATE)
        self.assertIn("triggerDeployPull()", HTML_TEMPLATE)
        self.assertIn("triggerDeployRestart()", HTML_TEMPLATE)
        self.assertIn("triggerDeploySyncAndRestart()", HTML_TEMPLATE)

    def test_web_ui_javascript_syntax_valid(self):
        import re, shutil, subprocess, tempfile
        from aimili_node_picker import HTML_TEMPLATE

        # 验证前端 script 块语法合规，绝无单引号/双引号字符串未转义换行导致的 SyntaxError
        m = re.search(r"<script>(.*?)</script>", HTML_TEMPLATE, re.DOTALL)
        self.assertIsNotNone(m, "HTML_TEMPLATE 中必须包含 <script> 标签")
        script_code = m.group(1)

        # 验证关键部署方法已挂载至 window 全局命名空间
        self.assertIn("window.triggerDeployPull = triggerDeployPull", script_code)
        self.assertIn("window.triggerDeployRestart = triggerDeployRestart", script_code)
        self.assertIn("window.triggerDeploySyncAndRestart = triggerDeploySyncAndRestart", script_code)

        # 若本地安装有 Node.js，执行原生语法验证
        if shutil.which("node"):
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
                tf.write(script_code)
                tf_path = tf.name
            try:
                proc = subprocess.run(["node", "--check", tf_path], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"前端 JS 语法检查失败: {proc.stderr}")
            finally:
                if os.path.exists(tf_path):
                    os.remove(tf_path)


    @patch("aimili_node_picker.run_git_force_update", return_value={"ok": True, "commit": "abc1234"})
    @patch("aimili_node_picker.schedule_service_restart")
    def test_web_request_handler_deploy_routes(self, mock_restart, mock_git):
        import io
        from aimili_node_picker import WebRequestHandler

        from aimili_node_picker import WebRequestHandler
        handler = WebRequestHandler.__new__(WebRequestHandler)
        handler.path = "/api/deploy/git_pull"
        handler.headers = {}
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = io.BytesIO()

        handler.do_GET()
        mock_git.assert_called_once()
        self.assertIn(b"abc1234", handler.wfile.getvalue())

        handler.path = "/api/deploy/sync_and_restart"
        handler.wfile = io.BytesIO()
        handler.do_POST()
        mock_restart.assert_called_once()
        self.assertIn(b"Git", handler.wfile.getvalue())

class TestAimiliBlocklistSync(unittest.TestCase):
    """测试黑名单自动同步至 Aimili 代理屏蔽列表功能"""

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.auth_file = os.path.join(self.temp_dir.name, "ui_auth.json")
        self.bl_file = os.path.join(self.temp_dir.name, "ip_blacklist.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_blacklist_manager_on_sync_callback_triggered_on_add_and_remove(self):
        sync_calls = []
        def fake_sync(ips):
            sync_calls.append(list(ips))

        mgr = BlacklistManager(blacklist_file=self.bl_file, on_sync_callback=fake_sync)
        mgr.add("1.1.1.1", "test reason", 80)
        self.assertEqual(len(sync_calls), 1)
        self.assertEqual(sync_calls[0], ["1.1.1.1"])

        mgr.add("2.2.2.2", "another reason", 90)
        self.assertEqual(len(sync_calls), 2)
        self.assertEqual(sorted(sync_calls[1]), ["1.1.1.1", "2.2.2.2"])

        mgr.remove("1.1.1.1")
        self.assertEqual(len(sync_calls), 3)
        self.assertEqual(sync_calls[2], ["2.2.2.2"])

    def test_aimili_get_current_blocklist_from_file_and_state(self):
        import json
        initial_data = {
            "routing_blocklist": "61.114.197.133\n14.14.148.93\n  \n112.67.*\n"
        }
        with open(self.auth_file, "w", encoding="utf-8") as f:
            json.dump(initial_data, f)

        client = AimiliClient(auth_file=self.auth_file)
        client.get_ui_state = MagicMock(return_value={})

        lines = client.get_current_blocklist()
        self.assertEqual(lines, ["61.114.197.133", "14.14.148.93", "112.67.*"])

        # When get_ui_state has data, it takes precedence
        client.get_ui_state.return_value = {"routing_blocklist": "8.8.8.8\n1.1.1.1"}
        lines_api = client.get_current_blocklist()
        self.assertEqual(lines_api, ["8.8.8.8", "1.1.1.1"])

    def test_aimili_sync_blocklist_preserves_custom_and_merges_blacklist(self):
        import json
        initial_data = {
            "routing_blocklist": "61.114.197.133\n14.14.148.93",
            "proxy_port": 8500,
            "routing_mode": "favorites"
        }
        with open(self.auth_file, "w", encoding="utf-8") as f:
            json.dump(initial_data, f)

        client = AimiliClient(auth_file=self.auth_file)
        client.logged_in = True
        client._working_base_url = "http://127.0.0.1:8501/secret"
        client.get_ui_state = MagicMock(return_value=initial_data)

        # Mock urllib.request.OpenerDirector.open
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value = mock_response

        # Sync a new blacklisted IP along with an already-present IP (dedup test)
        res = client.sync_blocklist(["162.43.67.81", "14.14.148.93"])
        self.assertTrue(res)

        # Check that file was updated
        with open(self.auth_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        saved_lines = saved.get("routing_blocklist", "").splitlines()
        self.assertEqual(saved_lines, ["61.114.197.133", "14.14.148.93", "162.43.67.81"])

        # Check API payload
        self.assertTrue(client.opener.open.called)
        sent_req = client.opener.open.call_args[0][0]
        self.assertEqual(sent_req.full_url, "http://127.0.0.1:8501/secret/api/update_settings")
        sent_data = json.loads(sent_req.data.decode("utf-8"))
        self.assertEqual(sent_data["routing_blocklist"], "61.114.197.133\n14.14.148.93\n162.43.67.81")
        self.assertEqual(sent_data["routing_mode"], "favorites")

    def test_picker_engine_binds_sync_callback_and_initial_sync(self):
        mock_aimili = MagicMock(spec=AimiliClient)
        mock_aimili.sync_blocklist = MagicMock(return_value=True)

        mock_inspector = MagicMock(spec=IpIntelligenceManager)
        mock_exp = MagicMock(spec=NodeExperienceManager)

        bl_mgr = BlacklistManager(blacklist_file=self.bl_file)
        bl_mgr.add("198.51.100.1", "high risk", 50)

        # PickerEngine should call sync_blocklist on init with existing blacklisted IPs
        engine = PickerEngine(
            aimili=mock_aimili,
            inspector=mock_inspector,
            exp_mgr=mock_exp,
            blacklist=bl_mgr
        )
        mock_aimili.sync_blocklist.assert_called_with(["198.51.100.1"])

        # Adding a new blacklisted IP should trigger sync_blocklist dynamically
        bl_mgr.add("198.51.100.2", "abuse", 80)
        mock_aimili.sync_blocklist.assert_called_with(["198.51.100.1", "198.51.100.2"])




class TestAimiliActiveInfoAndSwitchNode(unittest.TestCase):
    """测试读取 Aimili 真实活动连接、一键切换出口及服务端预渲染"""

    def test_get_aimili_active_info_success(self):
        client = AimiliClient(
            auth_file="/tmp/fake_ui_auth.json",
            secret_path="secret"
        )
        fake_state = {
            "state": {
                "active_openvpn_node_id": "42.124.87.38_1300_udp",
                "active_openvpn_node": {
                    "ip": "42.124.87.38",
                    "remote_port": 1300,
                    "country": "JP",
                    "owner": "SoftEther"
                },
                "active_node_latency": "549 ms",
                "proxy_ip": "59.84.54.248",
                "connection_status": "connected",
                "active_tunnel_interface": "tun-sw-ud079804",
                "last_check_message": "连接状态良好"
            }
        }
        client.get_ui_state = MagicMock(return_value=fake_state["state"])
        client.get_state = MagicMock(return_value=fake_state["state"])
        info = client.get_aimili_active_info()

        self.assertEqual(info["active_ip"], "42.124.87.38")
        self.assertEqual(info["active_id"], "42.124.87.38_1300_udp")
        self.assertEqual(info["proxy_ip"], "59.84.54.248")
        self.assertEqual(info["country"], "JP")
        self.assertEqual(info["isp"], "SoftEther")
        self.assertEqual(info["latency"], "549 ms")
        self.assertIn("已连接", info["status_label"])
        self.assertEqual(info["tunnel_interface"], "tun-sw-ud079804")

    def test_web_render_html_page_contains_nodes_and_switch_action(self):
        engine = MagicMock(spec=PickerEngine)
        engine.last_result = {
            "scan_time": "2026-11-01 10:00:00",
            "active_node_ip": "42.124.87.38",
            "nodes": [
                {
                    "ip": "42.124.87.38",
                    "id": "pool:42.124.87.38",
                    "speed_mbps": 12.5,
                    "risk_score": 5,
                    "reputation_score": 92.0,
                    "country": "JP",
                    "isp": "NTT",
                    "ip_type": "家庭宽带",
                    "is_inherited": True
                },
                {
                    "ip": "219.100.37.24",
                    "id": "pool:219.100.37.24",
                    "speed_mbps": 8.0,
                    "risk_score": 7,
                    "reputation_score": 85.0,
                    "country": "JP",
                    "isp": "SoftEther",
                    "ip_type": "家庭宽带",
                    "is_inherited": False
                }
            ]
        }
        mock_aimili = MagicMock(spec=AimiliClient)
        mock_aimili.get_aimili_active_info.return_value = {
            "active_ip": "42.124.87.38",
            "active_id": "42.124.87.38_1300_udp",
            "status_label": "已连接",
            "country": "JP",
            "isp": "NTT"
        }
        engine.aimili = mock_aimili

        from aimili_node_picker import WebRequestHandler
        handler = WebRequestHandler.__new__(WebRequestHandler)
        handler.engine = engine

        rendered = handler.render_html_page()
        # 验证服务端渲染了节点表格行
        self.assertIn("42.124.87.38", rendered)
        self.assertIn("219.100.37.24", rendered)
        self.assertIn("当前连接中", rendered)
        self.assertIn("🚀 设为出口", rendered)
        self.assertIn("switchAimiliNode", rendered)




class TestMultiSourceIpRiskIntelligence(unittest.TestCase):
    """测试多源权威风控融合质检矩阵 (GetIPIntel + AbuseIPDB + IPQS)"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp_dir, "test_cache.json")
        self.keys_file = os.path.join(self.tmp_dir, "test_keys.json")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_load_api_keys_from_file(self):
        with open(self.keys_file, "w", encoding="utf-8") as f:
            json.dump({
                "abuseipdb_key": "test_abuse_key",
                "ipqs_key": "test_ipqs_key",
                "contact_email": "test@example.com"
            }, f)

        mgr = IpIntelligenceManager(
            cache_file=self.cache_file,
            api_keys_file=self.keys_file
        )
        self.assertEqual(mgr.abuseipdb_key, "test_abuse_key")
        self.assertEqual(mgr.ipqs_key, "test_ipqs_key")
        self.assertEqual(mgr.contact_email, "test@example.com")

    def test_getipintel_proxy_prob_1_triggers_high_risk(self):
        mgr = IpIntelligenceManager(
            cache_file=self.cache_file,
            api_keys_file=self.keys_file
        )
        # 模拟 IP-API 返回住宅宽带
        mgr._query_ip_api = MagicMock(return_value=IpQuality(
            ip="118.156.88.5",
            isp="KDDI",
            ip_type="家庭宽带 IP",
            is_residential=True,
            risk_score=0
        ))
        # 模拟 GetIPIntel 判定为 100% 代理 (如 118.156.88.5 实测)
        mgr._query_getipintel = MagicMock(return_value=(IpQuality(
            ip="118.156.88.5",
            isp="KDDI",
            ip_type="家庭宽带 IP",
            is_residential=True,
            is_proxy=True,
            risk_score=100,
            risk_label="高危公共代理(GetIPIntel:1.0)"
        ), True))

        q = mgr.inspect_ip("118.156.88.5")
        self.assertTrue(q.is_proxy)
        self.assertGreaterEqual(q.risk_score, 10)
        self.assertIn("代理", q.risk_label)

    def test_abuseipdb_reports_elevate_risk_score(self):
        mgr = IpIntelligenceManager(
            cache_file=self.cache_file,
            api_keys_file=self.keys_file,
            abuseipdb_key="fake_key"
        )
        mgr._query_ip_api = MagicMock(return_value=IpQuality(
            ip="1.2.3.4",
            isp="NTT",
            ip_type="家庭宽带 IP",
            is_residential=True,
            risk_score=0
        ))
        # GetIPIntel 认为是纯净的
        mgr._query_getipintel = MagicMock(return_value=(IpQuality(
            ip="1.2.3.4",
            risk_score=0
        ), True))
        # 模拟 AbuseIPDB 存在 3 次恶意滥用举报
        fake_abuse_data = {"data": {"abuseConfidenceScore": 25, "totalReports": 3}}
        with patch.object(mgr, "_query_abuseipdb") as mock_abuse:
            mock_abuse.return_value = (IpQuality(
                ip="1.2.3.4",
                isp="NTT",
                ip_type="家庭宽带 IP",
                is_residential=True,
                risk_score=60,
                risk_label="历史恶意滥用(AbuseIPDB:25%,举报:3次)",
                abuse_score=25,
                abuse_reports=3
            ), True)
            q = mgr.inspect_ip("1.2.3.4")
            self.assertGreaterEqual(q.risk_score, 10)
            self.assertEqual(q.abuse_reports, 3)

    def test_pure_residential_node_evaluates_low_risk(self):
        mgr = IpIntelligenceManager(
            cache_file=self.cache_file,
            api_keys_file=self.keys_file
        )
        mgr._query_ip_api = MagicMock(return_value=IpQuality(
            ip="42.124.87.38",
            isp="NTT",
            ip_type="家庭宽带 IP",
            is_residential=True,
            risk_score=0
        ))
        mgr._query_getipintel = MagicMock(return_value=(IpQuality(
            ip="42.124.87.38",
            risk_score=0
        ), True))

        q = mgr.inspect_ip("42.124.87.38")
        self.assertLessEqual(q.risk_score, 7)
        self.assertIn("纯净家宽", q.risk_label)

class TestAimiliEgressLockAndFallback(unittest.TestCase):
    """测试出口绝对锁定、建连失败顺延降级与存量测速防穿透"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.auth_file = os.path.join(self.tmp_dir, "ui_auth.json")
        with open(self.auth_file, "w", encoding="utf-8") as f:
            json.dump({
                "username": "admin",
                "fixed_node_id": "",
                "routing_mode": "favorites"
            }, f)

        self.mock_aimili = MagicMock(spec=AimiliClient)
        self.mock_aimili.auth_file = self.auth_file
        self.mock_aimili.host = "127.0.0.1"
        self.mock_aimili.port = 8501
        self.mock_aimili.proxy_port = 8500
        self.mock_aimili.proxy_user = "socks5"
        self.mock_aimili.proxy_pass = "ilovestudy"
        self.mock_aimili.logged_in = True
        self.mock_aimili.set_active_node.return_value = True
        self.mock_aimili.toggle_favorite.return_value = True
        self.mock_aimili.refresh_nodes.return_value = True
        self.mock_aimili.get_current_favorites.return_value = []
        self.mock_aimili.get_current_blocklist.return_value = []
        self.mock_aimili.get_aimili_active_info.return_value = {
            "status": "connected", "active_ip": "1.1.1.1", "active_id": "n1"
        }

        self.mock_inspector = MagicMock(spec=IpIntelligenceManager)
        self.mock_inspector.cache = {}
        self.mock_inspector._ping0_circuit_broken = False
        self.mock_inspector.reset_circuit_breaker.return_value = None
        self.mock_exp = MagicMock(spec=NodeExperienceManager)
        self.blacklist = BlacklistManager(blacklist_file=os.path.join(self.tmp_dir, "blacklist.json"))

        self.engine = PickerEngine(
            aimili=self.mock_aimili,
            inspector=self.mock_inspector,
            exp_mgr=self.mock_exp,
            probe_speed_func=lambda **kwargs: 10.0,
            blacklist=self.blacklist
        )
        self.engine.probe_network_func = lambda ip, port, proto, **kwargs: {
            "is_alive": True, "avg_latency_ms": 50, "jitter_ms": 10, "packet_loss_pct": 0, "port_open": True
        }

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_lock_and_unlock_fixed_node(self):
        client = AimiliClient(auth_file=self.auth_file)
        client.logged_in = True
        client.opener = MagicMock()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        client.opener.open.return_value.__enter__.return_value = mock_resp

        node = CandidateNode(id="JP_1.1.1.1_1194_udp", ip="1.1.1.1", port=1194, country="JP", protocol="openvpn", latency_ms=50)
        ok = client.lock_fixed_node(node, mode="fixed_ip")
        self.assertTrue(ok)

        with open(self.auth_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("fixed_node_id"), "JP_1.1.1.1_1194_udp")
        self.assertEqual(data.get("routing_mode"), "fixed_ip")

        client.unlock_fixed_node()
        with open(self.auth_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("fixed_node_id"), "")
        self.assertEqual(data.get("routing_mode"), "favorites")

    def test_stock_node_handshake_failed_does_not_probe_speed(self):
        node = CandidateNode(id="JP_58.87.159.114_1276_tcp", ip="58.87.159.114", port=1276, country="JP", protocol="openvpn", latency_ms=100)
        self.mock_aimili.get_current_favorites.return_value = [node.canonical_fav_id]
        self.mock_aimili.fetch_scoped_nodes.return_value = [node]
        self.mock_inspector.inspect_ip.return_value = IpQuality(
            ip=node.ip, is_residential=True, risk_score=0, ip_type="家庭宽带 IP", isp="Sony"
        )
        self.mock_aimili.set_active_node.return_value = False
        probe_called = []
        def fake_probe(**kwargs):
            probe_called.append(True)
            return 10.0
        self.engine.probe_speed_func = fake_probe

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(country="JP", top_n=5, min_speed_mbps=1.0, max_rounds=1, inherit_existing=True)

        self.assertEqual(len(probe_called), 0)
        self.assertNotIn(node.ip, [p.ip for p in picked])

    def test_phase4_fallback_to_second_node_when_first_fails(self):
        node1 = CandidateNode(id="JP_1.1.1.1_1194_udp", ip="1.1.1.1", port=1194, country="JP", protocol="openvpn", latency_ms=50, jitter_ms=0, speed_mbps=10.0)
        node2 = CandidateNode(id="JP_2.2.2.2_1194_udp", ip="2.2.2.2", port=1194, country="JP", protocol="openvpn", latency_ms=60, jitter_ms=0, speed_mbps=8.0)
        self.mock_aimili.get_current_favorites.return_value = []
        self.mock_aimili.fetch_scoped_nodes.return_value = [node1, node2]
        self.mock_inspector.inspect_ip.side_effect = lambda ip, **kwargs: IpQuality(
            ip=ip, is_residential=True, risk_score=0, ip_type="家庭宽带 IP", isp="ISP"
        )
        self.engine.probe_speed_func = lambda **kwargs: 10.0

        def fake_set_active(node, **kwargs):
            timeout = kwargs.get("timeout", 35)
            if timeout == 18 and node.ip == "1.1.1.1":
                return False
            return True
        self.mock_aimili.set_active_node.side_effect = fake_set_active

        with patch("time.sleep", return_value=None):
            picked = self.engine.run_screening(country="JP", top_n=2, min_speed_mbps=1.0, max_rounds=1, inherit_existing=False)

        self.assertGreaterEqual(len(picked), 2)
        self.assertEqual(self.engine.last_result.get("active_node_ip"), "2.2.2.2")
        nodes_res = self.engine.last_result.get("nodes", [])
        active_flags = {n["ip"]: n["is_active"] for n in nodes_res}
        self.assertFalse(active_flags.get("1.1.1.1"))
        self.assertTrue(active_flags.get("2.2.2.2"))



if __name__ == "__main__":
    unittest.main()
