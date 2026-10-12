import unittest
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from stage3_quality_gate import (
    _match_title,
    GOOGLE_TITLE_PATTERNS,
    YOUTUBE_TITLE_PATTERNS,
    fetch_homepage_html,
    switch_sandbox_proxy,
    probe_tunnel_delay,
)
from filter_us_nodes import (
    run_stage3_real_quality_gate,
    MihomoSandbox,
)


class TestStage3QualityGate(unittest.TestCase):
    def test_match_title_google_patterns(self):
        cases = [
            b"<html><head><title>Google</title></head><body></body></html>",
            b"<html><head><title>Google Search</title></head></html>",
            b"<html><head><title>\xe8\xb0\xb7\xe6\xad\x8c\xe6\x90\x9c\xe7\xb4\xa2</title></head></html>",  # 谷歌搜索
            b"<html><head><title> Google - News </title></head></html>",
        ]
        for html in cases:
            matched = _match_title(html, GOOGLE_TITLE_PATTERNS)
            self.assertTrue(bool(matched), f"Failed to match title in: {html}")

    def test_match_title_youtube_patterns(self):
        cases = [
            b"<html><head><title>YouTube</title></head></html>",
            b"<html><head><title>YouTube - Home</title></head></html>",
            b"<html><head><title> Watch on YouTube </title></head></html>",
        ]
        for html in cases:
            matched = _match_title(html, YOUTUBE_TITLE_PATTERNS)
            self.assertTrue(bool(matched), f"Failed to match title in: {html}")

    def test_match_title_negative(self):
        html = b"<html><head><title>Cloudflare 403 Forbidden</title></head></html>"
        self.assertEqual(_match_title(html, GOOGLE_TITLE_PATTERNS), "")
        self.assertEqual(_match_title(html, YOUTUBE_TITLE_PATTERNS), "")

    def test_run_stage3_mock_mode_has_proxy_name(self):
        candidates = [
            ("104.16.222.118", 8443, 0.0, "LAX", 180.0, "CF-LAX"),
            ("162.159.24.131", 443, 0.0, "HKG", 120.0, "CF-HK"),
        ]
        sandbox = MihomoSandbox(candidates=candidates, mock_mode=True)
        results, status = run_stage3_real_quality_gate(candidates, sandbox, target_country="AUTO")
        self.assertEqual(status, "success")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["proxy_name"], "node_01")
        self.assertEqual(results[1]["proxy_name"], "node_02")
        self.assertEqual(results[0]["region"], "US")
        self.assertEqual(results[1]["region"], "HK")

    @patch("filter_us_nodes.probe_e2e_delay")
    @patch("filter_us_nodes.switch_sandbox_proxy")
    @patch("filter_us_nodes.probe_ip_sb_exit")
    @patch("stage3_quality_gate.fetch_homepage_html")
    def test_run_stage3_two_tier_pipeline(self, mock_fetch, mock_exit, mock_switch, mock_delay):
        candidates = [
            ("104.16.222.118", 8443, 0.0, "LAX", 180.0, "CF-LAX"),
            ("162.159.24.131", 443, 0.0, "HKG", 120.0, "CF-HK"),
            ("1.1.1.1", 443, 0.0, "SJC", 190.0, "Dead-Node"),
        ]
        sandbox = MagicMock()
        sandbox.mock_mode = False

        # 模拟 Tier 1: node_01 和 node_02 隧道通畅，node_03 失败
        def mock_delay_fn(name, url, **kwargs):
            if name in ("node_01", "node_02"):
                return 150
            return None
        mock_delay.side_effect = mock_delay_fn

        # 模拟 Tier 2: switch 成功
        mock_switch.return_value = True

        # 模拟出口探测: node_01 出口 US，node_02 出口 HK
        def mock_exit_fn(**kwargs):
            return True, "US", "104.28.1.1"
        mock_exit.side_effect = mock_exit_fn

        # 模拟真实页面: 返回包含 Google 的 HTML
        mock_fetch.return_value = (b"<html><head><title>Google</title></head></html>", "")

        results, status = run_stage3_real_quality_gate(candidates, sandbox, target_country="AUTO")
        self.assertEqual(len(results), 2)
        proxy_names = {r["proxy_name"] for r in results}
        self.assertEqual(proxy_names, {"node_01", "node_02"})
        self.assertEqual(results[0]["quality_gate_version"], 2)
        self.assertEqual(results[0]["real_page_pass_rate"], 1.0)

    def test_fallback_injects_proxy_name(self):
        import tempfile
        import shutil
        import json
        from filter_us_nodes import run_filter_and_export

        test_dir = tempfile.mkdtemp(prefix="test_fallback_")
        try:
            out_file = os.path.join(test_dir, "test_out.txt")
            detail_file = os.path.join(test_dir, "test_out_detail.json")
            candidates = [
                ("104.16.222.118", 8443, 0.0),
                ("162.159.24.131", 443, 0.0),
            ]
            run_filter_and_export(
                tag="公司",
                output_file=out_file,
                mock_mode=True,
                sandbox_enable=0,
                mock_candidates=candidates
            )
            self.assertTrue(os.path.exists(detail_file))
            with open(detail_file, "r", encoding="utf-8") as f:
                detail = json.load(f)
            nodes = detail.get("nodes", [])
            self.assertGreater(len(nodes), 0)
            for node in nodes:
                self.assertIn("proxy_name", node)
                self.assertNotEqual(node["proxy_name"], "node_??")
                self.assertTrue(node["proxy_name"].startswith("node_"))
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
