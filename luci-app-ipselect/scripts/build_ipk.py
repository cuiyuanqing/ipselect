#!/usr/bin/env python3
"""
scripts/build_ipk.py
Standalone pure-Python IPK packager for luci-app-ipselect.
Builds an OpenWrt opkg-compatible .ipk package without requiring
the OpenWrt SDK or cross-compilation toolchain.
"""

import argparse
import io
import os
import stat
import sys
import tarfile
import time
from pathlib import Path

PKG_NAME = "luci-app-ipselect"
PKG_VERSION = "1.1.0"
PKG_RELEASE = "1"
PKG_FULL_VERSION = f"{PKG_VERSION}-{PKG_RELEASE}"
PKG_ARCH = "all"
PKG_MAINTAINER = "JeffSmith"
PKG_DESCRIPTION = "LuCI web interface for ipselect node selector and speedtest tool."
PKG_DEPENDS = "python3, python3-requests, git, git-http"

# Mapping: (source_rel_path, target_rel_path, posix_mode)
PAYLOAD_MAP = [
    ("root/etc/config/ipselect", "etc/config/ipselect", 0o644),
    ("root/etc/init.d/ipselect", "etc/init.d/ipselect", 0o755),
    ("root/usr/bin/ipselect-runner", "usr/bin/ipselect-runner", 0o755),
    ("root/usr/share/luci/menu.d/luci-app-ipselect.json", "usr/share/luci/menu.d/luci-app-ipselect.json", 0o644),
    ("root/usr/share/rpcd/acl.d/luci-app-ipselect.json", "usr/share/rpcd/acl.d/luci-app-ipselect.json", 0o644),
    ("luasrc/controller/ipselect.lua", "usr/lib/lua/luci/controller/ipselect.lua", 0o644),
    ("htdocs/luci-static/resources/view/ipselect/overview.js", "www/luci-static/resources/view/ipselect/overview.js", 0o644),
]

CONTROL_TEMPLATE = """Package: {pkg_name}
Version: {pkg_version}
Depends: {depends}
Section: luci
Architecture: {arch}
Maintainer: {maintainer}
Description: {description}
"""

POSTINST_SCRIPT = """#!/bin/sh
[ -n "${IPKG_INSTROOT}" ] || {
	chmod 755 /usr/bin/ipselect-runner
	chmod 755 /etc/init.d/ipselect
	/etc/init.d/ipselect enable
	/etc/init.d/ipselect restart
	[ -s /lib/functions.sh ] && {
		. /lib/functions.sh
		default_postinst $0 $@
	}
	rm -rf /tmp/luci-indexcache* /tmp/luci-modulecache* 2>/dev/null || true
	/etc/init.d/rpcd reload 2>/dev/null || true
	/etc/init.d/uhttpd restart 2>/dev/null || true
	exit 0
}
chmod 755 ${IPKG_INSTROOT}/usr/bin/ipselect-runner 2>/dev/null || true
chmod 755 ${IPKG_INSTROOT}/etc/init.d/ipselect 2>/dev/null || true
exit 0
"""

POSTRM_SCRIPT = """#!/bin/sh
[ -n "${IPKG_INSTROOT}" ] || {
	rm -rf /tmp/luci-indexcache* /tmp/luci-modulecache* 2>/dev/null || true
	/etc/init.d/rpcd reload 2>/dev/null || true
	/etc/init.d/uhttpd restart 2>/dev/null || true
	exit 0
}
exit 0
"""


def _normalize_text_bytes(data: bytes) -> bytes:
    """Normalize CRLF to LF and ensure trailing LF."""
    data = data.replace(b"\r\n", b"\n")
    if data and not data.endswith(b"\n"):
        data += b"\n"
    return data


