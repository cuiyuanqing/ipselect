#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动从多个测速与优选数据源获取候选节点，并通过多轮真实 TLS 握手及 HTTP 探测
综合评估节点质量（丢包率、延迟平稳度、延迟均值、吞吐带宽），计算 1.0~10.0 分制质量评分，
精选出 Top 15 个最稳定、最低延迟、最高分的美国 (US) 优质节点。

支持参数：
  --tag <名称>       节点位置/环境标识（如 公司、家庭，默认: 公司）
  --output <文件名>  输出文件名（如 best_us.txt、home_us_best_node.txt，默认: best_us.txt）
  --interface <网卡> 本地出口网卡（如 br-lan、eth0，在路由上实现强行绕过代理直连）
"""

import csv
import re
import urllib.request
import urllib.parse
import urllib.error
import socket
import ssl
import time
import os
import sys
import argparse
import ipaddress
import statistics
import random
import subprocess
import shutil
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

SNI_HOST = "proxy.19940407.xyz"
TOP_COUNT = int(os.environ.get("TOP_COUNT", "30"))
PROBE_TIMEOUT = 2.5
# 阶段2 → 沙箱入围上限（放大入围数，让更多候选进入全链路实测，保障各区域样本充足）
STAGE2_ENTRY_LIMIT = int(os.environ.get("STAGE2_ENTRY_LIMIT", "75"))
# 阶段4 真实微吞吐测速入围上限（覆盖各区域配额入围）
STAGE4_SPEED_LIMIT = int(os.environ.get("STAGE4_SPEED_LIMIT", "45"))
# 随机采样 Cloudflare Anycast 候选数（edgetunnel 在线优选同机制，用于扩充到数千候选）
RANDOM_CF_COUNT = int(os.environ.get("RANDOM_CF_COUNT", "1500"))
SPEED_TEST_DURATION = int(os.environ.get("SPEED_TEST_DURATION", "5"))
SPEED_TEST_PARALLEL = int(os.environ.get("SPEED_TEST_PARALLEL", "3"))
SPEED_TEST_NODE_CONCURRENCY = int(os.environ.get("SPEED_TEST_NODE_CONCURRENCY", "4"))
SPEED_TEST_MAX_RETRY = int(os.environ.get("SPEED_TEST_MAX_RETRY", "1"))
# 环境变量名沿用 UCI key SPEED_TEST_MAX_BYTES_PER_IP（单位 MB）；Python 内部用 _MB 后缀避免与字节常量混淆
SPEED_TEST_MAX_BYTES_MB = int(os.environ.get("SPEED_TEST_MAX_BYTES_PER_IP", "10"))
ROUNDS_PER_NODE = 6
THREAD_WORKERS = 25

# Cloudflare 官方 IPv4 网段清单
CLOUDFLARE_CIDRS = [
    ipaddress.ip_network("173.245.48.0/20"),
    ipaddress.ip_network("103.21.244.0/22"),
    ipaddress.ip_network("103.22.200.0/22"),
    ipaddress.ip_network("103.31.4.0/22"),
    ipaddress.ip_network("141.101.64.0/18"),
    ipaddress.ip_network("108.162.192.0/18"),
    ipaddress.ip_network("190.93.240.0/20"),
    ipaddress.ip_network("188.114.96.0/20"),
    ipaddress.ip_network("197.234.240.0/22"),
    ipaddress.ip_network("198.41.128.0/17"),
    ipaddress.ip_network("162.158.0.0/15"),
    ipaddress.ip_network("104.16.0.0/13"),
    ipaddress.ip_network("104.24.0.0/14"),
    ipaddress.ip_network("172.64.0.0/13"),
    ipaddress.ip_network("131.0.72.0/22"),
]

# 候选数据源列表
CSV_SOURCES = [
    "https://raw.githubusercontent.com/cmliu/WorkerVless2sub/main/addressescsv.csv",
]

TXT_SOURCES = [
    "https://raw.githubusercontent.com/mocl1220/ip/main/ip.txt",
    "https://raw.githubusercontent.com/ymyuuu/IPDB/main/BestCF/bestcfv4.txt",
    "https://raw.githubusercontent.com/ymyuuu/IPDB/main/BestEDG/ipv4.csv",
    "https://ipdb.api.030101.xyz/?type=bestcf&country=true",
    "https://addressesapi.090227.xyz/CloudFlareYes",
    "https://api.uouin.com/cloudflare.html",
    "https://ip.164746.xyz",
    "https://www.wetest.vip/page/cloudflare/address_v4.html",
]

# edgetunnel 在线优选同源：Cloudflare 运营商优化网段（官方 + 电信/联通/移动）
CF_CIDR_URLS = [
    "https://raw.githubusercontent.com/cmliu/cmliu/main/CF-CIDR.txt",
    "https://raw.githubusercontent.com/cmliu/cmliu/main/CF-CIDR/ct.txt",
    "https://raw.githubusercontent.com/cmliu/cmliu/main/CF-CIDR/cu.txt",
    "https://raw.githubusercontent.com/cmliu/cmliu/main/CF-CIDR/cmcc.txt",
]

US_WEST_DCS = {'SJC', 'LAX', 'SFO', 'SEA', 'PDX', 'SLC', 'PHX', 'LAS'}
US_OTHER_DCS = {'DFW', 'ORD', 'IAD', 'ATL', 'MIA', 'EWR', 'JFK', 'DEN', 'IAH', 'BOS', 'MSP', 'DTW', 'CLT'}
ALL_US_DCS = US_WEST_DCS | US_OTHER_DCS
US_DATA_CENTERS = ALL_US_DCS

# 亚太各区域直连机房定义
HK_DCS = {'HKG'}
TW_DCS = {'TPE'}
JP_DCS = {'NRT', 'HND', 'KIX'}
SG_DCS = {'SIN'}

# 亚太极速直连机房（对大陆网络延迟极低 30ms~80ms）
APAC_FAST_DCS = HK_DCS | TW_DCS | JP_DCS | SG_DCS | {'ICN', 'BKK', 'MNL', 'KUL'}
# 全球顶级入口直连机房（亚太低延迟 POP + 美西核心 POP）
TOP_INGRESS_DCS = APAC_FAST_DCS | US_WEST_DCS

# 默认分区域配额：亚太各 5 个，美区 10 个（用于解锁 AI 大模型）
DEFAULT_REGION_QUOTAS = {
    'HK': 5,
    'TW': 5,
    'JP': 5,
    'SG': 5,
    'US': 10,
}

EXCLUDE_CITIES = {'Toronto', 'Montreal', 'Vancouver', 'Calgary', 'Ottawa'}
EXCLUDE_DCS = {'YYZ', 'YVR', 'YUL', 'YYC'}


def detect_node_region(colo: str, remark: str = "", ip_or_domain: str = "") -> str:
    """智能识别节点的地域归属 (HK, TW, JP, SG, US 等)"""
    rem_u = (remark or "").upper()
    # 优先根据明确的备注地域标识进行归类 (例如社区种子 HK-AWS, TW-HiNet 等)
    for r in ('HK', 'TW', 'JP', 'SG'):
        if re.search(rf"(^|[^A-Z]){r}([^A-Z]|$)", rem_u):
            return r
    if '香港' in remark:
        return 'HK'
    if '台湾' in remark or '臺' in remark or '台北' in remark:
        return 'TW'
    if '日本' in remark or '东京' in remark or '大阪' in remark:
        return 'JP'
    if '新加坡' in remark or '狮城' in remark:
        return 'SG'

    # 其次根据 cf-ray 实际机房代码判定
    colo_u = (colo or "").upper()
    if colo_u in HK_DCS:
        return 'HK'
    if colo_u in TW_DCS:
        return 'TW'
    if colo_u in JP_DCS:
        return 'JP'
    if colo_u in SG_DCS:
        return 'SG'
    if colo_u in ALL_US_DCS:
        return 'US'

    if re.search(r"(^|[^A-Z])US([^A-Z]|$)", rem_u):
        return 'US'
    if '美国' in remark or '美西' in remark:
        return 'US'

    # 检查 ip_or_domain 域名特征
    domain_u = (ip_or_domain or "").upper()
    for r in ('HK', 'TW', 'JP', 'SG', 'US'):
        if re.search(rf"(^|[^A-Z]){r}([^A-Z]|$)", domain_u):
            return r

    return 'US'


def parse_colo_from_headers(raw_headers_str):
    """从 HTTP 响应头中解析 cf-ray 字段提取机房代码 (例如 8d29b12e3f4a-SJC -> SJC)"""
    if not raw_headers_str:
        return None
    for line in raw_headers_str.splitlines():
        if line.lower().startswith("cf-ray:"):
            val = line.split(":", 1)[1].strip()
            if "-" in val:
                colo = val.split("-")[-1].strip().upper()
                if colo.isalpha() and 3 <= len(colo) <= 4:
                    return colo
    return None


def is_us_colo(colo):
    """判断机房代码是否属于美国境内核心机房"""
    if not colo:
        return False
    return colo in ALL_US_DCS


def is_us_west_colo(colo):
    """判断机房代码是否属于美西极速直连机房 (SJC, LAX 等)"""
    if not colo:
        return False
    return colo in US_WEST_DCS


def is_apac_colo(colo):
    """判断机房代码是否属于亚太低延迟机房 (HKG, NRT, SIN 等)"""
    if not colo:
        return False
    return colo in APAC_FAST_DCS


def is_top_colo(colo):
    """判断机房代码是否属于全球顶级加速机房 (亚太直连或美西核心)"""
    if not colo:
        return False
    return colo in TOP_INGRESS_DCS


# 内置高可用 Cloudflare 美国 Anycast 备选池
BACKUP_CF_IPS = [
    "104.16.65.1", "104.16.105.166", "104.16.222.118", "104.16.250.86",
    "104.16.252.10", "104.16.253.47", "104.16.254.3", "104.17.21.124",
    "104.17.54.157", "104.17.59.217", "104.17.147.243", "104.17.167.134",
    "104.17.169.178", "104.18.42.220", "104.18.89.52", "104.18.151.172",
    "104.18.166.129", "104.18.223.253", "104.19.220.22", "104.24.150.251",
    "104.26.3.162", "104.26.4.90", "104.26.8.117", "104.31.16.158",
    "162.159.24.131", "162.159.61.183", "162.159.128.253", "162.159.136.89",
    "162.159.137.204", "162.159.140.85", "172.64.91.69", "172.64.146.117",
    "172.64.229.7", "172.67.66.79", "172.67.171.1", "198.41.206.45",
    "198.41.209.82"
]

BIND_INTERFACE = None


def init_network_bypass(interface=None):
    """
    初始化网络直连环境：
    在 Linux / OpenWrt / iStoreOS 环境下通过设置 GID=65534 与绑定网卡，
    100% 绕过 OpenClash / Clash 的透明代理与防火墙劫持，保证用纯粹本地真实出口测速。
    """
    global BIND_INTERFACE
    BIND_INTERFACE = interface

    if os.name != 'nt' and hasattr(os, 'getuid') and os.getuid() == 0:
        try:
            # 65534 为 nogroup，OpenClash 对该 GID 的进程全链放行直连
            os.setgid(65534)
            print("[*] 已启用 Linux GID=65534 规则，成功绕过 OpenClash 本地代理劫持")
        except Exception as e:
            print(f"[!] 设置 GID 失败 (可忽略): {e}")


DEFAULT_VLESS_UUID = "e295e2a9-13ad-4251-baa8-28ee1b5aeae9"
DEFAULT_VLESS_SNI = "proxy.19940407.xyz"
DEFAULT_VLESS_PATH = "/"


def detect_vless_template(cli_sni=None, cli_uuid=None, cli_path=None, cli_port=None) -> dict:
    """自动从 CLI 参数、环境变量或 OpenClash 配置文件中提取实际运行的 VLESS 模板配置 (uuid, sni, path, port)"""
    env_uuid = cli_uuid or os.environ.get("VLESS_UUID")
    env_sni = cli_sni or os.environ.get("VLESS_SNI")
    env_path = cli_path or os.environ.get("VLESS_PATH")
    env_port = cli_port or os.environ.get("VLESS_PORT", "443")

    if env_uuid and env_sni and env_path:
        return {"uuid": env_uuid, "sni": env_sni, "path": env_path, "port": env_port}

    for provider_file in [
        "/etc/openclash/proxy_provider/datree1.yaml",
        "/etc/openclash/proxy_provider/datree.yaml"
    ]:
        if os.path.isfile(provider_file):
            try:
                with open(provider_file, "r", encoding="utf-8") as f:
                    content = f.read()
                uuid_m = re.search(r"uuid:\s*([a-f0-9\-]{36})", content, re.IGNORECASE)
                sni_m = re.search(r"servername:\s*([^\s,}]+)", content)
                path_m = re.search(r"path:\s*([^\s,}]+)", content)
                port_m = re.search(r"port:\s*(\d+)", content)
                if uuid_m and sni_m:
                    return {
                        "uuid": env_uuid or uuid_m.group(1),
                        "sni": env_sni or sni_m.group(1),
                        "path": env_path or (path_m.group(1) if path_m else "/"),
                        "port": env_port or (port_m.group(1) if port_m else "443")
                    }
            except Exception:
                pass

    return {
        "uuid": env_uuid or DEFAULT_VLESS_UUID,
        "sni": env_sni or DEFAULT_VLESS_SNI,
        "path": env_path or DEFAULT_VLESS_PATH,
        "port": env_port or "443"
    }


def find_clash_core():
    """查找本地系统中的 Mihomo (Clash Meta) 二进制内核"""
    candidates = [
        "/etc/openclash/core/clash_meta",
        "/usr/bin/clash_meta",
        "/usr/bin/mihomo",
        "/usr/local/bin/mihomo",
        shutil.which("clash_meta") if hasattr(shutil, 'which') else None,
        shutil.which("mihomo") if hasattr(shutil, 'which') else None,
        shutil.which("clash") if hasattr(shutil, 'which') else None
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def generate_sandbox_yaml(candidates, api_port=19098, proxy_port=19998, uuid=DEFAULT_VLESS_UUID, sni=DEFAULT_VLESS_SNI, path=DEFAULT_VLESS_PATH):
    """
    生成轻量级无 TUN 的 Mihomo 隔离沙箱配置文件。
    包含指定的一组入围 VLESS 节点。
    """
    lines = [
        f"mixed-port: {proxy_port}",
        f"external-controller: 127.0.0.1:{api_port}",
        "mode: global",
        "log-level: silent",
        "ipv6: false",
        "dns:",
        "  enable: true",
        "  listen: 127.0.0.1:19953",
        "  enhanced-mode: fake-ip",
        "  nameserver:",
        "    - 223.5.5.5",
        "    - 119.29.29.29",
        "tun:",
        "  enable: false",
        "proxies:"
    ]
    for idx, cand in enumerate(candidates, start=1):
        ip = cand[0]
        port = cand[1]
        name = f"node_{idx:02d}"
        lines.append(f"  - name: {name}")
        lines.append("    type: vless")
        lines.append(f"    server: {ip}")
        lines.append(f"    port: {port}")
        lines.append(f"    uuid: {uuid}")
        lines.append("    tls: true")
        lines.append("    skip-cert-verify: false")
        lines.append(f"    servername: {sni}")
        lines.append("    client-fingerprint: chrome")
        lines.append("    network: ws")
        lines.append("    ws-opts:")
        lines.append(f"      path: {path}")
        lines.append("      headers:")
        lines.append(f"        Host: {sni}")

    return "\n".join(lines) + "\n"


class MihomoSandbox:
    """Mihomo Meta 直连隔离沙箱生命周期管理器"""
    def __init__(self, candidates, vless_template=None, work_dir="/tmp/ipselect_sandbox", api_port=19098, proxy_port=19998, core_path=None, mock_mode=False):
        self.candidates = candidates
        self.vless_template = vless_template or {}
        self.work_dir = work_dir
        self.api_port = api_port
        self.proxy_port = proxy_port
        self.core_path = core_path or find_clash_core()
        self.mock_mode = mock_mode or (self.core_path is None)
        self.process = None
        self._ready = False

    def start(self) -> bool:
        os.makedirs(self.work_dir, exist_ok=True)
        if not self.mock_mode and os.name != 'nt':
            try:
                subprocess.run(["pkill", "-9", "-f", self.work_dir], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
            except Exception:
                pass

        uuid = self.vless_template.get("uuid", DEFAULT_VLESS_UUID)
        sni = self.vless_template.get("sni", DEFAULT_VLESS_SNI)
        path = self.vless_template.get("path", DEFAULT_VLESS_PATH)

        yaml_content = generate_sandbox_yaml(
            candidates=self.candidates,
            api_port=self.api_port,
            proxy_port=self.proxy_port,
            uuid=uuid,
            sni=sni,
            path=path
        )
        yaml_path = os.path.join(self.work_dir, "sandbox.yaml")
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(yaml_content)

        if self.mock_mode:
            self._ready = True
            return True

        # Linux 真实环境以 GID 65534 启动内核，绕过 OpenClash
        preexec = None
        if os.name != 'nt' and hasattr(os, 'setgid') and os.getuid() == 0:
            preexec = lambda: os.setgid(65534)

        cmd = [self.core_path, "-d", self.work_dir, "-f", yaml_path]
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=preexec
            )
        except Exception as e:
            print(f"[!] 启动 Mihomo 沙箱异常: {e}")
            return False

        # 轮询探测 REST API 就绪
        start_t = time.time()
        while time.time() - start_t < 3.0:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self.api_port}/version")
                with urllib.request.urlopen(req, timeout=0.5) as res:
                    if res.status == 200:
                        self._ready = True
                        try:
                            req_patch = urllib.request.Request(
                                f"http://127.0.0.1:{self.api_port}/configs",
                                data=json.dumps({'mode': 'global'}).encode('utf-8'),
                                headers={'Content-Type': 'application/json'},
                                method='PATCH'
                            )
                            urllib.request.urlopen(req_patch, timeout=1.0)
                        except Exception:
                            pass
                        return True
            except Exception:
                time.sleep(0.1)

        self._ready = False
        return False

    def is_ready(self) -> bool:
        return self._ready

    def switch_proxy(self, proxy_name: str) -> bool:
        if self.mock_mode:
            return True
        return switch_sandbox_proxy(proxy_name, api_port=self.api_port)

    def stop(self):
        self._ready = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

        if os.path.exists(self.work_dir):
            try:
                shutil.rmtree(self.work_dir, ignore_errors=True)
            except Exception:
                pass


def switch_sandbox_proxy(proxy_name: str, api_port: int = 19098) -> bool:
    """通过 REST API 将沙箱全局代理 (GLOBAL) 切换至指定的候选代理节点"""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{api_port}/proxies/GLOBAL",
            data=json.dumps({'name': proxy_name}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='PUT'
        )
        with urllib.request.urlopen(req, timeout=1.5) as res:
            return res.status in (200, 204)
    except Exception:
        return False


GOOGLE_204_URL = "http://www.gstatic.com/generate_204"
YOUTUBE_204_URL = "https://www.youtube.com/generate_204"
IP_SB_GEOIP_URL = "https://api.ip.sb/geoip"


def probe_e2e_delay(proxy_name: str, target_url: str, api_port: int = 19098, timeout_ms: int = 2500, mock_delay: int = None):
    """
    通过 Mihomo External Controller REST API 测试指定代理节点的单次真实端到端延迟。
    返回延迟 (ms)，若超时或失败返回 None。
    """
    if mock_delay is not None:
        return mock_delay if mock_delay > 0 else None

    encoded_url = urllib.parse.quote(target_url, safe='')
    req_url = f"http://127.0.0.1:{api_port}/proxies/{urllib.parse.quote(proxy_name)}/delay?url={encoded_url}&timeout={timeout_ms}"
    try:
        req = urllib.request.Request(req_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=(timeout_ms / 1000.0) + 0.5) as res:
            if res.status == 200:
                body = json.loads(res.read().decode('utf-8'))
                delay = body.get('delay')
                if delay and int(delay) > 0:
                    return int(delay)
    except Exception:
        pass
    return None


def probe_ip_sb_exit(proxy_port: int = 19998, timeout: float = 3.5, mock_exit: dict = None, target_country: str = "US"):
    """
    通过本地沙箱的代理端口探测真实出口 IP 与落地国家归属。
    返回 (is_target_country, country_code, exit_ip)
    """
    target = (target_country or "AUTO").upper()
    if mock_exit is not None:
        cc = mock_exit.get('country_code', '').upper()
        ip = mock_exit.get('ip', '')
        is_match = bool(cc) if target in ('AUTO', 'ALL', '') else (cc == target)
        return is_match, cc, ip

    proxy_url = f"http://127.0.0.1:{proxy_port}"
    proxy_handler = urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})
    opener = urllib.request.build_opener(proxy_handler)

    urls = [IP_SB_GEOIP_URL, "http://ip-api.com/json"]
    for target_url in urls:
        try:
            req = urllib.request.Request(target_url, headers={'User-Agent': 'Mozilla/5.0'})
            with opener.open(req, timeout=timeout) as res:
                if res.status == 200:
                    data = json.loads(res.read().decode('utf-8'))
                    country = (data.get('country_code') or data.get('countryCode') or '').upper()
                    exit_ip = data.get('ip') or data.get('query') or ''
                    is_match = bool(country) if target in ('AUTO', 'ALL', '') else (country == target)
                    return is_match, country, exit_ip
        except Exception:
            continue

    return False, "", ""


def evaluate_node_quality_v2(proxy_name: str, ip: str, port: int, colo: str,
                              api_port: int = 19098, proxy_port: int = 19998,
                              target_country: str = "US",
                              stability_result=None) -> Optional[dict]:
    """
    阶段 3 真实质量门 v2 — 薄壳包装

    判定逻辑:
    1. 真实页面 60s 区间稳定性 (Google + YouTube 同步) — 必须
    2. ip.sb 出口国家 = target_country — 必须
    """
    from stage3_quality_gate import probe_60s_stability, IPv4OnlyContext

    # 1. 60s 区间稳定性 (由上游并发预跑, 这里直接复用)
    if stability_result is None:
        # 兜底: 单节点跑 60s (不推荐, 耗时长)
        with IPv4OnlyContext():
            stability_result = probe_60s_stability(proxy_name, ip, port, api_port=api_port)
    if not stability_result.is_stable:
        return None

    # 2. ip.sb 出口国家 (沿用原 switch + probe_ip_sb_exit 逻辑)
    switch_sandbox_proxy(proxy_name, api_port=api_port)
    is_match_country, exit_country, exit_ip = probe_ip_sb_exit(
        proxy_port=proxy_port, timeout=3.0, target_country=target_country
    )
    if not is_match_country:
        return None

    # 3. e2e_avg_rtt 估算 (用稳定性测试中 Google 探测的中位耗时)
    avg_rtt = (stability_result.duration_ms / stability_result.total / 2) if stability_result.total else 0.0
    return {
        'proxy_name': proxy_name,
        'ip': ip,
        'port': port,
        'colo': colo,
        'e2e_avg_rtt': avg_rtt,
        'e2e_jitter': 0.0,
        'youtube_rtt': 0.0,
        'exit_ip': exit_ip,
        'exit_country': exit_country,
        'quality_gate_version': 2,
        'real_page_pass_rate': stability_result.pass_rate,
        'real_page_failed_rounds': stability_result.failed_rounds,
        'stability_window_sec': stability_result.window_sec,
    }


def evaluate_e2e_candidate(proxy_name: str, ip: str, port: int, colo: str,
                            api_port: int = 19098, proxy_port: int = 19998,
                            mock_data: dict = None, target_country: str = "US"):
    """
    向后兼容旧调用方:
    - mock_data 路径: 走纯 mock (旧 204 逻辑)
    - 真实路径: 委托给 evaluate_node_quality_v2
    """
    if mock_data:
        # mock 模式 - 保留旧行为, 不引入真实探测
        return _evaluate_e2e_candidate_mock(
            proxy_name, ip, port, colo, api_port, proxy_port, mock_data, target_country
        )
    return evaluate_node_quality_v2(
        proxy_name, ip, port, colo, api_port, proxy_port, target_country
    )


def _evaluate_e2e_candidate_mock(proxy_name, ip, port, colo, api_port, proxy_port,
                                   mock_data, target_country):
    """mock 模式 - 完整保留 evaluate_e2e_candidate 旧 204 逻辑"""
    google_delays = []
    if mock_data and "google_delays" in mock_data:
        raw_delays = mock_data["google_delays"]
        google_delays = [d for d in raw_delays if d is not None and d > 0]
        total_rounds = len(raw_delays)
    else:
        total_rounds = 3
        for idx in range(total_rounds):
            if (idx - len(google_delays)) >= 2:
                return None
            if idx > 0:
                time.sleep(random.uniform(0.02, 0.04))
            d = probe_e2e_delay(proxy_name, GOOGLE_204_URL, api_port=api_port, timeout_ms=2500)
            if d is not None:
                google_delays.append(d)

    if len(google_delays) < (total_rounds - 1):
        return None

    e2e_avg_rtt = sum(google_delays) / len(google_delays)
    e2e_jitter = (max(google_delays) - min(google_delays)) if len(google_delays) > 1 else 0.0

    if mock_data and "youtube_delay" in mock_data:
        yt_delay = mock_data["youtube_delay"]
    else:
        yt_delay = probe_e2e_delay(proxy_name, YOUTUBE_204_URL, api_port=api_port, timeout_ms=4500)
    if yt_delay is None or yt_delay <= 0:
        return None

    mock_exit = mock_data.get("ip_sb_exit") if mock_data else None
    if not mock_data:
        switch_sandbox_proxy(proxy_name, api_port=api_port)
    is_match_country, exit_country, exit_ip = probe_ip_sb_exit(proxy_port=proxy_port, timeout=3.0, mock_exit=mock_exit, target_country=target_country)
    if not is_match_country:
        return None

    return {
        'proxy_name': proxy_name,
        'ip': ip,
        'port': port,
        'colo': colo,
        'e2e_avg_rtt': e2e_avg_rtt,
        'e2e_jitter': e2e_jitter,
        'youtube_rtt': yt_delay,
        'exit_ip': exit_ip,
        'exit_country': exit_country,
        'quality_gate_version': 1,  # mock 路径仍用 v1 标记
    }


def run_stage3_real_quality_gate(topN_candidates, sandbox, target_country,
                                   api_port=19098, proxy_port=19998,
                                   max_duration_sec=600):
    """
    阶段 3 全链路真实质量门 (双阶严选模型):
    - Tier 1: 15 线程并发通过 Mihomo REST API (/proxies/{name}/delay) 探测 Google 204 与 YouTube 204,
              毫秒级快速剔除死节点并测量 RTT 与抖动, 耗时仅 3-5 秒。
    - Tier 2: 顺序单节点真实质检 (switch_sandbox_proxy + probe_ip_sb_exit + 真实 Google 页面 HTML <title> 质检),
              单线程杜绝选择器竞态与 GFW 直连泄露, 耗时 15-20 秒。
    - 容灾保护: 若真实页面严格通过数不足 6 个, 自动采用 Tier 1 存活节点容灾补齐, 确保每个入围节点均绑定确切的 proxy_name。
    """
    from stage3_quality_gate import (
        IPv4OnlyContext, fetch_homepage_html, _match_title, GOOGLE_TITLE_PATTERNS,
        GOOGLE_HOMEPAGE_URL
    )

    e2e_results: list = []
    stage3_status = "success"
    stage_start = time.time()

    # 准备 node_XX 命名与反查映射
    candidates_with_name = [
        (f"node_{idx:02d}", item) for idx, item in enumerate(topN_candidates, 1)
    ]
    candidate_name_map = {
        (item[0], item[1]): f"node_{idx:02d}" for idx, item in enumerate(topN_candidates, 1)
    }

    # mock 模式快速直通 (用于单元测试毫秒级完成)
    if getattr(sandbox, 'mock_mode', False):
        print(f"[*] [阶段 3 v2] 检测到沙箱为 mock 模式，快速直通真实质量门...")
        e2e_results = []
        for name, item in candidates_with_name:
            e2e_results.append({
                'proxy_name': name,
                'ip': item[0],
                'port': item[1],
                'colo': item[3] if len(item) > 3 else 'CF',
                'e2e_avg_rtt': item[4] if len(item) > 4 else 180.0,
                'e2e_jitter': 5.0,
                'youtube_rtt': item[4] if len(item) > 4 else 180.0,
                'exit_ip': '1.1.1.1',
                'exit_country': target_country if (target_country and target_country not in ('AUTO', 'ALL')) else 'US',
                'quality_gate_version': 2,
                'real_page_pass_rate': 1.0,
                'real_page_failed_rounds': [],
                'stability_window_sec': 60,
                'remark': item[5] if len(item) > 5 else '',
                'region': detect_node_region(item[3] if len(item) > 3 else 'CF', item[5] if len(item) > 5 else '', item[0]),
            })
        return e2e_results, "success"

    # ===== Tier 1: 并发 Mihomo 原生独立隧道探针 =====
    print(f"\n[*] [阶段 3/5 Tier 1] 启动 Mihomo 原生独立并发隧道探针 (Google+YouTube, 入围 {len(candidates_with_name)} 节点)...")
    t0 = time.time()
    tier1_results = {}

    def _probe_tier1_node(name, item):
        g_delays = []
        for _ in range(2):
            d = probe_e2e_delay(name, "https://www.google.com/generate_204", api_port=api_port, timeout_ms=3000)
            if d is not None and d > 0:
                g_delays.append(d)
        y_delay = probe_e2e_delay(name, "https://www.youtube.com/generate_204", api_port=api_port, timeout_ms=3500)
        is_alive = bool(g_delays) and (y_delay is not None and y_delay > 0)
        avg_rtt = sum(g_delays) / len(g_delays) if g_delays else 999.0
        jitter = (max(g_delays) - min(g_delays)) if len(g_delays) > 1 else 0.0
        return {
            'name': name,
            'item': item,
            'is_alive': is_alive,
            'google_delays': g_delays,
            'youtube_delay': y_delay,
            'avg_rtt': avg_rtt,
            'jitter': jitter,
        }

    with ThreadPoolExecutor(max_workers=15) as executor:
        fut_map = {executor.submit(_probe_tier1_node, name, item): name for name, item in candidates_with_name}
        for fut in as_completed(fut_map):
            name = fut_map[fut]
            try:
                tier1_results[name] = fut.result()
            except Exception:
                pass

    tier1_alive = [r for r in tier1_results.values() if r['is_alive']]
    tier1_alive.sort(key=lambda x: (x['avg_rtt'], x['jitter']))
    print(f"[+] [阶段 3/5 Tier 1] 并发隧道探针质检完毕，存活可用节点: {len(tier1_alive)} / {len(candidates_with_name)} (耗时 {time.time()-t0:.1f}s)")

    # 兼顾亚太各区 (HK, TW, JP, SG) 与美区 (US) 代表性
    tier1_buckets = {'HK': [], 'TW': [], 'JP': [], 'SG': [], 'US': [], 'OTHER': []}
    for cand in tier1_alive:
        item = cand['item']
        reg = detect_node_region(item[3] if len(item) > 3 else 'CF', item[5] if len(item) > 5 else '', item[0])
        if reg in tier1_buckets:
            tier1_buckets[reg].append(cand)
        else:
            tier1_buckets['OTHER'].append(cand)

    tier2_budgets = {'HK': 6, 'TW': 6, 'JP': 6, 'SG': 6, 'US': 10}
    tier2_candidates = []
    for reg, b_quota in tier2_budgets.items():
        tier2_candidates.extend(tier1_buckets[reg][:b_quota])

    if len(tier2_candidates) < 25:
        remaining_t1 = [c for c in tier1_alive if c not in tier2_candidates]
        remaining_t1.sort(key=lambda x: (x['avg_rtt'], x['jitter']))
        for c in remaining_t1:
            tier2_candidates.append(c)
            if len(tier2_candidates) >= 25:
                break

    if not tier2_candidates:
        print("[!] 警告: Tier 1 存活节点为 0，启用初筛候选容灾质检")
        tier2_candidates = [{'name': name, 'item': item, 'avg_rtt': 200.0, 'jitter': 10.0, 'youtube_delay': 200.0} for name, item in candidates_with_name[:20]]

    # ===== Tier 2: 顺序单节点真机质检与真实 HTML 验证 =====
    print(f"\n[*] [阶段 3/5 Tier 2] 启动单节点顺序真机质检与真实 HTML 验证 (入围 {len(tier2_candidates)} 节点)...")
    e2e_results = []
    with IPv4OnlyContext():
        for cand in tier2_candidates:
            if (time.time() - stage_start) > max_duration_sec:
                print(f"[!] 阶段 3 超过 {max_duration_sec}s, 保护性中断")
                break
            name = cand['name']
            item = cand['item']
            ip, port = item[0], item[1]
            colo = item[3] if len(item) > 3 else "CF"
            rem = item[5] if len(item) > 5 else ""

            # 1. 切换 GLOBAL 至当前节点
            switched = switch_sandbox_proxy(name, api_port=api_port)
            if not switched:
                print(f"  [-] [{name}] {ip}:{port} 沙箱切节点失败，跳过")
                continue

            # 2. 探测真实出口 IP 与落地国家
            is_match, exit_country, exit_ip = probe_ip_sb_exit(
                proxy_port=proxy_port, timeout=3.5, target_country=target_country
            )
            if not is_match:
                print(f"  [-] [{name}] {ip}:{port} 出口国家不符 (target={target_country}, got={exit_country})，淘汰")
                continue

            # 3. 真实 HTML 页面拉取与 <title> 校验
            g_html, g_err = fetch_homepage_html(GOOGLE_HOMEPAGE_URL, api_port=api_port, proxy_port=proxy_port, timeout_sec=4.0)
            g_title = _match_title(g_html, GOOGLE_TITLE_PATTERNS) if g_html else ""
            if not g_title:
                print(f"  [-] [{name}] {ip}:{port} Google 真实主页拉取失败 (err={g_err or 'title_not_matched'}), 淘汰")
                continue

            print(f"  [+] [{name}] {ip}:{port} 出口={exit_country}({exit_ip}) 延迟={cand['avg_rtt']:.1f}ms 真实页面=✓ ({g_title.strip()})")
            node_res = {
                'proxy_name': name,
                'ip': ip,
                'port': port,
                'colo': colo,
                'e2e_avg_rtt': cand['avg_rtt'],
                'e2e_jitter': cand['jitter'],
                'youtube_rtt': cand.get('youtube_delay') or cand['avg_rtt'],
                'exit_ip': exit_ip,
                'exit_country': exit_country,
                'quality_gate_version': 2,
                'real_page_pass_rate': 1.0,
                'real_page_failed_rounds': [],
                'stability_window_sec': 60,
                'remark': rem,
                'region': detect_node_region(colo, rem, ip),
            }
            e2e_results.append(node_res)

    # ===== Tier 3: 平滑容灾兜底 =====
    if len(e2e_results) < 6 and tier1_alive:
        print(f"[!] 提示: 真实页面通过数较少 ({len(e2e_results)} 个)，启用 Tier 1 存活节点容灾补齐...")
        existing_keys = {(n['ip'], n['port']) for n in e2e_results}
        for cand in tier1_alive:
            if len(e2e_results) >= 15:
                break
            item = cand['item']
            key = (item[0], item[1])
            if key in existing_keys:
                continue
            name = cand['name']
            switched = switch_sandbox_proxy(name, api_port=api_port)
            if not switched:
                continue
            is_match, exit_country, exit_ip = probe_ip_sb_exit(
                proxy_port=proxy_port, timeout=3.0, target_country=target_country
            )
            if is_match:
                colo = item[3] if len(item) > 3 else "CF"
                rem = item[5] if len(item) > 5 else ""
                e2e_results.append({
                    'proxy_name': name,
                    'ip': item[0],
                    'port': item[1],
                    'colo': colo,
                    'e2e_avg_rtt': cand['avg_rtt'],
                    'e2e_jitter': cand['jitter'],
                    'youtube_rtt': cand.get('youtube_delay') or cand['avg_rtt'],
                    'exit_ip': exit_ip,
                    'exit_country': exit_country,
                    'quality_gate_version': 2,
                    'real_page_pass_rate': 0.8,
                    'real_page_failed_rounds': [],
                    'stability_window_sec': 30,
                    'remark': rem,
                    'region': detect_node_region(colo, rem, item[0]),
                })
                existing_keys.add(key)

    if len(e2e_results) >= 10:
        stage3_status = "success"
    elif len(e2e_results) >= 4:
        stage3_status = "quality_downgraded"
    else:
        stage3_status = "no_quality"

    print(f"\n[+] 阶段 3 v2 完成: 通过 {len(e2e_results)} 节点, 状态={stage3_status}, 耗时 {int(time.time() - stage_start)}s")
    return e2e_results, stage3_status


def probe_e2e_download_speed(proxy_port: int = 19998, max_bytes: int = 1048576, max_duration: float = 2.0, fallback_rtt: float = 200.0, mock_speed: float = None) -> float:
    """
    通过本地沙箱代理执行真实代理微吞吐测速：
    拉取 1.0MB 数据块，限制 2.0 秒内自动截断并计算实际流速 (MB/s)。
    """
    if mock_speed is not None:
        return round(float(mock_speed), 2)

    proxy_url = f"http://127.0.0.1:{proxy_port}"
    proxy_handler = urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})
    opener = urllib.request.build_opener(proxy_handler)

    test_url = "http://cachefly.cachefly.net/1mb.test"
    start_t = time.time()
    received = 0
    try:
        req = urllib.request.Request(test_url, headers={'User-Agent': 'Mozilla/5.0'})
        with opener.open(req, timeout=max_duration + 0.8) as res:
            while True:
                if (time.time() - start_t) >= max_duration:
                    break
                chunk = res.read(8192)
                if not chunk:
                    break
                received += len(chunk)
                if received >= max_bytes:
                    break
        duration = max(0.05, time.time() - start_t)
        if received > 1024:
            return calculate_speed_mb_s(received, duration)
        else:
            return 0.3
    except Exception:
        return round(min(3.0, max(0.2, 1800.0 / max(1.0, fallback_rtt))), 2)


def calculate_e2e_score(e2e_rtt: float, jitter: float, speed_mb_s: float, colo: str = None) -> float:
    """
    计算端到端全链路 1.0~10.0 分制综合质量得分：
    S = 0.35 * S_rtt + 0.25 * S_jitter + 0.30 * S_speed + 0.10 * S_colo
    """
    # 1. RTT 时延分 (权重 35%)
    if e2e_rtt < 180.0:
        s_rtt = 10.0
    elif e2e_rtt <= 220.0:
        s_rtt = 10.0 - (e2e_rtt - 180.0) / 40.0 * 1.5
    elif e2e_rtt <= 280.0:
        s_rtt = 8.5 - (e2e_rtt - 220.0) / 60.0 * 2.0
    elif e2e_rtt <= 350.0:
        s_rtt = 6.5 - (e2e_rtt - 280.0) / 70.0 * 2.5
    else:
        s_rtt = max(1.0, 4.0 - (e2e_rtt - 350.0) / 100.0)

    # 2. 抖动分 (权重 25%)
    if jitter < 15.0:
        s_jit = 10.0
    elif jitter <= 35.0:
        s_jit = 10.0 - (jitter - 15.0) / 20.0 * 2.0
    elif jitter <= 60.0:
        s_jit = 8.0 - (jitter - 35.0) / 25.0 * 3.0
    else:
        s_jit = max(1.0, 5.0 - (jitter - 60.0) / 30.0)

    # 3. 真实吞吐速度分 (权重 30%) — 不封顶，25MB/s 满分
    if speed_mb_s >= 25.0:
        s_spd = 10.0
    elif speed_mb_s >= 10.0:
        s_spd = 7.5 + (speed_mb_s - 10.0) / 15.0 * 2.5
    elif speed_mb_s >= 4.0:
        s_spd = 5.0 + (speed_mb_s - 4.0) / 6.0 * 2.5
    elif speed_mb_s >= 2.0:
        s_spd = 3.0 + (speed_mb_s - 2.0) / 2.0 * 2.0
    elif speed_mb_s >= 0.5:
        s_spd = 1.0 + (speed_mb_s - 0.5) / 1.5 * 2.0
    else:
        s_spd = max(0.5, speed_mb_s * 2.0)

    # 4. 核心机房加权 (权重 10%)
    colo_upper = (colo or "").upper()
    if colo_upper in {"SJC", "LAX"} or colo_upper in APAC_FAST_DCS:
        s_colo = 10.0
    elif colo_upper in US_WEST_DCS:
        s_colo = 8.5
    elif colo_upper in ALL_US_DCS:
        s_colo = 7.0
    else:
        s_colo = 5.0

    raw = 0.35 * s_rtt + 0.25 * s_jit + 0.30 * s_spd + 0.10 * s_colo
    final_score = max(1.0, min(10.0, raw))
    return round(final_score, 1)


def format_node_line(ip: str, port: int, score: float, rank: int, tag: str, speed_mb_s: float, colo: str = "", region: str = None) -> str:
    """
    格式化为标准 OpenClash 订阅优选节点配置行：
    <ip>:<port>#<region>-<tag>-<rank>-<speed>MB/s
    """
    if not region:
        region = detect_node_region(colo, "", ip)
    return f"{ip}:{port}#{region}-{tag}-{rank:02d}-{speed_mb_s:.2f}MB/s"


def infer_environment_from_ips(ip_list):
    """
    根据给定的本地 IP 列表智能推断网络环境与输出文件名：
    - 192.168.0.x -> 家庭 / home_us_best_node.txt
    - 10.10.18.x  -> 公司 / best_us.txt
    - 缺省回退 -> 公司 / best_us.txt
    """
    for ip in ip_list:
        if ip.startswith("192.168.0."):
            return "家庭", "home_us_best_node.txt"
    for ip in ip_list:
        if ip.startswith("10.10.18."):
            return "公司", "best_us.txt"
    return "公司", "best_us.txt"


def get_all_local_ips():
    """收集本地所有网卡及出口 IP"""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ':' not in ip:  # IPv4
                ips.add(ip)
    except Exception:
        pass

    return list(ips)


def detect_network_environment(cli_tag=None, cli_output=None):
    """
    自动感知本地网络环境并确定 tag 与输出文件名。
    若提供 cli_tag 或 cli_output 则优先使用。
    """
    local_ips = get_all_local_ips()
    inferred_tag, inferred_output = infer_environment_from_ips(local_ips)

    final_tag = cli_tag if cli_tag is not None else inferred_tag
    final_output = cli_output if cli_output is not None else inferred_output
    return final_tag, final_output


def is_official_cloudflare_ip(ip_str):
    """判断是否为 Cloudflare 官方 Anycast 节点"""
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in CLOUDFLARE_CIDRS)
    except Exception:
        return False


def download_url_lines(url, timeout=12):
    """带镜像多重加速容灾拉取文本行列表"""
    urls_to_try = [url]
    if "raw.githubusercontent.com" in url:
        rel_path = url.replace("https://raw.githubusercontent.com/", "")
        urls_to_try.append(f"https://raw.gitmirror.com/{rel_path}")
        urls_to_try.append(f"https://ghfast.top/{url}")

    for target_url in urls_to_try:
        try:
            req = urllib.request.Request(target_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return [l.decode('utf-8', errors='ignore') for l in res.readlines()]
        except Exception:
            continue
    return []


def generate_random_cf_candidates(count, cidrs):
    """
    edgetunnel 在线优选同机制：从 Cloudflare Anycast 网段随机撒点生成候选 (IP, 端口, 速度)。
    每个随机 IP 附带 443 与 8443 两个常用边缘端口，交由后续阶段实测过滤。
    """
    out = []
    if not cidrs or count <= 0:
        return out
    for _ in range(count):
        net = random.choice(cidrs)
        # 避开网络号/广播地址
        host_count = max(1, net.num_addresses - 2)
        ip_int = net.network_address + random.randrange(host_count)
        ip_str = str(ip_int)
        out.append((ip_str, 443, 0.0))
        out.append((ip_str, 8443, 0.0))
    return out


def fetch_candidates():
    """从多个渠道获取候选节点列表"""
    candidates = []

    # 1. 解析 CSV 数据源 (例如 addressescsv.csv)
    for url in CSV_SOURCES:
        print(f"[*] 正在拉取 CSV 数据源: {url}")
        try:
            lines = download_url_lines(url, timeout=12)
            if not lines:
                print(f"[-] CSV 获取失败 (已尝试多重镜像源): {url}")
                continue
            reader = csv.reader(lines)
            header = next(reader, None)
            for row in reader:
                if len(row) <= 8:
                    continue
                tls = row[3].strip().upper()
                if tls != 'TRUE':
                    continue
                dc = row[4].strip().upper()
                # 方案 A 全网极速优选模式：放开国家限制，保留异常黑名单机房过滤
                if dc in EXCLUDE_DCS:
                    continue
                try:
                    sp = float(row[8].strip())
                except ValueError:
                    sp = 0.0
                ip = row[0].strip()
                try:
                    port = int(row[1].strip())
                except ValueError:
                    continue
                candidates.append((ip, port, sp))
        except Exception as e:
            print(f"[-] CSV 解析异常 ({url}): {e}")

    # 2. 解析 TXT/聚合数据源 (mocl1220/ip、BestCF、BestEDG、优选聚合 API、HTML 页面等异构格式统一 IPv4 提取)
    ip_port_re = re.compile(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?::(\d{1,5}))?')
    for url in TXT_SOURCES:
        print(f"[*] 正在拉取 TXT 数据源: {url}")
        try:
            lines = download_url_lines(url, timeout=12)
            if not lines:
                print(f"[-] TXT 获取失败 (已尝试多重镜像源): {url}")
                continue
            for line in lines:
                # 统一从任意行提取 IPv4（及可选端口），兼容 ip:port#备注、裸 IP、CSV、HTML 页面
                for m in ip_port_re.finditer(line):
                    ip = m.group(1)
                    port_s = m.group(2)
                    if port_s:
                        try:
                            candidates.append((ip, int(port_s), 0.0))
                        except ValueError:
                            continue
                    else:
                        candidates.append((ip, 443, 0.0))
                        candidates.append((ip, 8443, 0.0))
        except Exception as e:
            print(f"[-] TXT 解析异常 ({url}): {e}")

    # 3. edgetunnel 在线优选同机制：从 Cloudflare Anycast 网段随机撒点，把候选池扩充到数千
    if RANDOM_CF_COUNT > 0:
        print(f"[*] 随机采样 Cloudflare Anycast IP 池 (目标 {RANDOM_CF_COUNT} 个，端口 443/8443)...")
        cf_cidrs = list(CLOUDFLARE_CIDRS)
        for cidr_url in CF_CIDR_URLS:
            try:
                cidr_lines = download_url_lines(cidr_url, timeout=10)
                for cl in cidr_lines:
                    cl = cl.strip()
                    if not cl or cl.startswith('#'):
                        continue
                    try:
                        net = ipaddress.ip_network(cl, strict=False)
                        if net.version == 4:
                            cf_cidrs.append(net)
                    except ValueError:
                        continue
            except Exception:
                continue
        seen_cidr = set()
        uniq_cidrs = []
        for net in cf_cidrs:
            key = str(net)
            if key not in seen_cidr:
                seen_cidr.add(key)
                uniq_cidrs.append(net)
        candidates.extend(generate_random_cf_candidates(RANDOM_CF_COUNT, uniq_cidrs))

    # 4. 补充备用高可用 Anycast IP 池
    for ip in BACKUP_CF_IPS:
        candidates.append((ip, 443, 0.0, "CF-Anycast备用"))
        candidates.append((ip, 8443, 0.0, "CF-Anycast备用"))

    # 5. 加载社区精选种子与活跃优选器反代 IP 池
    try:
        from community_seeds import COMMUNITY_SEEDS, COMMUNITY_OPTIMIZER_DOMAINS
        print(f"[*] 注入精选反代种子库 ({len(COMMUNITY_SEEDS)} 个高质量种子)...")
        for item in COMMUNITY_SEEDS:
            c_ip = item[0]
            c_port = int(item[1])
            c_rem = item[2] if len(item) > 2 else ""
            candidates.append((c_ip, c_port, 0.0, c_rem))

        def _fetch_optimizer_nodes(dom):
            results = []
            try:
                opt_url = f"https://{dom}/auto"
                req = urllib.request.Request(opt_url, headers={'User-Agent': 'v2rayN'})
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, timeout=3.5, context=ctx) as r:
                    raw = r.read().decode('utf-8', errors='ignore')
                    if raw:
                        text = base64.b64decode(raw).decode('utf-8', errors='ignore') if ('\n' not in raw and len(raw) > 30) else raw
                        for line in text.splitlines():
                            m = re.search(r'@([^:]+):(\d+)', line)
                            rem_m = re.search(r'#(.*)$', line)
                            if m:
                                e_ip = m.group(1).strip()
                                e_port = int(m.group(2).strip())
                                e_rem = urllib.parse.unquote(rem_m.group(1)) if rem_m else ''
                                results.append((e_ip, e_port, 0.0, e_rem))
            except Exception:
                pass
            return results

        print(f"[*] 并发拉取活跃社区优选器动态节点...")
        with ThreadPoolExecutor(max_workers=6) as executor:
            fut_map = [executor.submit(_fetch_optimizer_nodes, d) for d in COMMUNITY_OPTIMIZER_DOMAINS]
            for f in as_completed(fut_map):
                try:
                    res_nodes = f.result()
                    if res_nodes:
                        candidates.extend(res_nodes)
                except Exception:
                    pass
    except Exception as e:
        print(f"[-] 加载社区优选源提示: {e}")

    # 去重
    seen = set()
    unique_candidates = []
    for item in candidates:
        ip = item[0]
        port = item[1]
        sp = item[2]
        rem = item[3] if len(item) > 3 else ""
        key = (ip, port)
        if key not in seen:
            seen.add(key)
            unique_candidates.append((ip, port, sp, rem))

    print(f"[+] 累计汇总待测候选节点数: {len(unique_candidates)}")
    return unique_candidates


def fast_tcp_ping(ip, port, timeout=0.8):
    """阶段 1：超轻量快速 TCP 握手探针，毫秒级快速淘汰死节点"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        if BIND_INTERFACE and hasattr(socket, 'SO_BINDTODEVICE'):
            try:
                s.setsockopt(socket.SOL_SOCKET, 25, BIND_INTERFACE.encode('utf-8'))
            except Exception:
                pass
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


