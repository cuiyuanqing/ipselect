#!/usr/bin/env python3
"""
tests/test_packaging.py
Unit tests verifying IPK packaging, control metadata, payload integrity,
permissions, and one-key deployment tool.
"""

import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.build_ipk import (
    PAYLOAD_MAP,
    PKG_ARCH,
    PKG_FULL_VERSION,
    PKG_MAINTAINER,
    PKG_NAME,
    build_ipk,
    read_ipk,
)


class TestPackaging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = REPO_ROOT
        cls.dist_dir = cls.repo_root / "dist"
        cls.default_ipk = (
            cls.dist_dir / f"{PKG_NAME}_{PKG_FULL_VERSION}_{PKG_ARCH}.ipk"
        )
        # Ensure fresh default build
        cls.dist_dir.mkdir(parents=True, exist_ok=True)
        build_ipk(cls.repo_root, cls.default_ipk, container_format="tar.gz")

    def test_01_build_ipk_generates_file(self):
        """Verify build_ipk generates dist/luci-app-ipselect_1.0.0-1_all.ipk with non-zero size."""
        self.assertTrue(
            self.default_ipk.exists(),
            f"Expected package file {self.default_ipk} does not exist",
        )
        file_size = self.default_ipk.stat().st_size
        self.assertGreater(
            file_size, 1024, f"Package file too small ({file_size} bytes)"
        )
        print(f"[OK] Generated {self.default_ipk.name} ({file_size} bytes)")

    def test_02_ipk_archive_structure(self):
        """Verify the .ipk container contains debian-binary, control.tar.gz, and data.tar.gz."""
        members = read_ipk(self.default_ipk)
        self.assertIn("debian-binary", members)
        self.assertIn("control.tar.gz", members)
        self.assertIn("data.tar.gz", members)

        # debian-binary should specify version 2.0
        debian_binary = members["debian-binary"]
        self.assertEqual(debian_binary.strip(), b"2.0")
        print("[OK] Verified outer container structure and debian-binary=2.0")

    def test_03_control_metadata_and_scripts(self):
        """Verify control metadata fields and postinst script in control.tar.gz."""
        members = read_ipk(self.default_ipk)
        control_tgz_bytes = members["control.tar.gz"]

        control_content = None
        postinst_content = None
        postinst_mode = None

        with tarfile.open(fileobj=io.BytesIO(control_tgz_bytes), mode="r:gz") as tar:
            for member in tar.getmembers():
                clean_name = member.name.lstrip("./")
                if clean_name == "control":
                    f = tar.extractfile(member)
                    control_content = f.read().decode("utf-8")
                elif clean_name == "postinst":
                    postinst_mode = member.mode
                    f = tar.extractfile(member)
                    postinst_content = f.read().decode("utf-8")

        self.assertIsNotNone(control_content, "Missing control file in control.tar.gz")
        self.assertIsNotNone(postinst_content, "Missing postinst in control.tar.gz")

        # Verify control metadata fields
        control_lines = {
            line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
            for line in control_content.strip().splitlines()
            if ":" in line
        }

        self.assertEqual(control_lines.get("Package"), PKG_NAME)
        self.assertEqual(control_lines.get("Version"), PKG_FULL_VERSION)
        self.assertEqual(control_lines.get("Maintainer"), PKG_MAINTAINER)
        self.assertEqual(control_lines.get("Architecture"), PKG_ARCH)
        self.assertIn("python3", control_lines.get("Depends", ""))
        self.assertIn("python3-requests", control_lines.get("Depends", ""))

        # Verify postinst script
        self.assertIn("/usr/bin/ipselect-runner", postinst_content)
        self.assertIn("/etc/init.d/ipselect", postinst_content)
        self.assertIn("/etc/init.d/ipselect enable", postinst_content)
        # Check executable bit on postinst
        self.assertTrue(
            bool(postinst_mode & 0o111),
            f"postinst should be executable, got mode {oct(postinst_mode)}",
        )
        print("[OK] Verified control metadata and postinst permissions")

    def test_04_data_payload_components_and_permissions(self):
        """Verify data.tar.gz contains all 7 core components with correct paths and permissions."""
        members = read_ipk(self.default_ipk)
        data_tgz_bytes = members["data.tar.gz"]

        extracted_files = {}
        with tarfile.open(fileobj=io.BytesIO(data_tgz_bytes), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.isreg():
                    clean_name = member.name.lstrip("./")
                    f = tar.extractfile(member)
                    extracted_files[clean_name] = (member.mode, f.read())

        # Expected 7 files
        expected_targets = {
            target_rel: (src_rel, mode) for src_rel, target_rel, mode in PAYLOAD_MAP
        }

        self.assertEqual(
            set(extracted_files.keys()),
            set(expected_targets.keys()),
            f"Mismatch in data.tar.gz files: {set(extracted_files.keys()) ^ set(expected_targets.keys())}",
        )

        for target_rel, (src_rel, expected_mode) in expected_targets.items():
            actual_mode, actual_data = extracted_files[target_rel]
            self.assertEqual(
                actual_mode,
                expected_mode,
                f"Mode mismatch for {target_rel}: expected {oct(expected_mode)}, got {oct(actual_mode)}",
            )
            # Verify executable bit specifically for runner and service
            if "bin" in target_rel or "init.d" in target_rel:
                self.assertEqual(
                    actual_mode,
                    0o755,
                    f"Executable bit missing on {target_rel} (got {oct(actual_mode)})",
                )

            # Compare content to repository file (normalized LF)
            repo_file = self.repo_root / src_rel
            expected_content = repo_file.read_bytes().replace(b"\r\n", b"\n")
            if expected_content and not expected_content.endswith(b"\n"):
                expected_content += b"\n"

            self.assertEqual(
                actual_data,
                expected_content,
                f"Content mismatch for payload {target_rel}",
            )

        print("[OK] Verified all 7 payload files, exact contents, and POSIX permissions")

    def test_05_ar_container_format(self):
        """Verify ar format container generation, parsing, and payload integrity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ar_ipk = Path(tmpdir) / "test_ar.ipk"
            build_ipk(self.repo_root, ar_ipk, container_format="ar")

            self.assertTrue(ar_ipk.exists())
            raw_bytes = ar_ipk.read_bytes()
            self.assertTrue(
                raw_bytes.startswith(b"!<arch>\n"),
                "ar package must begin with !<arch>\\n magic",
            )

            members = read_ipk(ar_ipk)
            self.assertIn("debian-binary", members)
            self.assertIn("control.tar.gz", members)
            self.assertIn("data.tar.gz", members)

            # Check that data.tar.gz inside ar can also be extracted and has 7 files
            with tarfile.open(
                fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz"
            ) as tar:
                reg_files = [m.name.lstrip("./") for m in tar.getmembers() if m.isreg()]
                self.assertEqual(len(reg_files), 7)

        print("[OK] Verified ar format container packaging and unpacking")

    def test_06_build_ipk_cli(self):
        """Verify build_ipk.py CLI executes cleanly."""
        res = subprocess.run(
            [sys.executable, str(self.repo_root / "scripts" / "build_ipk.py"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Pure Python IPK Builder", res.stdout)

        with tempfile.TemporaryDirectory() as tmpdir:
            custom_ipk = Path(tmpdir) / "cli_test.ipk"
            res = subprocess.run(
                [
                    sys.executable,
                    str(self.repo_root / "scripts" / "build_ipk.py"),
                    "-o",
                    str(custom_ipk),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertTrue(custom_ipk.exists())

        print("[OK] Verified build_ipk.py CLI execution")

    def test_07_deploy_to_router_dry_run(self):
        """Verify deploy_to_router.py CLI runs in dry-run mode for both sync and ipk modes."""
        deploy_script = self.repo_root / "scripts" / "deploy_to_router.py"
        res = subprocess.run(
            [sys.executable, str(deploy_script), "--dry-run"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Mode A: Hot-syncing files to router", res.stdout)
        self.assertIn("Purging LuCI cache and restarting services", res.stdout)

        res_ipk = subprocess.run(
            [sys.executable, str(deploy_script), "--mode", "ipk", "--dry-run"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_ipk.returncode, 0)
        self.assertIn("Mode B: IPK installation", res_ipk.stdout)

        print("[OK] Verified deploy_to_router.py dry-run in both modes")


if __name__ == "__main__":
    unittest.main()