def create_tar_gz_bytes(entries: list, base_time: int = None) -> bytes:
    """
    Create a tar.gz byte stream from entries.
    entries: list of dict with keys:
      - name: str (e.g. './etc/config/ipselect')
      - data: bytes or None (if isdir)
      - mode: int (e.g. 0o644 or 0o755)
      - is_dir: bool
    """
    if base_time is None:
        base_time = int(time.time())

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in entries:
            ti = tarfile.TarInfo(name=entry["name"])
            ti.mtime = base_time
            ti.uid = 0
            ti.gid = 0
            ti.uname = "root"
            ti.gname = "root"
            ti.mode = entry["mode"]

            if entry.get("is_dir", False):
                ti.type = tarfile.DIRTYPE
                ti.size = 0
                tar.addfile(ti)
            else:
                ti.type = tarfile.REGTYPE
                data = entry["data"]
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))

    return buf.getvalue()


def build_control_tar_gz(
    pkg_name: str = PKG_NAME,
    pkg_version: str = PKG_FULL_VERSION,
    depends: str = PKG_DEPENDS,
    arch: str = PKG_ARCH,
    maintainer: str = PKG_MAINTAINER,
    description: str = PKG_DESCRIPTION,
    base_time: int = None,
) -> bytes:
    """Generate control.tar.gz bytes containing control and postinst."""
    control_content = CONTROL_TEMPLATE.format(
        pkg_name=pkg_name,
        pkg_version=pkg_version,
        depends=depends,
        arch=arch,
        maintainer=maintainer,
        description=description,
    )
    control_bytes = _normalize_text_bytes(control_content.encode("utf-8"))
    postinst_bytes = _normalize_text_bytes(POSTINST_SCRIPT.encode("utf-8"))
    postrm_bytes = _normalize_text_bytes(POSTRM_SCRIPT.encode("utf-8"))

    entries = [
        {"name": "./", "data": None, "mode": 0o755, "is_dir": True},
        {"name": "./control", "data": control_bytes, "mode": 0o644, "is_dir": False},
        {"name": "./postinst", "data": postinst_bytes, "mode": 0o755, "is_dir": False},
        {"name": "./postrm", "data": postrm_bytes, "mode": 0o755, "is_dir": False},
    ]
    return create_tar_gz_bytes(entries, base_time=base_time)


def build_data_tar_gz(source_root: Path, base_time: int = None) -> bytes:
    """Generate data.tar.gz bytes containing all payload files."""
    entries = [{"name": "./", "data": None, "mode": 0o755, "is_dir": True}]
    dirs_added = {"."}

    for src_rel, target_rel, mode in PAYLOAD_MAP:
        src_path = source_root / src_rel
        if not src_path.exists():
            raise FileNotFoundError(f"Missing required payload source: {src_path}")

        # Ensure all intermediate parent directories are added
        parts = Path(target_rel).parts
        accum_dir = "."
        for part in parts[:-1]:
            accum_dir = f"{accum_dir}/{part}"
            if accum_dir not in dirs_added:
                entries.append(
                    {"name": f"./{accum_dir[2:]}", "data": None, "mode": 0o755, "is_dir": True}
                )
                dirs_added.add(accum_dir)

        # Read and normalize file content
        raw_data = src_path.read_bytes()
        norm_data = _normalize_text_bytes(raw_data)

        target_arcname = f"./{target_rel}"
        entries.append(
            {"name": target_arcname, "data": norm_data, "mode": mode, "is_dir": False}
        )

    return create_tar_gz_bytes(entries, base_time=base_time)


def write_ar_archive(output_path: Path, members: list, base_time: int = None):
    """Write members to Unix ar format."""
    if base_time is None:
        base_time = int(time.time())

    with open(output_path, "wb") as f:
        f.write(b"!<arch>\n")
        for name, data in members:
            # Unix ar header (60 bytes total)
            # name (16B), mtime (12B), uid (6B), gid (6B), mode (8B), size (10B), fmag (2B: `\n)
            header_name = (name + "/").ljust(16).encode("ascii")
            header_mtime = str(base_time).ljust(12).encode("ascii")
            header_uid = b"0".ljust(6)
            header_gid = b"0".ljust(6)
            header_mode = b"100644".ljust(8)
            header_size = str(len(data)).ljust(10).encode("ascii")
            header_fmag = b"`\n"
            header = (
                header_name
                + header_mtime
                + header_uid
                + header_gid
                + header_mode
                + header_size
                + header_fmag
            )
            assert len(header) == 60, f"Invalid ar header length: {len(header)}"
            f.write(header)
            f.write(data)
            if len(data) % 2 != 0:
                f.write(b"\n")


