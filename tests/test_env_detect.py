import unittest
import sys
import os

# 将 scripts 目录加入 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import detect_network_environment, infer_environment_from_ips


class TestEnvDetect(unittest.TestCase):
    def test_infer_home_environment(self):
        tag, output = infer_environment_from_ips(['192.168.0.105', '127.0.0.1'])
        self.assertEqual(tag, '家庭')
        self.assertEqual(output, 'home_us_best_node.txt')

    def test_infer_company_environment(self):
        tag, output = infer_environment_from_ips(['10.10.18.22', '127.0.0.1'])
        self.assertEqual(tag, '公司')
        self.assertEqual(output, 'best_us.txt')

    def test_infer_default_fallback(self):
        tag, output = infer_environment_from_ips(['172.16.1.5', '127.0.0.1'])
        self.assertEqual(tag, '公司')
        self.assertEqual(output, 'best_us.txt')

    def test_explicit_override(self):
        tag, output = detect_network_environment(cli_tag='自定义', cli_output='custom.txt')
        self.assertEqual(tag, '自定义')
        self.assertEqual(output, 'custom.txt')


if __name__ == '__main__':
    unittest.main()
