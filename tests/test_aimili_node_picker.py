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
        self.assertIn("if (document.getElementById('chkPauseLog')?.checked) return;", HTML_TEMPLATE)

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

        res = run_git_force_update("/home/chuiyuanwo/ipselect")
        self.assertTrue(res["ok"])
        self.assertEqual(res["repo_dir"], "/home/chuiyuanwo/ipselect")
        self.assertIn("d918950", res["commit"])

        # 验证调用的 git 命令包含 -C /home/chuiyuanwo/ipselect
        calls = [c[0][0] for c in mock_subproc.call_args_list]
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


    @patch("aimili_node_picker.run_git_force_update", return_value={"ok": True, "commit": "abc1234"})
    @patch("aimili_node_picker.schedule_service_restart")
    def test_web_request_handler_deploy_routes(self, mock_restart, mock_git):
        import io
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

if __name__ == "__main__":
    unittest.main()