def write_tar_gz_archive(output_path: Path, members: list, base_time: int = None):
    """Write members to standard opkg tar.gz format."""
    entries = []
    for name, data in members:
        arcname = name if name.startswith("./") else f"./{name}"
        entries.append({"name": arcname, "data": data, "mode": 0o644, "is_dir": False})
    tar_bytes = create_tar_gz_bytes(entries, base_time=base_time)
    output_path.write_bytes(tar_bytes)


def build_ipk(
    source_root: Path,
    output_path: Path,
    pkg_name: str = PKG_NAME,
    pkg_version: str = PKG_FULL_VERSION,
    pkg_arch: str = PKG_ARCH,
    maintainer: str = PKG_MAINTAINER,
    description: str = PKG_DESCRIPTION,
    depends: str = PKG_DEPENDS,
    container_format: str = "tar.gz",
) -> Path:
    """
    Build a complete .ipk file.
    container_format: 'tar.gz' (standard opkg) or 'ar' (Debian standard).
    """
    source_root = Path(source_root).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    base_time = int(time.time())
    debian_binary = b"2.0\n"

    control_tar_gz = build_control_tar_gz(
        pkg_name=pkg_name,
        pkg_version=pkg_version,
        depends=depends,
        arch=pkg_arch,
        maintainer=maintainer,
        description=description,
        base_time=base_time,
    )

    data_tar_gz = build_data_tar_gz(source_root=source_root, base_time=base_time)

    members = [
        ("debian-binary", debian_binary),
        ("control.tar.gz", control_tar_gz),
        ("data.tar.gz", data_tar_gz),
    ]

    if container_format == "ar":
        write_ar_archive(output_path, members, base_time=base_time)
    else:
        write_tar_gz_archive(output_path, members, base_time=base_time)

    return output_path


def read_ipk(ipk_path: Path) -> dict:
    """
    Read an .ipk file (supporting both tar.gz and ar containers).
    Returns dict: {'debian-binary': bytes, 'control.tar.gz': bytes, 'data.tar.gz': bytes}
    """
    ipk_path = Path(ipk_path)
    data = ipk_path.read_bytes()
    results = {}

    if data.startswith(b"!<arch>\n"):
        offset = 8
        while offset < len(data):
            header = data[offset : offset + 60]
            if len(header) < 60:
                break
            name = header[0:16].decode("ascii", errors="ignore").strip().rstrip("/")
            size = int(header[48:58].decode("ascii", errors="ignore").strip())
            offset += 60
            file_data = data[offset : offset + size]
            results[name] = file_data
            offset += size
            if size % 2 != 0 and offset < len(data) and data[offset : offset + 1] == b"\n":
                offset += 1
    elif data.startswith(b"\x1f\x8b"):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                name = member.name.lstrip("./")
                f = tar.extractfile(member)
                if f is not None:
                    results[name] = f.read()
    else:
        raise ValueError(f"Unrecognized ipk format for {ipk_path}")

    return results


