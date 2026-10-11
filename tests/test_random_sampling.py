import unittest
import sys
import os
import ipaddress

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import generate_random_cf_candidates, CLOUDFLARE_CIDRS


class TestRandomCandidates(unittest.TestCase):
    def test_count_and_ports(self):
        """每个随机 IP 应附带 443 与 8443 两个端口"""
        out = generate_random_cf_candidates(10, list(CLOUDFLARE_CIDRS))
        self.assertEqual(len(out), 20)
        ips = {c[0] for c in out}
        self.assertEqual(len(ips), 10)  # 10 个不同 IP
        ports = {c[1] for c in out}
        self.assertEqual(ports, {443, 8443})
        for ip, port, sp in out:
            self.assertIsInstance(ipaddress.ip_address(ip), ipaddress.IPv4Address)
            self.assertEqual(sp, 0.0)

    def test_ips_inside_cidrs(self):
        """生成的 IP 必须落在给定网段内"""
        cidrs = [ipaddress.ip_network("104.16.0.0/24")]
        out = generate_random_cf_candidates(50, cidrs)
        self.assertEqual(len(out), 100)
        for ip, _, _ in out:
            self.assertTrue(ipaddress.ip_address(ip) in cidrs[0])

    def test_empty_inputs(self):
        self.assertEqual(generate_random_cf_candidates(0, list(CLOUDFLARE_CIDRS)), [])
        self.assertEqual(generate_random_cf_candidates(10, []), [])


if __name__ == '__main__':
    unittest.main()
