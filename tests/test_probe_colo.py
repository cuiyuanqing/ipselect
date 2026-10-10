import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import parse_colo_from_headers, is_us_colo, is_us_west_colo, is_apac_colo, is_top_colo, US_WEST_DCS, ALL_US_DCS, APAC_FAST_DCS, TOP_INGRESS_DCS


class TestProbeColo(unittest.TestCase):
    def test_parse_cf_ray_standard(self):
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "Date: Fri, 09 Oct 2026 15:00:00 GMT\r\n"
            "Server: cloudflare\r\n"
            "cf-ray: 8d29b12e3f4a0123-SJC\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        colo = parse_colo_from_headers(headers)
        self.assertEqual(colo, "SJC")
        self.assertTrue(is_us_colo(colo))
        self.assertTrue(is_us_west_colo(colo))
        self.assertTrue(is_top_colo(colo))

    def test_parse_cf_ray_europe_rejected(self):
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "cf-ray: 8d29b12e3f4a0123-FRA\r\n\r\n"
        )
        colo = parse_colo_from_headers(headers)
        self.assertEqual(colo, "FRA")
        self.assertFalse(is_us_colo(colo))
        self.assertFalse(is_us_west_colo(colo))
        self.assertFalse(is_apac_colo(colo))

    def test_parse_cf_ray_us_east(self):
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "cf-ray: 8d29b12e3f4a0123-IAD\r\n\r\n"
        )
        colo = parse_colo_from_headers(headers)
        self.assertEqual(colo, "IAD")
        self.assertTrue(is_us_colo(colo))
        self.assertFalse(is_us_west_colo(colo))

    def test_parse_cf_ray_apac_fast(self):
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "cf-ray: 8d29b12e3f4a0123-HKG\r\n\r\n"
        )
        colo = parse_colo_from_headers(headers)
        self.assertEqual(colo, "HKG")
        self.assertTrue(is_apac_colo(colo))
        self.assertTrue(is_top_colo(colo))
        self.assertFalse(is_us_colo(colo))

    def test_parse_no_cf_ray(self):
        headers = "HTTP/1.1 502 Bad Gateway\r\nServer: nginx\r\n\r\n"
        colo = parse_colo_from_headers(headers)
        self.assertIsNone(colo)
        self.assertFalse(is_us_colo(colo))


if __name__ == '__main__':
    unittest.main()