OPENWRT25_INSTALLER_SCRIPT = """#!/bin/sh
# ==============================================================================
# luci-app-ipselect 一键独立自安装脚本 (适用于 OpenWrt 25 / iStoreOS 25 / apk 环境)
# ==============================================================================
set -e

echo "============================================================"
echo "  🚀 正在为 OpenWrt 25 / iStoreOS 25 安装 luci-app-ipselect"
echo "============================================================"

# 1. 解压内嵌 Payload 到系统根目录
echo "[1/4] 释放插件核心文件到系统目录 (/etc, /usr, /www) ..."
ARCHIVE_LINE=$(awk '/^__PAYLOAD_ARCHIVE_BELOW__/ {print NR + 1; exit 0; }' "$0")
if [ -z "$ARCHIVE_LINE" ]; then
    echo "❌ 错误: 未能在安装脚本中定位 Payload 归档数据"
    exit 1
fi

tail -n +"$ARCHIVE_LINE" "$0" | tar -xz -C /

# 2. 设置 POSIX 执行权限
echo "[2/4] 设置组件执行权限 (chmod 755) ..."
chmod 755 /usr/bin/ipselect-runner 2>/dev/null || true
chmod 755 /etc/init.d/ipselect 2>/dev/null || true

# 3. 注册开机自启并启动系统服务
echo "[3/4] 注册系统服务开机自启并启动 /etc/init.d/ipselect ..."
/etc/init.d/ipselect enable 2>/dev/null || true
/etc/init.d/ipselect restart 2>/dev/null || true

# 4. 清除 LuCI 缓存并重启 Web 服务器
echo "[4/4] 刷新 LuCI 菜单缓存并重载 rpcd 与 uhttpd ..."
rm -rf /tmp/luci-indexcache* /tmp/luci-modulecache* /var/luci-indexcache* 2>/dev/null || true
/etc/init.d/rpcd restart 2>/dev/null || true
/etc/init.d/uhttpd restart 2>/dev/null || true

echo "============================================================"
echo "  🎉 安装成功！"
echo "  请刷新浏览器后台，在【服务】(Services) 菜单中打开【节点优选】。"
echo "============================================================"
exit 0
__PAYLOAD_ARCHIVE_BELOW__
"""

OPENWRT25_UNINSTALL_SCRIPT = """#!/bin/sh
# ==============================================================================
# luci-app-ipselect 卸载清理脚本 (适用于 OpenWrt 25 / iStoreOS 25)
# ==============================================================================
set -e

echo "正在卸载 luci-app-ipselect ..."
/etc/init.d/ipselect stop 2>/dev/null || true
/etc/init.d/ipselect disable 2>/dev/null || true

rm -f /etc/config/ipselect
rm -f /etc/init.d/ipselect
rm -f /usr/bin/ipselect-runner
rm -f /usr/share/luci/menu.d/luci-app-ipselect.json
rm -f /usr/share/rpcd/acl.d/luci-app-ipselect.json
rm -f /usr/lib/lua/luci/controller/ipselect.lua
rm -f /www/luci-static/resources/view/ipselect/overview.js
rm -rf /www/luci-static/resources/view/ipselect

rm -rf /tmp/luci-indexcache* /tmp/luci-modulecache* /var/luci-indexcache* 2>/dev/null || true
/etc/init.d/rpcd restart 2>/dev/null || true
/etc/init.d/uhttpd restart 2>/dev/null || true

echo "卸载完成！"
exit 0
"""


