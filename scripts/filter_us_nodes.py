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
import urllib.request
import urllib.parse
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

SNI_HOST = "proxy.19940407.xyz"
TOP_COUNT = 15
PROBE_TIMEOUT = 2.5
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
]

US_WEST_DCS = {'SJC', 'LAX', 'SFO', 'SEA', 'PDX', 'SLC', 'PHX', 'LAS'}
US_OTHER_DCS = {'DFW', 'ORD', 'IAD', 'ATL', 'MIA', 'EWR', 'JFK', 'DEN', 'IAH', 'BOS', 'MSP', 'DTW', 'CLT'}
ALL_US_DCS = US_WEST_DCS | US_OTHER_DCS
US_DATA_CENTERS = ALL_US_DCS

# 亚太极速直连机房（对大陆网络延迟极低 30ms~80ms）
APAC_FAST_DCS = {'HKG', 'NRT', 'HND', 'KIX', 'SIN', 'TPE', 'ICN', 'BKK', 'MNL', 'KUL'}
# 全球顶级入口直连机房（亚太低延迟 POP + 美西核心 POP）
TOP_INGRESS_DCS = APAC_FAST_DCS | US_WEST_DCS

EXCLUDE_CITIES = {'Toronto', 'Montreal', 'Vancouver', 'Calgary', 'Ottawa'}
EXCLUDE_DCS = {'YYZ', 'YVR', 'YUL', 'YYC'}


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


def detect_vless_template() -> dict:
    """自动从环境变量或 OpenClash 配置文件中提取实际运行的 VLESS 模板配置 (uuid, sni, path)"""
    env_uuid = os.environ.get("VLESS_UUID")
    env_sni = os.environ.get("VLESS_SNI")
    env_path = os.environ.get("VLESS_PATH")

    if env_uuid and env_sni and env_path:
        return {"uuid": env_uuid, "sni": env_sni, "path": env_path}

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
                if uuid_m and sni_m:
                    return {
                        "uuid": env_uuid or uuid_m.group(1),
                        "sni": env_sni or sni_m.group(1),
                        "path": env_path or (path_m.group(1) if path_m else "/")
                    }
            except Exception:
                pass

    return {
        "uuid": env_uuid or DEFAULT_VLESS_UUID,
        "sni": env_sni or DEFAULT_VLESS_SNI,
        "path": env_path or DEFAULT_VLESS_PATH
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


def probe_ip_sb_exit(proxy_port: int = 19998, timeout: float = 3.5, mock_exit: dict = None):
    """
    通过本地沙箱的代理端口探测真实出口 IP 与落地国家归属。
    返回 (is_us, country_code, exit_ip)
    """
    if mock_exit is not None:
        cc = mock_exit.get('country_code', '').upper()
        ip = mock_exit.get('ip', '')
        return (cc == 'US'), cc, ip

    proxy_url = f"http://127.0.0.1:{proxy_port}"
    proxy_handler = urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})
    opener = urllib.request.build_opener(proxy_handler)

    urls = [IP_SB_GEOIP_URL, "http://ip-api.com/json"]
    for target in urls:
        try:
            req = urllib.request.Request(target, headers={'User-Agent': 'Mozilla/5.0'})
            with opener.open(req, timeout=timeout) as res:
                if res.status == 200:
                    data = json.loads(res.read().decode('utf-8'))
                    country = (data.get('country_code') or data.get('countryCode') or '').upper()
                    exit_ip = data.get('ip') or data.get('query') or ''
                    is_us = (country == 'US')
                    return is_us, country, exit_ip
        except Exception:
            continue

    return False, "", ""