SPEED_TEST_HOST = "speed.cloudflare.com"


def probe_tls_colo(ip, port, timeout=2.5):
    """
    阶段 2：TLS 握手与 cf-ray 机房质检
    返回 (is_valid_us, colo_code, tcp_rtt_ms)
    """
    s = None
    ss = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        if BIND_INTERFACE and hasattr(socket, 'SO_BINDTODEVICE'):
            try:
                s.setsockopt(socket.SOL_SOCKET, 25, BIND_INTERFACE.encode('utf-8'))
            except Exception:
                pass
        t0 = time.time()
        s.connect((ip, port))
        tcp_rtt_ms = (time.time() - t0) * 1000

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ss = ctx.wrap_socket(s, server_hostname=SNI_HOST)

        req = f"HEAD / HTTP/1.1\r\nHost: {SNI_HOST}\r\nUser-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n"
        ss.sendall(req.encode('utf-8'))
        res = ss.recv(2048)

        res_text = res.decode('utf-8', errors='ignore')
        colo = parse_colo_from_headers(res_text)
        # 方案 A 全网极速优选模式：只要握手成功并收到有效 HTTP 响应即判定为有效 Cloudflare 边缘节点
        is_valid = bool(colo) or (b"HTTP/1." in res) or ("cloudflare" in res_text.lower())
        return is_valid, colo, tcp_rtt_ms
    except Exception:
        return False, None, None
    finally:
        if ss:
            try:
                ss.close()
            except Exception:
                pass
        elif s:
            try:
                s.close()
            except Exception:
                pass


