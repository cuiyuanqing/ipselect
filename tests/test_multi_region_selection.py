import unittest
import sys
import os
import json
import tempfile
import shutil

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import detect_node_region, run_filter_and_export, DEFAULT_REGION_QUOTAS, parse_geoip_response


class TestMultiRegionSelection(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_multi_region_")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_detect_node_region(self):
        # 1. cf-ray 机房代码检测
        self.assertEqual(detect_node_region("HKG"), "HK")
        self.assertEqual(detect_node_region("TPE"), "TW")
        self.assertEqual(detect_node_region("NRT"), "JP")
        self.assertEqual(detect_node_region("KIX"), "JP")
        self.assertEqual(detect_node_region("SIN"), "SG")
        self.assertEqual(detect_node_region("ICN"), "KR")
        self.assertEqual(detect_node_region("GMP"), "KR")
        self.assertEqual(detect_node_region("SJC"), "US")
        self.assertEqual(detect_node_region("LAX"), "US")

        # 2. 备注地域标记检测
        self.assertEqual(detect_node_region("CF", "HK-AWS-天诚反代"), "HK")
        self.assertEqual(detect_node_region("CF", "TW-HiNet中华电信"), "TW")
        self.assertEqual(detect_node_region("CF", "JP-AWS反代"), "JP")
        self.assertEqual(detect_node_region("CF", "SG-CF优选"), "SG")
        self.assertEqual(detect_node_region("CF", "KR-首尔AWS反代"), "KR")
        self.assertEqual(detect_node_region("CF", "US-SJC-01"), "US")

        # 3. 中文地名检测
        self.assertEqual(detect_node_region("CF", "优质香港反代"), "HK")
        self.assertEqual(detect_node_region("CF", "台北HiNet"), "TW")
        self.assertEqual(detect_node_region("CF", "东京AWS机房"), "JP")
        self.assertEqual(detect_node_region("CF", "新加坡狮城节点"), "SG")
        self.assertEqual(detect_node_region("CF", "韩国仁川机房"), "KR")
        self.assertEqual(detect_node_region("CF", "美西高带宽"), "US")

        # 4. 防单词子串误伤 (例如 Host1Plus 不含 US 边界，不应误判)
        self.assertEqual(detect_node_region("CF", "Host1Plus"), "US")  # 默认回退 US
        self.assertEqual(detect_node_region("CF", "HK-Host1Plus反代"), "HK")  # 明确 HK 标签

    def test_multi_region_pipeline_allocation(self):
        out_file = os.path.join(self.test_dir, "multi_region_best.txt")
        detail_file = os.path.join(self.test_dir, "multi_region_best_detail.json")

        # 构造各区域充足的 mock 候选池：HK 6个, TW 6个, JP 6个, SG 6个, US 15个
        mock_candidates = []
        for i in range(1, 7):
            mock_candidates.append((f"10.1.1.{i}", 443, 0.0, f"HK-Node-{i:02d}"))
        for i in range(1, 7):
            mock_candidates.append((f"10.2.2.{i}", 443, 0.0, f"TW-Node-{i:02d}"))
        for i in range(1, 7):
            mock_candidates.append((f"10.3.3.{i}", 443, 0.0, f"JP-Node-{i:02d}"))
        for i in range(1, 7):
            mock_candidates.append((f"10.4.4.{i}", 443, 0.0, f"SG-Node-{i:02d}"))
        for i in range(1, 16):
            mock_candidates.append((f"10.5.5.{i}", 443, 0.0, f"US-Node-{i:02d}"))

        run_filter_and_export(
            tag="公司",
            output_file=out_file,
            mock_mode=True,
            mock_candidates=mock_candidates,
            target_country="AUTO"
        )

        self.assertTrue(os.path.exists(out_file))
        self.assertTrue(os.path.exists(detail_file))

        with open(out_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        # 检查总输出节点数为 5 + 5 + 5 + 5 + 10 = 30
        self.assertEqual(len(lines), 30)

        hk_lines = [l for l in lines if "#HK-公司-" in l]
        tw_lines = [l for l in lines if "#TW-公司-" in l]
        jp_lines = [l for l in lines if "#JP-公司-" in l]
        sg_lines = [l for l in lines if "#SG-公司-" in l]
        us_lines = [l for l in lines if "#US-公司-" in l]

        self.assertEqual(len(hk_lines), 5)
        self.assertEqual(len(tw_lines), 5)
        self.assertEqual(len(jp_lines), 5)
        self.assertEqual(len(sg_lines), 5)
        self.assertEqual(len(us_lines), 10)

        # 检查各区域内部编号从 01 到 配额上限
        self.assertIn("#HK-公司-01-", hk_lines[0])
        self.assertIn("#HK-公司-05-", hk_lines[4])
        self.assertIn("#TW-公司-01-", tw_lines[0])
        self.assertIn("#TW-公司-05-", tw_lines[4])
        self.assertIn("#JP-公司-01-", jp_lines[0])
        self.assertIn("#JP-公司-05-", jp_lines[4])
        self.assertIn("#SG-公司-01-", sg_lines[0])
        self.assertIn("#SG-公司-05-", sg_lines[4])
        self.assertIn("#US-公司-01-", us_lines[0])
        self.assertIn("#US-公司-10-", us_lines[9])

        # 检查 detail.json 统计
        with open(detail_file, "r", encoding="utf-8") as f:
            detail = json.load(f)

        self.assertEqual(detail["count"], 30)
        self.assertEqual(detail["region_distribution"], {
            'HK': 5,
            'TW': 5,
            'JP': 5,
            'SG': 5,
            'US': 10
        })

    def test_parse_geoip_response(self):
        # 1. api.ip.sb 风格 JSON
        c, ip = parse_geoip_response("https://api.ip.sb/geoip", '{"country_code": "KR", "ip": "104.28.162.46"}')
        self.assertEqual(c, "KR")
        self.assertEqual(ip, "104.28.162.46")

        # 2. ipleak.net 风格 JSON
        c, ip = parse_geoip_response("https://ipleak.net/json/", '{"country_code": "US", "ip": "104.28.166.29"}')
        self.assertEqual(c, "US")
        self.assertEqual(ip, "104.28.166.29")

        # 3. cloudflare cdn-cgi/trace 纯文本
        trace_data = "fl=123\nh=cloudflare.com\nip=104.28.157.107\nts=123\nloc=US\nwarp=off\n"
        c, ip = parse_geoip_response("https://cloudflare.com/cdn-cgi/trace", trace_data)
        self.assertEqual(c, "US")
        self.assertEqual(ip, "104.28.157.107")

        # 4. ip-api.com 风格 JSON
        c, ip = parse_geoip_response("http://ip-api.com/json", '{"countryCode": "JP", "query": "104.28.162.99"}')
        self.assertEqual(c, "JP")
        self.assertEqual(ip, "104.28.162.99")

    def test_kr_exit_not_misclassified_as_us(self):
        # 验证 ICN 节点明确归入 KR，不会被错误冠以 US 标签
        from filter_us_nodes import format_node_line
        reg = detect_node_region("ICN", "", "123.111.169.70")
        self.assertEqual(reg, "KR")

        line = format_node_line("123.111.169.70", 443, 6.5, 1, "联通优选", 7.8, "ICN", region=reg)
        self.assertIn("#KR-联通优选-01-", line)
        self.assertNotIn("#US-", line)


if __name__ == '__main__':
    unittest.main()