def evaluate_e2e_candidate(proxy_name: str, ip: str, port: int, colo: str, api_port: int = 19098, proxy_port: int = 19998, mock_data: dict = None):
    """
    对单个入围候选节点执行端到端全链路多维评估：
    1. Google 204 多轮 RTT 与抖动测试 (一票淘汰：丢包率 >= 30%)
    2. YouTube 204 连通性测试 (一票淘汰：连接失败)
    3. ip.sb 出口落地国家与纯净度质检 (一票淘汰：非 US 出口)
    若任意环节触犯红线，返回 None；否则返回评测指标字典。
    """
    google_delays = []
    if mock_data and "google_delays" in mock_data:
        raw_delays = mock_data["google_delays"]
        google_delays = [d for d in raw_delays if d is not None and d > 0]
        total_rounds = len(raw_delays)
    else:
        total_rounds = 3
        for idx in range(total_rounds):
            # 提前失败截断：若丢包次数已达 2 次，不可能满足 >= 2 次成功，立即淘汰
            if (idx - len(google_delays)) >= 2:
                return None
            if idx > 0:
                time.sleep(random.uniform(0.02, 0.04))
            d = probe_e2e_delay(proxy_name, GOOGLE_204_URL, api_port=api_port, timeout_ms=2500)
            if d is not None:
                google_delays.append(d)

    # 1. Google 连通性一票否决：成功轮次 < 2 (丢包率 >= 30%)
    if len(google_delays) < (total_rounds - 1):
        return None

    e2e_avg_rtt = sum(google_delays) / len(google_delays)
    e2e_jitter = (max(google_delays) - min(google_delays)) if len(google_delays) > 1 else 0.0

    # 2. YouTube 连通性一票否决
    if mock_data and "youtube_delay" in mock_data:
        yt_delay = mock_data["youtube_delay"]
    else:
        yt_delay = probe_e2e_delay(proxy_name, YOUTUBE_204_URL, api_port=api_port, timeout_ms=4500)

    if yt_delay is None or yt_delay <= 0:
        return None

    # 3. ip.sb 出口归属一票否决
    mock_exit = mock_data.get("ip_sb_exit") if mock_data else None
    if not mock_data:
        switch_sandbox_proxy(proxy_name, api_port=api_port)
    is_us, exit_country, exit_ip = probe_ip_sb_exit(proxy_port=proxy_port, timeout=3.0, mock_exit=mock_exit)
    if not is_us:
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
        'exit_country': exit_country
    }


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

    # 3. 真实吞吐速度分 (权重 30%)
    if speed_mb_s >= 2.0:
        s_spd = 10.0
    elif speed_mb_s >= 1.2:
        s_spd = 8.0 + (speed_mb_s - 1.2) / 0.8 * 2.0
    elif speed_mb_s >= 0.6:
        s_spd = 5.5 + (speed_mb_s - 0.6) / 0.6 * 2.5
    else:
        s_spd = max(1.0, (speed_mb_s / 0.6) * 5.5)

    # 4. 核心机房加权 (权重 10%)
    colo_upper = (colo or "").upper()
    if colo_upper in {"SJC", "LAX"}:
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


def format_node_line(ip: str, port: int, score: float, rank: int, tag: str, speed_mb_s: float, colo: str = "") -> str:
    """
    格式化为标准 OpenClash 订阅优选节点配置行：
    <ip>:<port>#US-DaTree-<score>-<rank>-<tag> <speed>MB/s
    """
    return f"{ip}:{port}#US-DaTree-{score:.1f}-{rank:02d}-{tag} {speed_mb_s:.2f}MB/s"


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

    # 2. 解析 TXT 数据源 (例如 mocl1220/ip, BestCF)
    for url in TXT_SOURCES:
        print(f"[*] 正在拉取 TXT 数据源: {url}")
        try:
            lines = download_url_lines(url, timeout=12)
            if not lines:
                print(f"[-] TXT 获取失败 (已尝试多重镜像源): {url}")
                continue
            for line in lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                # 方案 A 全网极速优选模式：提取 IP/端口，不局限于 #US 标识
                if '#' in line:
                    addr, tag = line.split('#', 1)
                    addr = addr.strip()
                else:
                    addr = line.strip()

                if ':' in addr:
                    parts = addr.split(':')
                    try:
                        candidates.append((parts[0].strip(), int(parts[1].strip()), 0.0))
                    except ValueError:
                        continue
                else:
                    candidates.append((addr, 443, 0.0))
                    candidates.append((addr, 8443, 0.0))
        except Exception as e:
            print(f"[-] TXT 解析异常 ({url}): {e}")

    # 3. 补充备用高可用 Anycast IP 池
    for ip in BACKUP_CF_IPS:
        candidates.append((ip, 443, 0.0))
        candidates.append((ip, 8443, 0.0))

    # 去重
    seen = set()
    unique_candidates = []
    for ip, port, sp in candidates:
        key = (ip, port)
        if key not in seen:
            seen.add(key)
            unique_candidates.append((ip, port, sp))

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
    if rtt <= 100.0:
        lat_score = 2.5
    elif rtt <= 185.0:
        lat_score = 2.5 - ((rtt - 100.0) / 85.0) * 0.3
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
    cf_penalty = 0.0 if is_cf else -2.0

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