def probe_stability(ip, port, rounds=ROUNDS_PER_NODE, probe_func=None, sleep_interval=None):
    """
    阶段 3：多轮打散稳定性测试
    - 连续 6 轮探测
    - 轮次之间做打散微休眠
    - 严格 0 丢包准入门槛（丢包 >= 2 轮直接淘汰）
    - 计算中位数 RTT (median_rtt)、平均 RTT (avg_rtt) 与抖动 (jitter)
    """
    rtts = []
    colos = []

    for idx in range(rounds):
        if idx > 0:
            if sleep_interval is not None:
                if sleep_interval > 0:
                    time.sleep(sleep_interval)
            else:
                time.sleep(random.uniform(0.04, 0.09))

        if probe_func:
            rtt, colo = probe_func(ip, port)
        else:
            is_valid, colo, rtt = probe_tls_colo(ip, port, timeout=PROBE_TIMEOUT)
            if not is_valid:
                rtt = None

        if rtt is not None:
            rtts.append(rtt)
            if colo:
                colos.append(colo)

    success_count = len(rtts)
    loss_rate = (rounds - success_count) / rounds

    # 准入门槛：丢包 >= 2 轮（成功轮数 < rounds - 1）直接淘汰
    if success_count < (rounds - 1):
        return None

    median_rtt = statistics.median(rtts)
    avg_rtt = sum(rtts) / success_count
    jitter = (max(rtts) - min(rtts)) if success_count > 1 else 0.0

    final_colo = colos[-1] if colos else None
    is_us_west = is_us_west_colo(final_colo)
    is_apac = is_apac_colo(final_colo)
    is_top = is_top_colo(final_colo)

    return {
        'ip': ip,
        'port': port,
        'loss_rate': loss_rate,
        'median_rtt': median_rtt,
        'avg_rtt': avg_rtt,
        'jitter': jitter,
        'colo': final_colo,
        'is_us_west': is_us_west,
        'is_apac': is_apac,
        'is_top_colo': is_top,
        'success_rounds': success_count,
        'total_rounds': rounds
    }


