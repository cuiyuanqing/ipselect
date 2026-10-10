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
PKG_VERSION = "1.0.0"
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
	exit 0
}
chmod 755 ${IPKG_INSTROOT}/usr/bin/ipselect-runner 2>/dev/null || true
chmod 755 ${IPKG_INSTROOT}/etc/init.d/ipselect 2>/dev/null || true
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

    entries = [
        {"name": "./", "data": None, "mode": 0o755, "is_dir": True},
        {"name": "./control", "data": control_bytes, "mode": 0o644, "is_dir": False},
        {"name": "./postinst", "data": postinst_bytes, "mode": 0o755, "is_dir": False},
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


def main():
    parser = argparse.ArgumentParser(
        description="Pure Python IPK Builder for luci-app-ipselect"
    )
    repo_root = Path(__file__).resolve().parent.parent
    default_output = repo_root / "dist" / f"{PKG_NAME}_{PKG_FULL_VERSION}_{PKG_ARCH}.ipk"

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_output,
        help=f"Output IPK path (default: {default_output})",
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

    args = parser.parse_args()

    print(f"[build_ipk] Packaging {PKG_NAME} ({args.version}) ...")
    print(f"[build_ipk] Source root: {args.source_root}")
    print(f"[build_ipk] Output file: {args.output}")
    print(f"[build_ipk] Container format: {args.format}")

    out_file = build_ipk(
        source_root=args.source_root,
        output_path=args.output,
        pkg_version=args.version,
        maintainer=args.maintainer,
        container_format=args.format,
    )

    size = out_file.stat().st_size
    print(f"[build_ipk] Successfully built: {out_file} ({size:,} bytes)")


if __name__ == "__main__":
    main()
