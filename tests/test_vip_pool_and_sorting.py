# -*- coding: utf-8 -*-
"""
tests/test_vip_pool_and_sorting.py
测试往期节点复用、高价值 VIP 节点留存池（>= 8.0 MB/s）与跨批次区域聚合排序。
"""

import json
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.filter_us_nodes import (
    sort_qualified_nodes_by_region_and_speed,
    load_warm_and_vip_seeds,
    update_vip_nodes_pool,
    DEFAULT_REGION_PRIORITY,
)


def test_sort_qualified_nodes_by_region_and_speed():
    """验证跨批次节点重排: 区域优先分组 (US -> HK -> JP) 且各组内部严格按速度降序"""
    mock_nodes = [
        {"ip": "1.1.1.1", "port": 443, "exit_country": "JP", "speed": 7.0, "score": 6.0, "e2e_avg_rtt": 400.0, "round": 1},
        {"ip": "2.2.2.2", "port": 443, "exit_country": "US", "speed": 8.5, "score": 5.0, "e2e_avg_rtt": 900.0, "round": 2},
        {"ip": "3.3.3.3", "port": 443, "exit_country": "HK", "speed": 6.0, "score": 5.5, "e2e_avg_rtt": 350.0, "round": 1},
        {"ip": "4.4.4.4", "port": 443, "exit_country": "US", "speed": 5.0, "score": 4.5, "e2e_avg_rtt": 950.0, "round": 1},
        {"ip": "5.5.5.5", "port": 443, "exit_country": "US", "speed": 9.2, "score": 5.8, "e2e_avg_rtt": 880.0, "round": 3},
        {"ip": "6.6.6.6", "port": 443, "exit_country": "HK", "speed": 7.5, "score": 6.2, "e2e_avg_rtt": 320.0, "round": 2},
        {"ip": "7.7.7.7", "port": 443, "exit_country": "DE", "speed": 4.0, "score": 3.0, "e2e_avg_rtt": 1100.0, "round": 1},
    ]

    sorted_nodes = sort_qualified_nodes_by_region_and_speed(
        mock_nodes,
        env_tag="联通优选",
        region_priority=["US", "HK", "JP", "DE"]
    )

    # 1. 验证顺序: 前 3 个必须是 US 节点
    assert [n["ip"] for n in sorted_nodes[:3]] == ["5.5.5.5", "2.2.2.2", "4.4.4.4"]
    # 验证 US 组内速度严格递减: 9.2 -> 8.5 -> 5.0
    assert sorted_nodes[0]["speed"] == 9.2
    assert sorted_nodes[1]["speed"] == 8.5
    assert sorted_nodes[2]["speed"] == 5.0
    # 验证 US 命名与序号重新生成
    assert sorted_nodes[0]["remark"] == "US-联通优选-01-9.20MB/s"
    assert sorted_nodes[1]["remark"] == "US-联通优选-02-8.50MB/s"
    assert sorted_nodes[2]["remark"] == "US-联通优选-03-5.00MB/s"

    # 2. 接着必须是 HK 节点: 7.5 -> 6.0
    assert [n["ip"] for n in sorted_nodes[3:5]] == ["6.6.6.6", "3.3.3.3"]
    assert sorted_nodes[3]["remark"] == "HK-联通优选-01-7.50MB/s"
    assert sorted_nodes[4]["remark"] == "HK-联通优选-02-6.00MB/s"

    # 3. 接着是 JP 节点 (7.0)
    assert sorted_nodes[5]["ip"] == "1.1.1.1"
    assert sorted_nodes[5]["remark"] == "JP-联通优选-01-7.00MB/s"

    # 4. 最后是 DE 节点 (4.0)
    assert sorted_nodes[6]["ip"] == "7.7.7.7"
    assert sorted_nodes[6]["remark"] == "DE-联通优选-01-4.00MB/s"