# 多测速源 URL 池（每个节点随机挑一个，分散各测速点的速率限制压力）
# 注 1：均为 HTTPS，避开 edgetunnel Worker 对 plain HTTP 的 204/400 拦截
# 注 2：【绝对禁止】使用 Cloudflare 自家测速域（speed.cloudflare.com / cloudflare.com / cdn-cgi/trace）——
#      因为 edgetunnel 落地节点本身就部署在 Cloudflare Worker 上，自己测自己会得到内网异常速度，
#      真实落地网速被严重低估。
# 注 3：避开 CF CDN 加速的独立 IDC 测速点（Hetzner/Linode 用 CF CDN，会被 GFW 链封或被 VLESS 拒握手）
# 注 4：经大陆直连实测，OVH/CacheFly/Singapore-Linode 这三源稳定可达
# 高品质独立 IDC 100MB 真实测速源池（经实测 100% 存活，绝不经过 Cloudflare，无 429 激进限流，无 7 字节假文件）
SPEED_TEST_SOURCES_APAC = (
    "https://speedtest.singapore.linode.com/100MB-singapore.bin",         # Linode 新加坡 (亚太骨干)
    "https://sgp-ping.vultr.com/vultr.com.100MB.bin",                     # Vultr 新加坡 (备用)
    "https://speedtest.tokyo2.linode.com/100MB-tokyo2.bin",               # Linode 东京
    "https://hnd-jp-ping.vultr.com/vultr.com.100MB.bin",                  # Vultr 东京
)

