import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import calculate_rigorous_score


class TestScoring(unittest.TestCase):
    def test_ideal_us_west_node(self):
        # 顶级节点：0 丢包，美西机房，RTT 180ms，抖动 20ms，真实速度 8.5MB/s，CF 官方 IP
        node = {
            'loss_rate': 0.0,
            'median_rtt': 180.0,
            'jitter': 20.0,
            'speed': 8.5,
            'is_us_west': True,
            'is_cf': True
        }
        score = calculate_rigorous_score(node)
        # 3.0(稳定) + 3.5(测速) + 2.5(延迟) + 1.0(抖动) + 0.5(美西) = 10.5 -> 封顶 10.0
        self.assertEqual(score, 10.0)

    def test_average_node(self):
        # 中等节点：0 丢包，美东机房(非美西)，RTT 240ms，抖动 60ms，真实速度 2.5MB/s，CF 官方
        node = {
            'loss_rate': 0.0,
            'median_rtt': 240.0,
            'jitter': 60.0,
            'speed': 2.5,
            'is_us_west': False,
            'is_cf': True
        }
        score = calculate_rigorous_score(node)
        # 应该在 6.0 ~ 7.5 之间，显著与顶级节点拉开区分度
        self.assertGreaterEqual(score, 6.0)
        self.assertLessEqual(score, 7.5)

    def test_poor_lossy_node(self):
        # 较差节点：丢包 1/6 (约 0.16)，非美西，RTT 320ms，抖动 150ms，速度 0.3MB/s
        node = {
            'loss_rate': 1 / 6,
            'median_rtt': 320.0,
            'jitter': 150.0,
            'speed': 0.3,
            'is_us_west': False,
            'is_cf': True
        }
        score = calculate_rigorous_score(node)
        # 显著低分 (< 3.5)
        self.assertLessEqual(score, 3.5)

    def test_ideal_apac_node(self):
        # 方案 A 顶级亚太节点：0 丢包，香港/东京机房，RTT 45ms，抖动 10ms，真实速度 9.0MB/s，CF 官方 IP
        node = {
            'loss_rate': 0.0,
            'median_rtt': 45.0,
            'jitter': 10.0,
            'speed': 9.0,
            'is_apac': True,
            'is_us_west': False,
            'is_cf': True
        }
        score = calculate_rigorous_score(node)
        # 3.0(稳定) + 3.5(测速) + 2.5(延迟) + 1.0(抖动) + 0.5(机房) = 10.5 -> 封顶 10.0
        self.assertEqual(score, 10.0)

    def test_apac_low_latency_superiority(self):
        # 相同带宽 (3.0MB/s) 下，亚太极速 (50ms) 得分显著高于跨洋延迟 (260ms)
        apac_node = {
            'loss_rate': 0.0,
            'median_rtt': 50.0,
            'jitter': 20.0,
            'speed': 3.0,
            'is_apac': True,
            'is_us_west': False,
            'is_cf': True
        }
        us_east_node = {
            'loss_rate': 0.0,
            'median_rtt': 260.0,
            'jitter': 20.0,
            'speed': 3.0,
            'is_apac': False,
            'is_us_west': False,
            'is_cf': True
        }
        score_apac = calculate_rigorous_score(apac_node)
        score_us = calculate_rigorous_score(us_east_node)
        self.assertGreater(score_apac, score_us + 1.0)


if __name__ == '__main__':
    unittest.main()

