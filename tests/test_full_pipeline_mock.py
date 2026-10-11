import unittest
import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import run_filter_and_export


class TestFullPipelineMock(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_pipeline_")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_run_filter_and_export_mock(self):
        out_file = os.path.join(self.test_dir, "test_best.txt")
        # 验证在纯 mock 模式下执行 pipeline 成功生成目标文件
        mock_candidates = [
            ("104.16.222.118", 8443, 0.0),
            ("104.18.223.253", 8443, 0.0),
            ("162.159.24.131", 443, 0.0)
        ]
        run_filter_and_export(
            tag="测试",
            output_file=out_file,
            mock_mode=True,
            mock_candidates=mock_candidates
        )
        self.assertTrue(os.path.exists(out_file))
        with open(out_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        self.assertGreaterEqual(len(lines), 1)
        self.assertIn("#US-测试-", lines[0])
        self.assertIn("-测试", lines[0])

    def test_full_pipeline_mock_has_speed_field(self):
        """mock 流水线最终节点应包含 speed 字段且非 None"""
        out_file = os.path.join(self.test_dir, "test_best2.txt")
        detail_file = out_file.replace(".txt", "_detail.json")
        run_filter_and_export(
            tag="速度",
            output_file=out_file,
            mock_mode=True,
            mock_candidates=[
                ("104.16.222.118", 8443, 0.0),
                ("104.18.223.253", 8443, 0.0),
            ],
        )
        import json
        with open(detail_file, "r", encoding="utf-8") as f:
            detail = json.load(f)
        self.assertGreater(len(detail["nodes"]), 0)
        self.assertIn("speed", detail["nodes"][0])
        self.assertIsNotNone(detail["nodes"][0]["speed"])


if __name__ == '__main__':
    unittest.main()