def test_update_vip_nodes_pool():
    """验证 VIP 节点库沉淀(>= 8.0 MB/s)与低速失效节点淘汰机制"""
    with tempfile.TemporaryDirectory() as tmpdir:
        vip_file = os.path.join(tmpdir, "vip_nodes.json")

        # 初始预置一个 VIP 节点和一个后来掉速的节点
        init_vip_data = {
            "updated_at": "2026-10-12 12:00:00",
            "vip_threshold": 8.0,
            "nodes": [
                {"ip": "100.1.1.1", "port": 443, "speed": 8.2, "exit_country": "US", "colo": "SJC"},
                {"ip": "100.2.2.2", "port": 443, "speed": 8.1, "exit_country": "HK", "colo": "HKG"},
            ]
        }
        with open(vip_file, "w", encoding="utf-8") as f:
            json.dump(init_vip_data, f)

        # 本轮测试结果:
        # 100.1.1.1 本轮依然强劲 (8.8 MB/s) -> 保留并更新
        # 100.2.2.2 本轮断流/掉速至 2.0 MB/s -> 淘汰
        # 100.3.3.3 新涌现出极速节点 (9.5 MB/s) -> 新增沉淀
        # 100.4.4.4 普通达标节点 (5.5 MB/s) -> 不进 VIP
        tested_nodes = [
            {"ip": "100.1.1.1", "port": 443, "speed": 8.8, "exit_country": "US", "colo": "SJC"},
            {"ip": "100.2.2.2", "port": 443, "speed": 2.0, "exit_country": "HK", "colo": "HKG"},
            {"ip": "100.3.3.3", "port": 443, "speed": 9.5, "exit_country": "US", "colo": "LAX"},
            {"ip": "100.4.4.4", "port": 443, "speed": 5.5, "exit_country": "SG", "colo": "SIN"},
        ]

        saved_vip = update_vip_nodes_pool(
            vip_file=vip_file,
            tested_nodes=tested_nodes,
            vip_threshold=8.0,
            min_speed_threshold=3.0
        )

        vip_ips = [n["ip"] for n in saved_vip["nodes"]]
        assert "100.1.1.1" in vip_ips
        assert "100.3.3.3" in vip_ips
        assert "100.2.2.2" not in vip_ips  # 已淘汰
        assert "100.4.4.4" not in vip_ips  # 未达到 8.0 门槛

        # 检查磁盘文件更新
        with open(vip_file, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        assert len(disk_data["nodes"]) == 2


def test_load_warm_and_vip_seeds():
    """验证从历史 best_us 文件与 VIP 库中加载种子"""
    with tempfile.TemporaryDirectory() as tmpdir:
        vip_file = os.path.join(tmpdir, "vip_nodes.json")
        txt_file = os.path.join(tmpdir, "best_us.txt")
        detail_file = os.path.join(tmpdir, "best_us_detail.json")

        with open(vip_file, "w", encoding="utf-8") as f:
            json.dump({
                "nodes": [
                    {"ip": "11.11.11.11", "port": 443, "speed": 8.5, "exit_country": "US", "colo": "SJC"}
                ]
            }, f)

        with open(txt_file, "w", encoding="utf-8") as f:
            f.write("22.22.22.22:443#US-公司-01-6.50MB/s\n33.33.33.33:8443#HK-公司-01-5.20MB/s\n")

        with open(detail_file, "w", encoding="utf-8") as f:
            json.dump({
                "nodes": [
                    {"ip": "22.22.22.22", "port": "443", "exit_country": "US", "colo": "SJC"},
                    {"ip": "33.33.33.33", "port": "8443", "exit_country": "HK", "colo": "HKG"},
                ]
            }, f)

        seeds = load_warm_and_vip_seeds(
            workdir=tmpdir,
            output_file="best_us.txt",
            vip_file=vip_file
        )

        seed_tuples = [(s["ip"], s["port"]) for s in seeds]
        assert ("11.11.11.11", 443) in seed_tuples
        assert ("22.22.22.22", 443) in seed_tuples
        assert ("33.33.33.33", 8443) in seed_tuples