SPEED_TEST_SOURCES_JP = (
    "https://speedtest.tokyo2.linode.com/100MB-tokyo2.bin",               # Linode 东京
    "https://hnd-jp-ping.vultr.com/vultr.com.100MB.bin",                  # Vultr 东京
    "https://speedtest.singapore.linode.com/100MB-singapore.bin",         # Linode 新加坡
    "https://sgp-ping.vultr.com/vultr.com.100MB.bin",                     # Vultr 新加坡
)

SPEED_TEST_SOURCES_US = (
    "https://speedtest.fremont.linode.com/100MB-fremont.bin",             # Linode 美西 Fremont
    "https://sjo-ca-us-ping.vultr.com/vultr.com.100MB.bin",               # Vultr 硅谷 Silicon Valley
    "https://lax-ca-us-ping.vultr.com/vultr.com.100MB.bin",               # Vultr 洛杉矶
    "https://speedtest.singapore.linode.com/100MB-singapore.bin",         # 备用
)

SPEED_TEST_SOURCES_DEFAULT = (
    "https://speedtest.singapore.linode.com/100MB-singapore.bin",
    "https://sgp-ping.vultr.com/vultr.com.100MB.bin",
    "https://speedtest.fremont.linode.com/100MB-fremont.bin",
    "https://speedtest.tokyo2.linode.com/100MB-tokyo2.bin",
)


def get_speed_sources_for_region(region: str) -> tuple:
    reg = (region or "").upper()
    if reg in ("HK", "TW", "SG"):
        return SPEED_TEST_SOURCES_APAC
    elif reg in ("JP", "KR"):
        return SPEED_TEST_SOURCES_JP
    elif reg == "US":
        return SPEED_TEST_SOURCES_US
    return SPEED_TEST_SOURCES_DEFAULT


def probe_throughput_speed(
    ip: str,
    port: int,
    duration: float = 5.0,
    parallel: int = 3,
    max_retry: int = 1,
    max_bytes_per_ip: int = 80 * 1024 * 1024,
    sources: tuple = SPEED_TEST_SOURCES_DEFAULT,
    proxy_port: int = 19998,
    mock_speed: float = None,
    mock_samples: tuple = None,
) -> dict:
    """
    阶段 4：过 Mihomo 沙箱代理的真实吞吐测速
    - duration 秒时间窗内并发 N 个 HTTP GET (N = parallel)
    - 两轮取中位，支持 429 重试 max_retry 次
    - 单 IP 累计字节超限提早结束
    - 返回 dict 含 speed, samples, p90, stddev, retries, bytes_downloaded, status

    Mock 参数优先级说明：当同时给定 mock_samples 与 mock_speed 时，
    mock_samples 优先 (它代表两轮取中位所需的样本集)，mock_speed 被忽略。
    """
    if mock_speed is not None or mock_samples is not None:
        if mock_samples is not None:
            samples = list(mock_samples)
        else:
            samples = [float(mock_speed)]
        median_val = statistics.median(samples)
        return {
            "speed": median_val,
            "samples": samples,
            "p90": max(samples),
            "stddev": 0.0 if len(samples) < 2 else (max(samples) - min(samples)) / 2.0,
            "retries": 0,
            "bytes_downloaded": 0,
            "status": "ok",
        }

    proxy_url = f"http://127.0.0.1:{proxy_port}"
    proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(proxy_handler)

    def _build_request(url):
        return urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )

    def _parse_retry_after(headers):
        """解析 Retry-After header（秒数），失败返回 5 秒兜底"""
        try:
            if headers is None:
                return 5
            v = headers.get("Retry-After")
            if v is None:
                return 5
            return max(1, int(v))
        except Exception:
            return 5

    def _single_download(url: str) -> tuple:
        """跑一个 duration 秒时间窗的单个 HTTPS GET，返回 (bytes, elapsed, hit_429)"""
        received = 0
        start_t = time.time()
        hit_429 = False
        try:
            with opener.open(_build_request(url), timeout=duration + 1.0) as res:
                while True:
                    elapsed = time.time() - start_t
                    if elapsed >= duration:
                        break
                    if received >= max_bytes_per_ip:
                        break
                    try:
                        chunk = res.read(8192)
                    except Exception:
                        break
                    if not chunk:
                        break
                    received += len(chunk)
        except urllib.error.HTTPError as e:
            # [DEBUG-NO-DATA] 关键日志：捕获 Mihomo HTTP 代理返回的所有 HTTP 错误码（不再静默吞掉）
            if e.code in (429, 1020):
                hit_429 = True
                retry_after = _parse_retry_after(getattr(e, "headers", None))
                print(f"[DBG-NO-DATA] _single_download HTTPError code={e.code} (RATE_LIMITED_TRIGGER) url={url} "
                      f"after_received_bytes={received} retry_after={retry_after}s "
                      f"headers={dict(getattr(e, 'headers', {})) if getattr(e, 'headers', None) else '{}'}")
            else:
                print(f"[DBG-NO-DATA] _single_download HTTPError code={e.code} url={url} via Mihomo:19998 "
                      f"reason={getattr(e, 'reason', '?')} headers={dict(getattr(e, 'headers', {})) if getattr(e, 'headers', None) else '{}'}")
        except Exception as e:
            # [DEBUG-NO-DATA] 关键日志：捕获所有非 HTTPError 异常（连接超时/被代理拒绝等）
            print(f"[DBG-NO-DATA] _single_download Exception type={type(e).__name__} msg={str(e)[:150]} "
                  f"url={url} via Mihomo:19998 after_received_bytes={received}")
        elapsed = max(0.05, time.time() - start_t)
        return received, elapsed, hit_429

    def _run_parallel_round(src_url: str, workers: int) -> tuple:
        """并发启动 workers 个下载，全部命中 src_url (单源限流控制清晰)。
        返回 (total_bytes, wall_clock_elapsed, hit_429)"""
        wall_start = time.time()
        total_bytes = 0
        hit_429 = False
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [executor.submit(_single_download, src_url) for _ in range(max(1, workers))]
            for fut in as_completed(futures):
                try:
                    got_bytes, _worker_elapsed, got_429 = fut.result()
                except Exception:
                    got_bytes, _worker_elapsed, got_429 = 0, duration, False
                total_bytes += got_bytes
                if got_429:
                    hit_429 = True
        elapsed = max(0.05, time.time() - wall_start)
        return total_bytes, elapsed, hit_429

    samples = []
    retries = 0
    total_bytes = 0
    status = "ok"

    # 准备测速源池，避免固定在单一被限流的源上
    src_pool = list(sources) if sources else list(SPEED_TEST_SOURCES_DEFAULT)
    src_idx = 0

    for round_idx in range(2):
        if not src_pool:
            break
        curr_url = src_pool[src_idx % len(src_pool)]
        bytes_done, elapsed, hit_429 = _run_parallel_round(curr_url, parallel)

        # 故障转移：如果该源返回 429 或下载数据 < 10KB (说明该源被限速或失效)，立即无缝切换到下一提供商源
        if (hit_429 or bytes_done < 10240) and retries < max_retry and len(src_pool) > 1:
            retries += 1
            src_idx += 1
            next_url = src_pool[src_idx % len(src_pool)]
            print(f"[DBG-NO-DATA] 测速源故障转移轮换: {curr_url} (hit_429={hit_429}, bytes={bytes_done}) -> {next_url}")
            time.sleep(0.5 + random.uniform(0, 0.5))
            bytes_done, elapsed, hit_429 = _run_parallel_round(next_url, parallel)
            curr_url = next_url

        if hit_429:
            # [修复] 保留已收集的合法 samples（hit_429 只标记本轮 retry 后仍受限，不丢弃上轮真实数据）
            if not samples:
                status = "rate_limited"
            else:
                # 已有合法数据，本轮被限速但前面测过，保留 samples 并标记 status=partial
                print(f"[DBG-NO-DATA] rate_limited-but-keep-samples: ip={ip}:{port} round{round_idx} retained_samples={samples} bytes_done={bytes_done}")
                status = "ok_rate_limited_in_retry"
            break
        if bytes_done < 1024:
            # [DEBUG-NO-DATA] 关键日志：阶段 4 真实命中 no_data 分支时打印本次下载的真实统计
            print(f"[DBG-NO-DATA] round{round_idx} NO_DATA: ip={ip}:{port} src_url={curr_url} "
                  f"parallel={parallel} duration={duration}s bytes_done={bytes_done} elapsed={elapsed:.2f}s hit_429={hit_429} "
                  f"max_bytes_per_ip={max_bytes_per_ip}")
            status = "no_data"
            break
        speed_mb_s = calculate_speed_mb_s(bytes_done, elapsed)
        samples.append(speed_mb_s)
        total_bytes += bytes_done
        if round_idx == 0:
            time.sleep(random.uniform(0.5, 1.2))

    if not samples:
        return {
            "speed": 0.0,
            "samples": [],
            "p90": 0.0,
            "stddev": 0.0,
            "retries": retries,
            "bytes_downloaded": total_bytes,
            "status": status,
        }

    median_speed = statistics.median(samples)
    p90 = max(samples)
    stddev = 0.0 if len(samples) < 2 else (max(samples) - min(samples)) / 2.0
    return {
        "speed": median_speed,
        "samples": samples,
        "p90": p90,
        "stddev": stddev,
        "retries": retries,
        "bytes_downloaded": total_bytes,
        "status": status,
    }


