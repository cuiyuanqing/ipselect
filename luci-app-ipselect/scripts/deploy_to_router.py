#!/usr/bin/env python3
"""
scripts/deploy_to_router.py
One-key deployment and LuCI reload tool for luci-app-ipselect.
Supports:
  - Mode A (sync): Hot push local source files directly to target router directories.
  - Mode B (ipk): Upload and install generated .ipk package (via opkg or payload extract).
  - Post-deploy cache purge & service reload (rpcd, uhttpd, ipselect).
"""

import argparse
import io
import os
import posixpath
import sys
import time
from pathlib import Path

# Add project root to sys.path to allow importing build_ipk if needed
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import paramiko

    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

# Mapping of source files to target paths and permissions on the router
DEPLOY_FILES = [
    ("root/etc/config/ipselect", "/etc/config/ipselect", 0o644),
    ("root/etc/init.d/ipselect", "/etc/init.d/ipselect", 0o755),
    ("root/usr/bin/ipselect-runner", "/usr/bin/ipselect-runner", 0o755),
    ("root/usr/share/luci/menu.d/luci-app-ipselect.json", "/usr/share/luci/menu.d/luci-app-ipselect.json", 0o644),
    ("root/usr/share/rpcd/acl.d/luci-app-ipselect.json", "/usr/share/rpcd/acl.d/luci-app-ipselect.json", 0o644),
    ("luasrc/controller/ipselect.lua", "/usr/lib/lua/luci/controller/ipselect.lua", 0o644),
    ("htdocs/luci-static/resources/view/ipselect/overview.js", "/www/luci-static/resources/view/ipselect/overview.js", 0o644),
]


def normalize_lf(data: bytes) -> bytes:
    """Normalize text content to Unix LF line endings."""
    data = data.replace(b"\r\n", b"\n")
    if data and not data.endswith(b"\n"):
        data += b"\n"
    return data


def run_ssh_cmd(ssh_client, cmd: str) -> tuple[int, str, str]:
    """Execute command on remote host via Paramiko SSHClient."""
    stdin, stdout, stderr = ssh_client.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return exit_code, out, err


def sftp_mkdir_p(sftp_client, remote_directory: str):
    """Recursively create remote directory via SFTP."""
    dirs = []
    current = remote_directory
    while current and current not in ("/", ""):
        dirs.append(current)
        current = posixpath.dirname(current)
    dirs.reverse()

    for d in dirs:
        try:
            sftp_client.stat(d)
        except IOError:
            try:
                sftp_client.mkdir(d)
            except IOError:
                pass


def deploy_sync_mode(ssh_client, sftp_client, repo_root: Path, dry_run: bool = False) -> bool:
    """
    Mode A: Hot push files directly to corresponding system directories.
    """
    print("\n[deploy] === Mode A: Hot-syncing files to router ===")
    for src_rel, remote_path, mode in DEPLOY_FILES:
        local_path = repo_root / src_rel
        if not local_path.exists():
            print(f"[deploy] ERROR: Local source file missing: {local_path}")
            return False

        print(f"[deploy] Syncing {src_rel} -> {remote_path} (mode {oct(mode)}) ...")
        if dry_run:
            continue

        raw_bytes = local_path.read_bytes()
        norm_bytes = normalize_lf(raw_bytes)

        remote_dir = posixpath.dirname(remote_path)
        sftp_mkdir_p(sftp_client, remote_dir)

        with sftp_client.open(remote_path, "wb") as rf:
            rf.write(norm_bytes)
        sftp_client.chmod(remote_path, mode)

    if not dry_run:
        # Ensure executable bits and service enablement
        print("[deploy] Setting executable permissions and enabling service...")
        run_ssh_cmd(ssh_client, "chmod 755 /usr/bin/ipselect-runner /etc/init.d/ipselect")
        code, out, err = run_ssh_cmd(ssh_client, "/etc/init.d/ipselect enable")
        if code != 0 and err:
            print(f"[deploy] Note: /etc/init.d/ipselect enable: {err}")

    print("[deploy] File sync completed successfully.")
    return True


def deploy_ipk_mode(
    ssh_client,
    sftp_client,
    repo_root: Path,
    ipk_path: Path = None,
    dry_run: bool = False,
) -> bool:
    """
    Mode B: Upload and install .ipk package.
    """
    print("\n[deploy] === Mode B: IPK installation ===")
    if ipk_path is None or not ipk_path.exists():
        default_ipk = repo_root / "dist" / "luci-app-ipselect_1.0.0-1_all.ipk"
        if not default_ipk.exists():
            print(f"[deploy] IPK file not found at {default_ipk}. Building now...")
            if not dry_run:
                from scripts.build_ipk import build_ipk

                build_ipk(repo_root, default_ipk)
        ipk_path = default_ipk

    remote_ipk = f"/tmp/{ipk_path.name}"
    print(f"[deploy] Uploading {ipk_path} -> {remote_ipk} ...")

    if dry_run:
        print("[deploy] [dry-run] Would upload IPK and run opkg install")
        return True

    sftp_client.put(str(ipk_path), remote_ipk)

    # Check if opkg is available on router
    code, out, err = run_ssh_cmd(ssh_client, "which opkg")
    if code == 0:
        install_cmd = f"opkg install --force-reinstall {remote_ipk}"
        print(f"[deploy] Running opkg install: {install_cmd} ...")
        code, out, err = run_ssh_cmd(ssh_client, install_cmd)
        print(f"[deploy] opkg output:\n{out}")
        if err:
            print(f"[deploy] opkg stderr: {err}")
        if code != 0:
            print(f"[deploy] ERROR: opkg install returned non-zero exit code {code}")
            return False
    else:
        print(
            "[deploy] opkg not found (apk/modern OpenWrt environment). Unpacking IPK payload..."
        )
        unpack_script = (
            f"tmpdir=$(mktemp -d /tmp/ipk-unpack.XXXXXX) && "
            f"tar -xzf {remote_ipk} -C \"$tmpdir\" && "
            f"tar -xzf \"$tmpdir/data.tar.gz\" -C / && "
            f"tar -xzf \"$tmpdir/control.tar.gz\" -C \"$tmpdir\" && "
            f"[ -f \"$tmpdir/postinst\" ] && chmod 755 \"$tmpdir/postinst\" && \"$tmpdir/postinst\" ; "
            f"rm -rf \"$tmpdir\""
        )
        code, out, err = run_ssh_cmd(ssh_client, unpack_script)
        if code != 0:
            print(f"[deploy] ERROR: IPK extraction failed (code {code}): {err}")
            return False
        print("[deploy] IPK unpacked and postinst executed successfully.")

    return True