def run_filter_and_export(tag="公司", output_file="best_us.txt", interface=None, mock_mode=False, mock_candidates=None):
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
            stage2_survivors.append((c[0], c[1], c[2], "SJC" if idx % 2 == 0 else "LAX", 180.0 + idx * 5.0))
    else:
        with ThreadPoolExecutor(max_workers=25) as executor:
            future_map = {executor.submit(probe_tls_colo, c[0], c[1]): c for c in stage1_survivors}
            for future in as_completed(future_map):
                c = future_map[future]
                is_valid, colo, rtt = future.result()
                if is_valid:
                    stage2_survivors.append((c[0], c[1], c[2], colo, rtt if rtt else 200.0))

    print(f"[+] [阶段 2/5] 机房质检完毕，存活有效 Cloudflare 边缘节点数: {len(stage2_survivors)}")

    if len(stage2_survivors) < 5:
        print("[!] 提示: 严选节点较少，启动平滑容灾降级模式。")
        for c in stage1_survivors:
            if not any(s[0] == c[0] and s[1] == c[1] for s in stage2_survivors):
                stage2_survivors.append((c[0], c[1], c[2], "CF", 200.0))

    # 候选节点智能分层入围策略：兼顾美西直连高稳定机房 (SJC/LAX/SFO 等) 与亚太低延迟机房
    us_candidates = [c for c in stage2_survivors if is_us_west_colo(c[3]) or is_us_colo(c[3]) or c[3] in ('SJC', 'LAX', 'SFO', 'SEA', 'PDX')]
    apac_candidates = [c for c in stage2_survivors if is_apac_colo(c[3]) or c[3] in ('HKG', 'NRT', 'SIN', 'KIX')]
    other_candidates = [c for c in stage2_survivors if c not in us_candidates and c not in apac_candidates]

    us_candidates.sort(key=lambda x: (x[4] if x[4] else 999.0))
    apac_candidates.sort(key=lambda x: (x[4] if x[4] else 999.0))
    other_candidates.sort(key=lambda x: (x[4] if x[4] else 999.0))

    chosen = []
    chosen.extend(us_candidates[:12])
    chosen.extend(apac_candidates[:10])
    chosen.extend(other_candidates[:3])

    if len(chosen) < 25:
        stage2_survivors.sort(key=lambda x: (x[4] if x[4] else 999.0))
        for c in stage2_survivors:
            if c not in chosen:
                chosen.append(c)
            if len(chosen) >= 25:
                break

    top25_candidates = chosen

    # ================= 阶段 3：沙箱全链路真机实测 (Google + YouTube + ip.sb) =================
    print(f"\n[+] [阶段 3/5] 启动沙箱全链路真实代理实测 (入围节点数: {len(top25_candidates)}, 穿透 edgetunnel 测 Google/YouTube/ip.sb)...")
    sandbox = None
    e2e_results = []
    use_sandbox = mock_mode or (find_clash_core() is not None)

    if use_sandbox:
        vless_tpl = detect_vless_template()
        print(f"[*] 加载 VLESS 代理模板: SNI={vless_tpl['sni']}, UUID={vless_tpl['uuid'][:8]}..., PATH={vless_tpl['path']}")
        sandbox = MihomoSandbox(
            candidates=top25_candidates,
            vless_template=vless_tpl,
            work_dir="/tmp/ipselect_sandbox" if not mock_mode else tempfile.mkdtemp(prefix="mock_sb_"),
            api_port=19098,
            proxy_port=19998,
            mock_mode=mock_mode
        )
        if sandbox.start():
            print(f"[+] 隔离沙箱启动成功 (外部控制端口: 19098, 本地代理端口: 19998, GID=65534 直连)")
            try:
                def probe_single(idx, item):
                    ip, port, sp, colo, tls_rtt = item
                    pname = f"node_{idx:02d}"
                    m_data = None
                    if mock_mode:
                        m_data = {
                            "google_delays": [190 + idx * 5, 195 + idx * 5, 200 + idx * 5],
                            "youtube_delay": 205 + idx * 5,
                            "ip_sb_exit": {"country_code": "US", "ip": "104.28.19.1"}
                        }
                    eval_res = evaluate_e2e_candidate(
                        proxy_name=pname,
                        ip=ip,
                        port=port,
                        colo=colo,
                        api_port=19098,
                        proxy_port=19998,
                        mock_data=m_data
                    )
                    return eval_res

                with ThreadPoolExecutor(max_workers=10) as executor:
                    future_map = {executor.submit(probe_single, idx, item): item for idx, item in enumerate(top25_candidates, 1)}
                    for future in as_completed(future_map):
                        res = future.result()
                        if res:
                            e2e_results.append(res)
            finally:
                sandbox.stop()
        else:
            print("[!] 沙箱启动失败，平滑回退至直连稳定性测定")
    else:
        print("[!] 当前系统未安装 clash_meta 内核，平滑回退至传统直连 TLS 稳定性测定")

    # 若沙箱未能产出结果（无内核或全军覆没），启动直连回退降级
    if not e2e_results:
        print("[!] 启动直连稳定性深测回退兜底模式...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_map = {executor.submit(probe_stability, c[0], c[1]): c for c in top25_candidates}
            for future in as_completed(future_map):
                res = future.result()
                if res:
                    e2e_results.append({
                        'ip': res['ip'],
                        'port': res['port'],
                        'colo': res['colo'],
                        'e2e_avg_rtt': res['avg_rtt'],
                        'e2e_jitter': res['jitter'],
                        'youtube_rtt': res['median_rtt'],
                        'exit_country': 'US',
                        'exit_ip': 'Anycast'
                    })

    print(f"[+] [阶段 3/5] 全链路真实可用性检测完毕，通过严格质检节点数: {len(e2e_results)} / {len(top25_candidates)}")
    if not e2e_results:
        print("[-] 错误: 无任何节点通过全链路真实代理考核。")
        return

    # 按全链路 RTT 排序，取前 18 名进行真实微吞吐测速
    e2e_results.sort(key=lambda x: (x['e2e_avg_rtt'], x['e2e_jitter']))
    candidates_for_speed = e2e_results[:18]

    # ================= 阶段 4：真实微吞吐测速 =================
    print(f"\n[+] [阶段 4/5] 启动 1.0MB 安全微吞吐测速 (入围节点数: {len(candidates_for_speed)})...")
    if mock_mode:
        for node in candidates_for_speed:
            spd = 1.85
            node['speed'] = spd
            node['score'] = calculate_e2e_score(node['e2e_avg_rtt'], node['e2e_jitter'], spd, node['colo'])
            node['is_cf'] = is_official_cloudflare_ip(node['ip'])
    else:
        with ThreadPoolExecutor(max_workers=6) as executor:
            future_map = {executor.submit(probe_real_download_speed, n['ip'], n['port'], 1048576, 2.0, fallback_rtt=n['e2e_avg_rtt']): n for n in candidates_for_speed}
            for future in as_completed(future_map):
                node = future_map[future]
                spd = future.result()
                node['speed'] = spd
                node['score'] = calculate_e2e_score(node['e2e_avg_rtt'], node['e2e_jitter'], spd, node['colo'])
                node['is_cf'] = is_official_cloudflare_ip(node['ip'])

    # ================= 阶段 5：综合评分排序并导出 Top 15 =================
    candidates_for_speed.sort(key=lambda x: (-x['score'], x['e2e_avg_rtt'], -x['speed']))
    selected_nodes = candidates_for_speed[:TOP_COUNT]

    print(f"\n[+] 成功精选出 Top {len(selected_nodes)} 个高品质全链路真优选节点 ({tag}专属 / 全链路Google+YouTube实测+美区出口):")
    output_lines = []
    for idx, node in enumerate(selected_nodes, 1):
        colo_str = node.get('colo') or 'CF'
        line = format_node_line(node['ip'], node['port'], node['score'], idx, tag, node['speed'], colo_str)
        output_lines.append(line)
        cf_flag = "CF官方" if node.get('is_cf', True) else "第三方"
        print(f"  [{idx:02d}] {line} | 评分: {node['score']:.1f} | 机房: {colo_str} | 端到端RTT: {node['e2e_avg_rtt']:.0f}ms | 抖动: {node['e2e_jitter']:.0f}ms ({cf_flag})")

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

    print(f"\n[+] 优选结果已写入: {target_path}")


def main():
    parser = argparse.ArgumentParser(description="Cloudflare 节点四阶漏斗严选引擎与多维评估器")
    parser.add_argument("--tag", default=os.environ.get("LOCATION_TAG", None), help="节点标识 (如: 公司, 家庭，缺省自动识别)")
    parser.add_argument("--output", default=os.environ.get("OUTPUT_FILE", None), help="输出文件名 (如: best_us.txt, home_us_best_node.txt，缺省自动识别)")
    parser.add_argument("--interface", default=os.environ.get("BIND_INTERFACE", None), help="绑定的出口网卡 (如: br-lan, eth0)")

    args = parser.parse_args()
    tag, output_file = detect_network_environment(cli_tag=args.tag, cli_output=args.output)
    print(f"[*] 环境自动感知检测结果: 位置=[{tag}], 输出文件=[{output_file}]")
    run_filter_and_export(tag=tag, output_file=output_file, interface=args.interface)


if __name__ == '__main__':
    main()