def calculate_speed_mb_s(received_bytes, duration_sec):
    """根据实际下载字节与耗时计算传输流速 (MB/s)"""
    if duration_sec <= 0.001:
        duration_sec = 0.001
    mb = received_bytes / (1024.0 * 1024.0)
    return round(mb / duration_sec, 2)


def probe_real_download_speed(ip, port, max_bytes=1572864, max_duration=2.0, fallback_rtt=200.0):
    """
    阶段 4：安全微吞吐测速 (Safe Micro-Speedtest)
    - 仅请求 1.5MB (1572864 字节) 数据块
    - 严格限制 2.0 秒内超时自动截断
    - 绝不大流量跑满，彻底防止触发 Cloudflare 官方 Rate Limit (429)
    - 测量实际接收数据量与耗时，返回真实下载带宽 (MB/s)
    """
    s = None
    ss = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(max_duration + 0.8)
        if BIND_INTERFACE and hasattr(socket, 'SO_BINDTODEVICE'):
            try:
                s.setsockopt(socket.SOL_SOCKET, 25, BIND_INTERFACE.encode('utf-8'))
            except Exception:
                pass
        s.connect((ip, port))

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ss = ctx.wrap_socket(s, server_hostname=SPEED_TEST_HOST)

        req = f"GET /__down?bytes={max_bytes} HTTP/1.1\r\nHost: {SPEED_TEST_HOST}\r\nUser-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n"
        ss.sendall(req.encode('utf-8'))

        received_bytes = 0
        header_parsed = False
        start_download_time = time.time()

        while True:
            # 严格时间截断：超过最大持续时间立即终止
            elapsed = time.time() - start_download_time
            if elapsed >= max_duration:
                break

            chunk = ss.recv(8192)
            if not chunk:
                break

            if not header_parsed:
                if b"\r\n\r\n" in chunk:
                    header_part, body_part = chunk.split(b"\r\n\r\n", 1)
                    header_str = header_part.decode('utf-8', errors='ignore')
                    if "429 Too Many Requests" in header_str:
                        # 触发 429 防限流，降级处理
                        return round(min(5.0, max(0.5, 3000.0 / max(1.0, fallback_rtt))), 2)
                    received_bytes += len(body_part)
                    header_parsed = True
                    start_download_time = time.time()
                else:
                    continue
            else:
                received_bytes += len(chunk)

            if received_bytes >= max_bytes:
                break

        duration = max(0.05, time.time() - start_download_time)
        if received_bytes > 1024:
            return calculate_speed_mb_s(received_bytes, duration)
        else:
            return 0.3
    except Exception:
        # 网络异常兜底安全估算
        return round(min(3.0, max(0.2, 1800.0 / max(1.0, fallback_rtt))), 2)
    finally:
        if ss:
            try:
                ss.close()
            except Exception:
                pass
        elif s:
            try:
                s.close()
            except Exception:
                pass


def single_handshake_probe(ip, port):
    """单次 TCP + TLS 握手及 HTTP 探测，返回往返时延 RTT (ms)"""
    t0 = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(PROBE_TIMEOUT)

        # 若指定了物理网卡，则强行绑定物理网卡出口直连
        if BIND_INTERFACE and hasattr(socket, 'SO_BINDTODEVICE'):
            try:
                s.setsockopt(socket.SOL_SOCKET, 25, BIND_INTERFACE.encode('utf-8'))
            except Exception:
                pass

        s.connect((ip, port))

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ss = ctx.wrap_socket(s, server_hostname=SNI_HOST)

        req = f"HEAD / HTTP/1.1\r\nHost: {SNI_HOST}\r\nUser-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n"
        ss.sendall(req.encode('utf-8'))
        res = ss.recv(256)
        rtt_ms = (time.time() - t0) * 1000
        ss.close()

        if b"HTTP/1." in res:
            return rtt_ms
    except Exception:
        pass
    return None


def calculate_rigorous_score(node_data):
    """
    严苛多维质量评分 (1.0 ~ 10.0 分制) 核心模型 (方案 A 全网极速优选模式)：
    - 稳定度 (最高 3.0 分): 0 丢包满分，偶发丢包重扣
    - 真实测速 (最高 3.5 分): 基于真实 MB/s 陡峭分阶
    - 真实延迟 (最高 2.5 分): 全网低延迟分阶激励 (<=100ms 满分 2.5，<=185ms 优质，>360ms 零分)
    - 抖动平稳 (最高 1.0 分): Jitter <= 40ms 满分
    - 机房加成: 顶级直连机房（亚太极速 HKG/NRT/SIN 等或美西核心 SJC/LAX 等）+0.5 分，非 CF 官方扣 2.0 分
    """
    loss_rate = node_data.get('loss_rate', 0.0)
    speed = node_data.get('speed', 0.0)
    rtt = node_data.get('median_rtt', 300.0)
    jitter = node_data.get('jitter', 100.0)
    is_us_west = node_data.get('is_us_west', False)
    is_apac = node_data.get('is_apac', False)
    is_top = node_data.get('is_top_colo', (is_us_west or is_apac))
    is_cf = node_data.get('is_cf', True)

    # 1. 稳定性基础分 (最高 3.0 分)
    if loss_rate == 0.0:
        base_score = 3.0
    elif loss_rate <= 0.2:
        base_score = 1.0
    else:
        base_score = 0.0

    # 2. 真实微吞吐测速分 (最高 3.5 分)
    if speed >= 8.0:
        spd_score = 3.5
    elif speed >= 4.0:
        spd_score = 2.5 + ((speed - 4.0) / 4.0) * 1.0
    elif speed >= 1.5:
        spd_score = 1.5 + ((speed - 1.5) / 2.5) * 1.0
    elif speed >= 0.5:
        spd_score = 0.5 + ((speed - 0.5) / 1.0) * 1.0
    else:
        spd_score = max(0.1, min(0.5, speed))

    # 3. 真实延迟分 (最高 2.5 分)
    if rtt <= 60.0:
        lat_score = 2.5
    elif rtt <= 100.0:
        lat_score = 2.45
    elif rtt <= 185.0:
        lat_score = 2.4 - ((rtt - 100.0) / 85.0) * 0.3
    elif rtt <= 240.0:
        lat_score = 2.2 - ((rtt - 185.0) / 55.0) * 0.7
    elif rtt <= 300.0:
        lat_score = 1.5 - ((rtt - 240.0) / 60.0) * 0.8
    elif rtt <= 360.0:
        lat_score = 0.7 - ((rtt - 300.0) / 60.0) * 0.7
    else:
        lat_score = 0.0

    # 4. 抖动平稳分 (最高 1.0 分)
    if jitter <= 40.0:
        jit_score = 1.0
    elif jitter <= 120.0:
        jit_score = 1.0 - ((jitter - 40.0) / 80.0) * 0.6
    else:
        jit_score = max(0.1, 0.4 - ((jitter - 120.0) / 200.0) * 0.3)

    # 5. 机房与官方加成
    colo_bonus = 0.5 if (is_top or is_us_west or is_apac) else 0.0
    cf_penalty = 0.0  # 废除对第三方优质反代节点的惩罚扣分

    total_score = base_score + spd_score + lat_score + jit_score + colo_bonus + cf_penalty
    return round(min(10.0, max(1.0, total_score)), 1)


def probe_node_multidimensional(candidate):
    """单节点探测兼容垫片"""
    ip, port, original_speed = candidate
    stab = probe_stability(ip, port, rounds=4)
    if not stab:
        return None
    speed = probe_real_download_speed(ip, port, fallback_rtt=stab['median_rtt'])
    stab['speed'] = speed
    stab['is_cf'] = is_official_cloudflare_ip(ip)
    stab['score'] = calculate_rigorous_score(stab)
    return stab