def build_openwrt25_packages(
    source_root: Path,
    output_dir: Path,
    pkg_version: str = PKG_FULL_VERSION,
    base_time: int = None,
) -> dict:
    """
    Build standalone installer and tar.gz bundle for OpenWrt 25 / apk environment.
    """
    source_root = Path(source_root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if base_time is None:
        base_time = int(time.time())

    data_tar_gz = build_data_tar_gz(source_root=source_root, base_time=base_time)

    # 1. Single-file self-extracting runner (OpenWrt 25 免 SDK 一键自安装脚本)
    installer_sh_path = output_dir / "luci-app-ipselect-openwrt25-installer.sh"
    header_bytes = OPENWRT25_INSTALLER_SCRIPT.replace("\r\n", "\n").encode("utf-8")
    installer_sh_path.write_bytes(header_bytes + data_tar_gz)

    # 2. Complete tar.gz bundle with install.sh and uninstall.sh
    tar_gz_bundle_path = output_dir / f"luci-app-ipselect_{pkg_version}_openwrt25.tar.gz"
    install_script_bytes = (
        """#!/bin/sh\nset -e\nDIR="$(cd "$(dirname "$0")" && pwd)"\n"""
        """tar -xzf "$DIR/data.tar.gz" -C /\n"""
        """chmod 755 /usr/bin/ipselect-runner /etc/init.d/ipselect\n"""
        """/etc/init.d/ipselect enable\n/etc/init.d/ipselect restart\n"""
        """rm -rf /tmp/luci-indexcache* /tmp/luci-modulecache* /var/luci-indexcache* 2>/dev/null || true\n"""
        """/etc/init.d/rpcd restart 2>/dev/null || true\n"""
        """/etc/init.d/uhttpd restart 2>/dev/null || true\necho "安装完成！"\n"""
    ).encode("utf-8")
    uninstall_script_bytes = OPENWRT25_UNINSTALL_SCRIPT.replace("\r\n", "\n").encode("utf-8")

    entries = [
        {"name": "./install.sh", "data": install_script_bytes, "mode": 0o755},
        {"name": "./uninstall.sh", "data": uninstall_script_bytes, "mode": 0o755},
        {"name": "./data.tar.gz", "data": data_tar_gz, "mode": 0o644},
    ]
    bundle_bytes = create_tar_gz_bytes(entries, base_time=base_time)
    tar_gz_bundle_path.write_bytes(bundle_bytes)

    # 3. Readme for OpenWrt 25
    readme_path = output_dir / "README.md"
    readme_content = f"""# luci-app-ipselect 安装指南 (OpenWrt 25 / iStoreOS 25 / apk 环境)

OpenWrt 25.12+ 起包管理器全面由 `opkg` 升级为 `apk` (Alpine apk-tools 3.x)。
由于新版系统已不再预置 `opkg` 二进制，本目录提供适用于 OpenWrt 25 的两套免编译一键安装方案：

### 推荐方案：单文件自解压自安装脚本
1. 上传 `luci-app-ipselect-openwrt25-installer.sh` 到路由器的 `/tmp/` 目录；
2. 在终端执行：
   ```bash
   sh /tmp/luci-app-ipselect-openwrt25-installer.sh
   ```
3. 脚本会自动将 7 大核心文件释放到系统目录、修正 0755/0644 权限、配置开机自启、刷新 LuCI 菜单缓存并重载服务。

### 备用方案：tar.gz 压缩包
1. 上传 `luci-app-ipselect_{pkg_version}_openwrt25.tar.gz` 到路由器的 `/tmp/` 目录；
2. 在终端解压并执行安装：
   ```bash
   tar -xzf /tmp/luci-app-ipselect_{pkg_version}_openwrt25.tar.gz -C /tmp/
   sh /tmp/install.sh
   ```
"""
    readme_path.write_text(readme_content.replace("\r\n", "\n"), encoding="utf-8")

    return {
        "installer_sh": installer_sh_path,
        "tar_gz": tar_gz_bundle_path,
        "readme": readme_path,
    }


def build_all_releases(source_root: Path, dist_dir: Path, pkg_version: str = PKG_FULL_VERSION) -> dict:
    """
    Build dual releases for both OpenWrt 24 (opkg IPK) and OpenWrt 25 (apk installer).
    """
    source_root = Path(source_root)
    dist_dir = Path(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)

    dir_24 = dist_dir / "openwrt-24"
    dir_25 = dist_dir / "openwrt-25"
    dir_24.mkdir(parents=True, exist_ok=True)
    dir_25.mkdir(parents=True, exist_ok=True)

    # 1. OpenWrt 24 (opkg .ipk)
    ipk_name = f"{PKG_NAME}_{pkg_version}_{PKG_ARCH}.ipk"
    root_ipk = dist_dir / ipk_name
    ow24_ipk = dir_24 / ipk_name

    build_ipk(source_root=source_root, output_path=root_ipk, pkg_version=pkg_version)
    ow24_ipk.write_bytes(root_ipk.read_bytes())

    # Readme for OpenWrt 24
    readme_24 = dir_24 / "README.md"
    readme_24_content = f"""# luci-app-ipselect 安装指南 (OpenWrt 24 / iStoreOS 22/24 / opkg 环境)

适用于 OpenWrt 24.10、23.05、22.03 及更早版本的标准 `opkg` IPK 安装包。

### 命令行安装
上传 `{ipk_name}` 到路由器的 `/tmp/` 目录，执行：
```bash
opkg update
opkg install /tmp/{ipk_name}
```

### Web 界面一键安装
在 LuCI 管理后台进入【系统】->【软件包】（或 iStore 商店），点击“上传软件包”，选择此 IPK 文件即可一键安装。
"""
    readme_24.write_text(readme_24_content.replace("\r\n", "\n"), encoding="utf-8")

    # 2. OpenWrt 25
    ow25_results = build_openwrt25_packages(source_root=source_root, output_dir=dir_25, pkg_version=pkg_version)

    # 3. Global dist index
    global_readme = dist_dir / "README.md"
    global_readme_content = f"""# luci-app-ipselect 安装包发布索引

本目录同时提供针对 **OpenWrt 24 (opkg)** 与 **OpenWrt 25 (apk)** 的安装包方案：

| 目标系统 | 包管理器 / 机制 | 产物目录与文件 | 安装指引 |
| :--- | :--- | :--- | :--- |
| **OpenWrt 24** (及 23/22/21) | `opkg` (标准 IPK) | `openwrt-24/{ipk_name}` | `opkg install {ipk_name}` 或 LuCI 网页直接上传 |
| **OpenWrt 25** (25.12+ / iStoreOS 25) | `apk` (免 SDK 一键包) | `openwrt-25/luci-app-ipselect-openwrt25-installer.sh` | 终端执行 `sh installer.sh` 一键就绪 |
"""
    global_readme.write_text(global_readme_content.replace("\r\n", "\n"), encoding="utf-8")

    return {
        "openwrt24_ipk": ow24_ipk,
        "openwrt25_installer": ow25_results["installer_sh"],
        "openwrt25_tar_gz": ow25_results["tar_gz"],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Pure Python IPK Builder for luci-app-ipselect (supporting OpenWrt 24 & 25)"
    )
    repo_root = Path(__file__).resolve().parent.parent
    default_output = repo_root / "dist" / f"{PKG_NAME}_{PKG_FULL_VERSION}_{PKG_ARCH}.ipk"

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=f"Specific output IPK path (default: build dual releases for both 24 and 25)",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=repo_root,
        help=f"Project root directory (default: {repo_root})",
    )
    parser.add_argument(
        "--format",
        choices=["tar.gz", "ar"],
        default="tar.gz",
        help="Container archive format: tar.gz (default, standard opkg) or ar",
    )
    parser.add_argument(
        "--version",
        default=PKG_FULL_VERSION,
        help=f"Package full version (default: {PKG_FULL_VERSION})",
    )
    parser.add_argument(
        "--maintainer",
        default=PKG_MAINTAINER,
        help=f"Package maintainer (default: {PKG_MAINTAINER})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=True,
        help="Build dual releases for both OpenWrt 24 (opkg) and OpenWrt 25 (apk)",
    )

    args = parser.parse_args()

    if args.output:
        print(f"[build_ipk] Packaging single IPK {PKG_NAME} ({args.version}) -> {args.output} ...")
        out_file = build_ipk(
            source_root=args.source_root,
            output_path=args.output,
            pkg_version=args.version,
            maintainer=args.maintainer,
            container_format=args.format,
        )
        size = out_file.stat().st_size
        print(f"[build_ipk] Successfully built: {out_file} ({size:,} bytes)")
    else:
        dist_dir = args.source_root / "dist"
        print(f"[build_ipk] Building dual packages for OpenWrt 24 (opkg) & OpenWrt 25 (apk) ...")
        res = build_all_releases(source_root=args.source_root, dist_dir=dist_dir, pkg_version=args.version)
        print(f"[build_ipk] [OK] OpenWrt 24 (opkg): {res['openwrt24_ipk']} ({res['openwrt24_ipk'].stat().st_size:,} bytes)")
        print(f"[build_ipk] [OK] OpenWrt 25 (installer): {res['openwrt25_installer']} ({res['openwrt25_installer'].stat().st_size:,} bytes)")
        print(f"[build_ipk] [OK] OpenWrt 25 (tar.gz): {res['openwrt25_tar_gz']} ({res['openwrt25_tar_gz'].stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()