def reload_router_services(ssh_client, dry_run: bool = False) -> bool:
    """
    Purge LuCI cache and restart rpcd, uhttpd, and ipselect.
    """
    print("\n[deploy] === Purging LuCI cache and restarting services ===")
    reload_cmds = [
        ("rm -rf /tmp/luci-indexcache* /tmp/luci-modulecache* /var/luci-indexcache* 2>/dev/null || true", "Clear LuCI index/module cache"),
        ("/etc/init.d/rpcd restart", "Restart rpcd service"),
        ("/etc/init.d/uhttpd restart", "Restart uhttpd web server"),
        ("/etc/init.d/ipselect restart", "Restart ipselect service"),
    ]

    if dry_run:
        for cmd, desc in reload_cmds:
            print(f"[deploy] [dry-run] Would execute: {cmd} ({desc})")
        return True

    success = True
    for cmd, desc in reload_cmds:
        print(f"[deploy] Executing: {cmd} ({desc}) ...")
        code, out, err = run_ssh_cmd(ssh_client, cmd)
        if out:
            print(f"         Output: {out}")
        if code != 0:
            print(f"         Warning/Error (code {code}): {err}")
            # Cache removal might produce no output or minor warnings, but let's log
            if "rm -f" not in cmd:
                success = False

    print("[deploy] Services reloaded.")
    return success


def deploy(
    host: str = "10.10.18.2",
    port: int = 22,
    username: str = "root",
    password: str = "Xg@2020+",
    key_filename: str = None,
    mode: str = "sync",
    ipk_path: Path = None,
    skip_reload: bool = False,
    dry_run: bool = False,
    repo_root: Path = REPO_ROOT,
) -> bool:
    """
    Connect to router and deploy luci-app-ipselect.
    """
    if not HAS_PARAMIKO:
        raise RuntimeError("paramiko is required for remote deployment. Please install via: pip install paramiko")

    print(f"[deploy] Connecting to {username}@{host}:{port} ...")
    if dry_run:
        print(f"[deploy] [dry-run] Mode: {mode}, skipping network connection.")
        if mode == "sync":
            deploy_sync_mode(None, None, repo_root, dry_run=True)
        else:
            deploy_ipk_mode(None, None, repo_root, ipk_path, dry_run=True)
        if not skip_reload:
            reload_router_services(None, dry_run=True)
        return True

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            key_filename=key_filename,
            timeout=15,
            look_for_keys=False if password else True,
            allow_agent=False if password else True,
        )
        print(f"[deploy] Connected successfully to {host}:{port}")

        sftp = client.open_sftp()
        try:
            if mode == "sync":
                ok = deploy_sync_mode(client, sftp, repo_root, dry_run=False)
            else:
                ok = deploy_ipk_mode(client, sftp, repo_root, ipk_path, dry_run=False)

            if not ok:
                print("[deploy] ERROR: Deployment failed.")
                return False

            if not skip_reload:
                reload_router_services(client, dry_run=False)

            print("\n[deploy] SUCCESS: luci-app-ipselect deployed and active!")
            return True
        finally:
            sftp.close()
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(
        description="One-key deployment and LuCI reload tool for luci-app-ipselect"
    )
    parser.add_argument("--host", default="10.10.18.2", help="Target router IP/host (default: 10.10.18.2)")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("-u", "--user", default="root", help="SSH username (default: root)")
    parser.add_argument("-p", "--password", default="Xg@2020+", help="SSH password (default: Xg@2020+)")
    parser.add_argument("--key-file", default=None, help="SSH private key path")
    parser.add_argument(
        "-m",
        "--mode",
        choices=["sync", "ipk"],
        default="sync",
        help="Deployment mode: sync (Mode A: hot push, default) or ipk (Mode B: opkg install)",
    )
    parser.add_argument("--ipk", type=Path, default=None, help="Path to custom .ipk archive (for ipk mode)")
    parser.add_argument("--skip-reload", action="store_true", help="Skip LuCI cache purge and service restart")
    parser.add_argument("--dry-run", action="store_true", help="Simulate deployment steps without remote changes")

    args = parser.parse_args()

    success = deploy(
        host=args.host,
        port=args.port,
        username=args.user,
        password=args.password,
        key_filename=args.key_file,
        mode=args.mode,
        ipk_path=args.ipk,
        skip_reload=args.skip_reload,
        dry_run=args.dry_run,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
