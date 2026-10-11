import unittest
import sys
import os
import shutil
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import MihomoSandbox, generate_sandbox_yaml, find_clash_core


class TestMihomoSandbox(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_sandbox_")
        self.candidates = [
            ("104.16.222.118", 8443, "LAX"),
            ("162.159.24.131", 443, "SJC"),
        ]

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_generate_sandbox_yaml(self):
        yaml_content = generate_sandbox_yaml(
            candidates=self.candidates,
            api_port=19098,
            proxy_port=19998,
            uuid="e295e2a9-13ad-4251-baa8-28ee1b5aeae9",
            sni="proxy.19940407.xyz",
            path="/"
        )
        self.assertIn("mixed-port: 19998", yaml_content)
        self.assertIn("external-controller: 127.0.0.1:19098", yaml_content)
        self.assertIn("mode: direct", yaml_content)
        self.assertIn("proxies:", yaml_content)
        self.assertIn("server: 104.16.222.118", yaml_content)
        self.assertIn("port: 8443", yaml_content)
        self.assertIn("type: vless", yaml_content)
        self.assertIn("uuid: e295e2a9-13ad-4251-baa8-28ee1b5aeae9", yaml_content)
        self.assertIn("servername: proxy.19940407.xyz", yaml_content)
        self.assertIn("node_01", yaml_content)
        self.assertIn("node_02", yaml_content)

    def test_sandbox_lifecycle_mock(self):
        sandbox = MihomoSandbox(
            candidates=self.candidates,
            work_dir=self.test_dir,
            api_port=19098,
            proxy_port=19998,
            mock_mode=True
        )
        started = sandbox.start()
        self.assertTrue(started)
        self.assertTrue(sandbox.is_ready())
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "sandbox.yaml")))

        sandbox.stop()
        self.assertFalse(sandbox.is_ready())


if __name__ == '__main__':
    unittest.main()