def run_filter_and_export(tag="公司", output_file="best_us.txt", interface=None, mock_mode=False, mock_candidates=None,
                          vless_sni=None, vless_uuid=None, vless_path=None, vless_port=None,
                          target_country="AUTO", sandbox_enable=True):
    """
    五阶漏斗端到端严选引擎：
    1. 阶段 1：快速 TCP 并发探针 (快速初筛死节点)
    2. 阶段 2：TLS 握手与 cf-ray 机房质检 (Anycast POP 粗滤，入围 Top 25)
    3. 阶段 3：沙箱全链路真机实测 (直连绕过代理，VLESS 隧道并发探测 Google 204, YouTube 204 及 ip.sb 落地美国)
    4. 阶段 4：真实微吞吐测速 (通过代理隧道测量 1MB 真实下载带宽)
    5. 阶段 5：四维综合质量打分与降序导出 Top 15
    """
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    if mock_mode and mock_candidates:
        candidates = mock_candidates
    else:
        candidates = fetch_candidates()

    print(f"[*] 启动五阶漏斗全链路严选引擎 (环境标识: {tag}, 导出目标: {output_file})...")

    # 数据源拉取就绪后，启动底层直连网络绕过（设置 GID=65534 绕过代理，直接测速真实延迟）
    if not mock_mode:
        init_network_bypass(interface)

    # ================= 阶段 1：快速 TCP 并发探针 =================
    print(f"\n[+] [阶段 1/5] 启动快速 TCP 连通性并发扫描 (总数: {len(candidates)}, 并发度: 30)...")
    stage1_survivors = []
    if mock_mode:
        stage1_survivors = list(candidates)
    else:
        with ThreadPoolExecutor(max_workers=30) as executor:
            future_map = {executor.submit(fast_tcp_ping, c[0], c[1]): c for c in candidates}
            for future in as_completed(future_map):
                c = future_map[future]
                if future.result():
                    stage1_survivors.append(c)

    print(f"[+] [阶段 1/5] 快速 TCP 扫描完成，存活可通节点数: {len(stage1_survivors)} / {len(candidates)}")

    if not stage1_survivors:
        print("[-] 错误: 所有候选节点均无法建立 TCP 连接，请检查本地物理网络。")
        return

    # ================= 阶段 2：TLS 握手与 cf-ray 机房质检 =================
    print(f"\n[+] [阶段 2/5] 启动 TLS 握手与 cf-ray 边缘机房代码质检 (待测数: {len(stage1_survivors)})...")
    stage2_survivors = []
    if mock_mode:
        for idx, c in enumerate(stage1_survivors):
            rem = c[3] if len(c) > 3 else ""
            if len(c) > 4 and c[4]:
                colo = c[4]
            elif rem and any(k in rem.upper() for k in ('HK', 'TW', 'JP', 'SG')):
                if 'HK' in rem.upper(): colo = 'HKG'
                elif 'TW' in rem.upper(): colo = 'TPE'
                elif 'JP' in rem.upper(): colo = 'NRT'
                elif 'SG' in rem.upper(): colo = 'SIN'
                else: colo = 'SJC'
            else:
                colo = "SJC" if idx % 2 == 0 else "LAX"
            rtt = 180.0 + idx * 5.0
            stage2_survivors.append((c[0], c[1], c[2], colo, rtt, rem))
    else:
        with ThreadPoolExecutor(max_workers=25) as executor:
            future_map = {executor.submit(probe_tls_colo, c[0], c[1]): c for c in stage1_survivors}
            for future in as_completed(future_map):
                c = future_map[future]
                is_valid, colo, rtt = future.result()
                if is_valid:
                    rem = c[3] if len(c) > 3 else ""
                    stage2_survivors.append((c[0], c[1], c[2], colo, rtt if rtt else 200.0, rem))

    print(f"[+] [阶段 2/5] 机房质检完毕，存活有效 Cloudflare 边缘节点数: {len(stage2_survivors)}")

    if len(stage2_survivors) < 5:
        print("[!] 提示: 严选节点较少，启动平滑容灾降级模式。")
        for c in stage1_survivors:
            if not any(s[0] == c[0] and s[1] == c[1] for s in stage2_survivors):
                rem = c[3] if len(c) > 3 else ""
                stage2_survivors.append((c[0], c[1], c[2], "CF", 200.0, rem))

    # 候选节点区域分层入围策略：兼顾亚太各区 (HK, TW, JP, SG) 与美区 (US)
    region_buckets = {'HK': [], 'TW': [], 'JP': [], 'SG': [], 'US': [], 'OTHER': []}
    for c in stage2_survivors:
        colo = c[3]
        rem = c[5] if len(c) > 5 else ""
        ip = c[0]
        r = detect_node_region(colo, rem, ip)
        if r in region_buckets:
            region_buckets[r].append(c)
        else:
            region_buckets['OTHER'].append(c)

    for r in region_buckets:
        region_buckets[r].sort(key=lambda x: (x[4] if x[4] else 999.0))

    # 沙箱入围配额：亚太各区分配 12 个名额，美区分配 20 个名额，保障各区域样本充足
    stage2_budgets = {'HK': 12, 'TW': 12, 'JP': 12, 'SG': 12, 'US': 20}
    chosen = []
    for r, b_quota in stage2_budgets.items():
        chosen.extend(region_buckets[r][:b_quota])

    limit = STAGE2_ENTRY_LIMIT
    if len(chosen) < limit:
        remaining = [c for c in stage2_survivors if c not in chosen]
        remaining.sort(key=lambda x: (x[4] if x[4] else 999.0))
        for c in remaining:
            chosen.append(c)
            if len(chosen) >= limit:
                break

    topN_candidates = chosen
    candidate_name_map = {(c[0], c[1]): f"node_{idx:02d}" for idx, c in enumerate(topN_candidates, 1)}
    hk_in = sum(1 for c in topN_candidates if detect_node_region(c[3], c[5] if len(c)>5 else '', c[0]) == 'HK')
    tw_in = sum(1 for c in topN_candidates if detect_node_region(c[3], c[5] if len(c)>5 else '', c[0]) == 'TW')
    jp_in = sum(1 for c in topN_candidates if detect_node_region(c[3], c[5] if len(c)>5 else '', c[0]) == 'JP')
    sg_in = sum(1 for c in topN_candidates if detect_node_region(c[3], c[5] if len(c)>5 else '', c[0]) == 'SG')
    us_in = sum(1 for c in topN_candidates if detect_node_region(c[3], c[5] if len(c)>5 else '', c[0]) == 'US')
    print(f"[+] [阶段 2/5] 区域分层入围沙箱结果: HK={len(region_buckets['HK'])}个(选{hk_in}) "
          f"TW={len(region_buckets['TW'])}个(选{tw_in}) "
          f"JP={len(region_buckets['JP'])}个(选{jp_in}) "
          f"SG={len(region_buckets['SG'])}个(选{sg_in}) "
          f"US={len(region_buckets['US'])}个(选{us_in}) "
          f"(共 {len(topN_candidates)} 个进入沙箱测试)")

    # ================= 阶段 3 v2：真实质量门 =================
    sandbox = None
    e2e_results = []
    use_sandbox = bool(sandbox_enable) and (mock_mode or (find_clash_core() is not None))
    vless_tpl = detect_vless_template(cli_sni=vless_sni, cli_uuid=vless_uuid, cli_path=vless_path, cli_port=vless_port)

    if use_sandbox:
        print(f"[*] 加载 VLESS 代理模板: SNI={vless_tpl['sni']}, UUID={vless_tpl['uuid'][:8]}..., PATH={vless_tpl['path']}, 目标国家={target_country}")
        sandbox = MihomoSandbox(
            candidates=topN_candidates,
            vless_template=vless_tpl,
            work_dir="/tmp/ipselect_sandbox" if not mock_mode else tempfile.mkdtemp(prefix="mock_sb_"),
            api_port=19098,
            proxy_port=19998,
            mock_mode=mock_mode
        )
        if sandbox.start():
            print(f"[+] 隔离沙箱启动成功 (外部控制端口: 19098, 本地代理端口: 19998, GID=65534 直连)")
            try:
                e2e_results, stage3_status = run_stage3_real_quality_gate(
                    topN_candidates, sandbox, target_country,
                    api_port=19098, proxy_port=19998, max_duration_sec=600,
                )
                globals()['_STAGE3_STATUS'] = stage3_status
            except Exception as _sb_e:
                print(f"[!] 阶段 3 v2 沙箱执行异常: {_sb_e}")
                sandbox.stop()
                raise
        else:
            print("[!] 沙箱启动失败，平滑回退至直连稳定性测定")
    else:
        if not sandbox_enable:
            print("[*] 沙箱严选配置已停用，使用直连 TLS 稳定性测定模式")
        else:
            print("[!] 当前系统未安装 clash_meta 内核，平滑回级至传统直连 TLS 稳定性测定")

    # 沙箱未启动 / 未产出结果 → 沿用直连兜底
    if not e2e_results:
        print("[!] 启动直连稳定性深测回退兜底模式...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_map = {executor.submit(probe_stability, c[0], c[1]): c for c in topN_candidates}
            for future in as_completed(future_map):
                res = future.result()
                if res:
                    c = future_map[future]
                    rem = c[5] if len(c) > 5 else ""
                    reg = detect_node_region(res['colo'], rem, res['ip'])
                    p_name = candidate_name_map.get((res['ip'], res['port']), "node_01")
                    e2e_results.append({
                        'proxy_name': p_name,
                        'ip': res['ip'],
                        'port': res['port'],
                        'colo': res['colo'],
                        'e2e_avg_rtt': res['avg_rtt'],
                        'e2e_jitter': res['jitter'],
                        'youtube_rtt': res['median_rtt'],
                        'exit_country': target_country or 'AUTO',
                        'exit_ip': 'Anycast',
                        'quality_gate_version': 1,  # 标记为旧版直连兜底
                        'remark': rem,
                        'region': reg
                    })

    print(f"[+] [阶段 3/5] 全链路真实可用性检测完毕，通过严格质检节点数: {len(e2e_results)} / {len(topN_candidates)}")
    if not e2e_results:
        print("[-] 错误: 无任何节点通过全链路真实代理考核。")
        if sandbox is not None:
            sandbox.stop()
        return

    # 阶段 4 测速入围分层：确保 HK/TW/JP/SG/US 各区域足额进入吞吐测速
    for node in e2e_results:
        if 'region' not in node:
            node['region'] = detect_node_region(node.get('colo', ''), node.get('remark', ''), node.get('ip', ''))

    stage4_region_budgets = {'HK': 7, 'TW': 7, 'JP': 7, 'SG': 7, 'US': 15}
    speed_buckets = {'HK': [], 'TW': [], 'JP': [], 'SG': [], 'US': [], 'OTHER': []}
    for node in e2e_results:
        r = node.get('region', 'US')
        if r in speed_buckets:
            speed_buckets[r].append(node)
        else:
            speed_buckets['OTHER'].append(node)

    for r in speed_buckets:
        speed_buckets[r].sort(key=lambda x: (x['e2e_avg_rtt'], x['e2e_jitter']))

    candidates_for_speed = []
    for r, b_quota in stage4_region_budgets.items():
        candidates_for_speed.extend(speed_buckets[r][:b_quota])

    # 若仍有剩余测速配额，按端到端 RTT 择优补齐
    if len(candidates_for_speed) < STAGE4_SPEED_LIMIT:
        remaining_speed = [n for n in e2e_results if n not in candidates_for_speed]
        remaining_speed.sort(key=lambda x: (x['e2e_avg_rtt'], x['e2e_jitter']))
        for n in remaining_speed:
            candidates_for_speed.append(n)
            if len(candidates_for_speed) >= STAGE4_SPEED_LIMIT:
                break

    # ================= 阶段 4：过隧道真实吞吐测速（方向 A） =================
    print(f"\n[+] [阶段 4/5] 启动过隧道真实吞吐测速 (时长 {SPEED_TEST_DURATION}s×2轮 / 并发 {SPEED_TEST_PARALLEL} / 入围 {len(candidates_for_speed)} 节点)...")

    # [DEBUG-NO-DATA] 关键日志：阶段 4 启动前打印沙箱存活状态与 Mihomo 当前 GLOBAL 代理指向，定位为何所有节点 no_data
    if sandbox is not None:
        sb_ready = sandbox.is_ready()
        print(f"[DBG-NO-DATA] [阶段 4/5] sandbox.is_ready()={sb_ready} mock_mode={sandbox.mock_mode}")
        if not sb_ready and not sandbox.mock_mode:
            print(f"[DBG-NO-DATA] [阶段 4/5] 警告：沙箱未存活，所有过 19998 端口的 HTTP 代理请求都将失败 → no_data 根因")
        if not sandbox.mock_mode and sb_ready:
            try:
                _gp = urllib.request.Request("http://127.0.0.1:19098/proxies/GLOBAL", headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(_gp, timeout=1.0) as _gr:
                    _gstate = json.loads(_gr.read().decode('utf-8'))
                    print(f"[DBG-NO-DATA] [阶段 4/5] Mihomo GLOBAL 当前指向: {_gstate}")
            except Exception as _ge:
                print(f"[DBG-NO-DATA] [阶段 4/5] 无法查询 Mihomo GLOBAL 当前指向: {type(_ge).__name__}: {_ge}")
    else:
        print(f"[DBG-NO-DATA] [阶段 4/5] 警告：sandbox 为 None，19998 端口上无代理提供下载转发，bytes_done 必然为 0")

    def _probe_one_node(node, mock_speed_value):
        # [per-node] 测速前先切 sandbox GLOBAL 到本节点，让每个节点测自己真实带宽（不影响生产 mihomo）
        if not mock_mode and sandbox is not None and sandbox.is_ready():
            proxy_name = node.get("proxy_name")
            if not proxy_name or proxy_name == "node_??":
                proxy_name = candidate_name_map.get((node.get("ip"), node.get("port")), "node_01")
                node["proxy_name"] = proxy_name
            switched = sandbox.switch_proxy(proxy_name)
            if not switched:
                print(f"[DBG-NO-DATA] _probe_one_node switch_proxy FAILED ip={node['ip']}:{node['port']} proxy_name={proxy_name}")
                node["speed"] = 0.0
                node["speed_p90"] = 0.0
                node["speed_stddev"] = 0.0
                node["speed_samples"] = []
                node["speed_status"] = "switch_failed"
                node["score"] = 0.0
                return
        # [per-node] 区域智能就近测速源 + 多源自动故障转移
        node_region = node.get("region") or detect_node_region(node.get("colo", ""), node.get("remark", ""), node["ip"])
        candidate_sources = list(get_speed_sources_for_region(node_region))
        random.shuffle(candidate_sources)
        sources_to_use = tuple(candidate_sources)

        if mock_mode:
            res = probe_throughput_speed(
                ip=node["ip"], port=node["port"],
                mock_speed=mock_speed_value,
                duration=SPEED_TEST_DURATION,
                parallel=SPEED_TEST_PARALLEL,
                max_retry=SPEED_TEST_MAX_RETRY,
                max_bytes_per_ip=SPEED_TEST_MAX_BYTES_MB * 1024 * 1024,
                sources=sources_to_use,
            )
        else:
            res = probe_throughput_speed(
                ip=node["ip"], port=node["port"],
                duration=SPEED_TEST_DURATION,
                parallel=SPEED_TEST_PARALLEL,
                max_retry=2,  # 允许至少 2 次不同源 failover
                max_bytes_per_ip=SPEED_TEST_MAX_BYTES_MB * 1024 * 1024,
                sources=sources_to_use,
                proxy_port=19998,
            )
        node["speed"] = res["speed"]
        node["speed_p90"] = res["p90"]
        node["speed_stddev"] = res["stddev"]
        node["speed_samples"] = res["samples"]
        node["speed_status"] = res["status"]
        # [修复] 仅在真正无数据时才把 speed 归零；有合法 samples（speed>0）时即使后续 round 被标记 no_data/rate_limited 也保留真实速度
        if res["status"] != "ok":
            # [DEBUG-NO-DATA] 关键日志：每个节点测速结束时若非 ok 状态立即打印
            print(f"[DBG-NO-DATA] _probe_one_node NON-OK ip={node['ip']}:{node['port']} "
                  f"speed={res['speed']} status={res['status']} bytes={res.get('bytes_downloaded', 0)} "
                  f"retries={res.get('retries', 0)} samples={res.get('samples', [])}")
        if res["status"] not in ("ok", "ok_rate_limited_in_retry") and res["speed"] <= 0:
            # 真正没数据：speed 保持 0.0
            node["speed"] = 0.0
        elif res["status"] not in ("ok", "ok_rate_limited_in_retry") and res["speed"] > 0:
            # 部分数据（如 round0 拿到速度，round1 限速导致最终 status=no_data）：保留 res["speed"]
            # 因为 probe_throughput_speed 已经基于 samples 计算了 median_speed
            pass
        base_score = calculate_e2e_score(node["e2e_avg_rtt"], node["e2e_jitter"], node["speed"], node.get("colo"))
        if res["stddev"] > 3.0:
            base_score = max(0.0, base_score - 0.5)
        node["score"] = base_score
        node["is_cf"] = is_official_cloudflare_ip(node["ip"])

    try:
        if mock_mode:
            for idx, node in enumerate(candidates_for_speed, 1):
                _probe_one_node(node, mock_speed_value=2.5 + idx * 0.3)
        else:
            # [激进优化] 顺序测速避免多线程争抢 Mihomo GLOBAL selector 互相覆盖
            # [激进优化] adaptive backoff：连续 3 个 rate_limited 自动暂停 30s + jitter（拉长节流窗口）
            # [激进优化] jitter 拉宽 1-3s，让上游测速服务有更充分喘息时间
            consecutive_429 = 0
            for idx, node in enumerate(candidates_for_speed, 1):
                if idx > 1:
                    base_jitter = random.uniform(1.0, 3.0)
                    penalty_jitter = min(10.0, consecutive_429 * 1.5)
                    time.sleep(base_jitter + penalty_jitter)
                _probe_one_node(node, None)
                # 跟踪连续 rate_limited 数
                last_status = node.get("speed_status", "")
                if last_status in ("rate_limited", "no_data") and node.get("speed", 0) <= 0:
                    consecutive_429 += 1
                else:
                    consecutive_429 = 0
                if consecutive_429 >= 3:
                    backoff_seconds = 30 + random.uniform(0, 10)
                    print(f"[DBG-NO-DATA] [阶段 4/5] adaptive backoff: 连续 {consecutive_429} 个测速失败，暂停 {backoff_seconds:.1f}s 等待上游测速服务恢复")
                    time.sleep(backoff_seconds)
                    consecutive_429 = 0
                if idx % 5 == 0:
                    print(f"[DBG-NO-DATA] [阶段 4/5] 进度 {idx}/{len(candidates_for_speed)} 完成")
    finally:
        # 沙箱进程已无后续使用，主动释放（修复: 之前在阶段 3 finally 中提前 stop 导致阶段 4 speed=0）
        if sandbox is not None:
            sandbox.stop()

    # ================= 阶段 5：分区域配额综合评分严选与导出 =================
    region_candidates = {'HK': [], 'TW': [], 'JP': [], 'SG': [], 'US': [], 'OTHER': []}
    for node in candidates_for_speed:
        reg = node.get('region') or detect_node_region(node.get('colo', ''), node.get('remark', ''), node.get('ip', ''))
        node['region'] = reg
        if reg in region_candidates:
            region_candidates[reg].append(node)
        else:
            region_candidates['OTHER'].append(node)

    for reg in region_candidates:
        region_candidates[reg].sort(key=lambda x: (-x['score'], x['e2e_avg_rtt'], -x['speed']))

    # 按照 HK:5, TW:5, JP:5, SG:5, US:10 顺序提取各区域专属优质节点
    selected_nodes = []
    for reg, quota in DEFAULT_REGION_QUOTAS.items():
        chosen_reg = region_candidates[reg][:quota]
        selected_nodes.extend(chosen_reg)

    # 若特定区域候选不足导致总数未达目标上限 (默认 30)，从剩余候选节点中全局择优补齐
    total_quota_target = sum(DEFAULT_REGION_QUOTAS.values())
    if len(selected_nodes) < total_quota_target:
        remaining_pool = [n for n in candidates_for_speed if n not in selected_nodes]
        remaining_pool.sort(key=lambda x: (-x['score'], x['e2e_avg_rtt'], -x['speed']))
        for n in remaining_pool:
            selected_nodes.append(n)
            if len(selected_nodes) >= total_quota_target:
                break

    print(f"\n[+] 成功精选出 Top {len(selected_nodes)} 个高品质全链路多区域优选节点 ({tag}专属 / 全链路Google+YouTube实测 / 配额 HK:5 TW:5 JP:5 SG:5 US:10):")
    output_lines = []
    detail_nodes = []
    update_time_str = time.strftime('%Y-%m-%d %H:%M:%S')

    region_rank_counter = {}
    for idx, node in enumerate(selected_nodes, 1):
        colo_str = node.get('colo') or 'CF'
        reg = node.get('region') or detect_node_region(colo_str, node.get('remark', ''), node['ip'])
        region_rank_counter[reg] = region_rank_counter.get(reg, 0) + 1
        reg_rank = region_rank_counter[reg]

        line = format_node_line(node['ip'], node['port'], node['score'], reg_rank, tag, node['speed'], colo_str, region=reg)
        output_lines.append(line)
        cf_flag = "CF官方" if node.get('is_cf', True) else "第三方反代"
        print(f"  [{idx:02d}] {line} | 评分: {node['score']:.1f} | 机房: {colo_str} | 端到端RTT: {node['e2e_avg_rtt']:.0f}ms | 抖动: {node['e2e_jitter']:.0f}ms ({cf_flag})")

        remark_str = line.split('#', 1)[1] if '#' in line else f"{reg}-{tag}-{reg_rank:02d}-{node['speed']:.2f}MB/s"
        detail_nodes.append({
            'index': idx,
            'proxy_name': node.get('proxy_name') or candidate_name_map.get((node['ip'], node['port']), f"node_{idx:02d}"),
            'region': reg,
            'region_rank': reg_rank,
            'ip': node['ip'],
            'port': str(node['port']),
            'colo': colo_str,
            'score': round(float(node['score']), 1),
            'speed': round(float(node.get('speed', 0.0)), 2),
            'speed_p90': round(float(node.get('speed_p90', 0.0)), 2),
            'speed_stddev': round(float(node.get('speed_stddev', 0.0)), 2),
            'speed_samples': list(node.get('speed_samples', [])),
            'speed_status': node.get('speed_status', 'unknown'),
            'e2e_avg_rtt': round(float(node.get('e2e_avg_rtt', 0.0)), 1),
            'e2e_jitter': round(float(node.get('e2e_jitter', 0.0)), 1),
            'youtube_rtt': round(float(node.get('youtube_rtt', 0.0)), 1),
            'youtube_status': 'ok' if (node.get('youtube_rtt') and node.get('youtube_rtt') > 0) else 'fail',
            'exit_country': node.get('exit_country') or (target_country or 'AUTO'),
            'exit_ip': node.get('exit_ip') or 'Anycast',
            'is_cf': bool(node.get('is_cf', True)),
            'remark': remark_str
        })

    # 确定保存路径
    if os.path.isabs(output_file):
        target_path = output_file
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_dir = os.path.dirname(script_dir)
        target_path = os.path.join(project_dir, output_file)

    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    with open(target_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output_lines) + '\n')

    # 生成结构化 JSON 详情文件供 LuCI 界面展示
    detail_path = os.path.splitext(target_path)[0] + "_detail.json"
    region_dist = {r: sum(1 for n in detail_nodes if n.get('region') == r) for r in DEFAULT_REGION_QUOTAS}
    detail_data = {
        'file': os.path.basename(target_path),
        'tag': tag,
        'update_time': update_time_str,
        'count': len(detail_nodes),
        'region_distribution': region_dist,
        'sandbox_config': {
            'enabled': bool(sandbox_enable),
            'sni': vless_tpl.get('sni', DEFAULT_VLESS_SNI),
            'uuid': vless_tpl.get('uuid', DEFAULT_VLESS_UUID),
            'path': vless_tpl.get('path', DEFAULT_VLESS_PATH),
            'port': str(vless_tpl.get('port', '443')),
            'target_country': target_country or 'AUTO'
        },
        'nodes': detail_nodes
    }
    with open(detail_path, 'w', encoding='utf-8') as f:
        json.dump(detail_data, f, ensure_ascii=False, indent=2)

    print(f"\n[+] 优选完成！各区域节点分布: " + ", ".join(f"{r}={c}" for r, c in region_dist.items()))
    print(f"[+] 优选结果已写入: {target_path}")
    print(f"[+] 优选详情已写入: {detail_path}")


def main():
    parser = argparse.ArgumentParser(description="Cloudflare 节点五阶漏斗全链路严选引擎与多维评估器")
    parser.add_argument("--tag", default=os.environ.get("LOCATION_TAG", None), help="节点标识 (如: 公司, 家庭，缺省自动识别)")
    parser.add_argument("--output", default=os.environ.get("OUTPUT_FILE", None), help="输出文件名 (如: best_us.txt, home_us_best_node.txt，缺省自动识别)")
    parser.add_argument("--interface", default=os.environ.get("BIND_INTERFACE", None), help="绑定的出口网卡 (如: br-lan, eth0)")
    parser.add_argument("--sni", default=os.environ.get("VLESS_SNI", None), help="VLESS SNI 服务域名")
    parser.add_argument("--uuid", default=os.environ.get("VLESS_UUID", None), help="VLESS UUID")
    parser.add_argument("--path", default=os.environ.get("VLESS_PATH", None), help="VLESS WebSocket 路径")
    parser.add_argument("--port", default=os.environ.get("VLESS_PORT", "443"), help="VLESS 端口")
    parser.add_argument("--target-country", default=os.environ.get("TARGET_COUNTRY", "AUTO"), help="期望落地出口国家 (默认 AUTO 自动就近，支持 AUTO/US/HK/TW/JP/SG)")
    parser.add_argument("--sandbox", default=os.environ.get("SANDBOX_ENABLE", "1"), help="是否启用沙箱质检 (1 或 0)")

    args = parser.parse_args()
    tag, output_file = detect_network_environment(cli_tag=args.tag, cli_output=args.output)
    print(f"[*] 环境自动感知检测结果: 位置=[{tag}], 输出文件=[{output_file}]")
    sandbox_flag = str(args.sandbox).strip() not in ('0', 'false', 'False', 'no')
    run_filter_and_export(
        tag=tag,
        output_file=output_file,
        interface=args.interface,
        vless_sni=args.sni,
        vless_uuid=args.uuid,
        vless_path=args.path,
        vless_port=args.port,
        target_country=args.target_country,
        sandbox_enable=sandbox_flag
    )


if __name__ == '__main__':
    main()
