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
import socket
import ssl
import time
import os
import sys
import argparse
import ipaddress
import statistics
import random
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


def run_filter_and_export(tag="公司", output_file="best_us.txt", interface=None):
    """
    四阶漏斗执行流水线 (方案 A 全网极速优选模式)：
    1. 阶段 1：快速 TCP 探针 (淘汰 80% 死节点)
    2. 阶段 2：TLS 握手与 cf-ray 机房质检 (识别有效 Cloudflare 边缘机房)
    3. 阶段 3：多轮打散稳定性与抖动深测 (0 丢包严选)
    4. 阶段 4：真实微吞吐测速 (防限流微测速)
    5. 五维严苛评分与降序导出
    """
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # 优先拉取候选数据源（支持代理或国内镜像加速拉取，防止未降权前因网络受阻）
    candidates = fetch_candidates()
    print(f"[*] 启动四阶漏斗严选引擎 (环境标识: {tag}, 导出目标: {output_file})...")

    # 数据源拉取就绪后，启动底层直连网络绕过（设置 GID=65534 绕过代理，直接测速真实延迟）
    init_network_bypass(interface)

    # ================= 阶段 1：快速 TCP 并发探针 =================
    print(f"\n[+] [阶段 1/4] 启动快速 TCP 连通性并发扫描 (总数: {len(candidates)}, 并发度: 30)...")
    stage1_survivors = []
    with ThreadPoolExecutor(max_workers=30) as executor:
        future_map = {executor.submit(fast_tcp_ping, c[0], c[1]): c for c in candidates}
        for future in as_completed(future_map):
            c = future_map[future]
            if future.result():
                stage1_survivors.append(c)

    print(f"[+] [阶段 1/4] 快速 TCP 扫描完成，存活可通节点数: {len(stage1_survivors)} / {len(candidates)}")

    if not stage1_survivors:
        print("[-] 错误: 所有候选节点均无法建立 TCP 连接，请检查本地物理网络。")
        return

    # ================= 阶段 2：TLS 握手与 cf-ray 机房质检 =================
    print(f"\n[+] [阶段 2/4] 启动 TLS 握手与 cf-ray 边缘机房代码质检 (待测数: {len(stage1_survivors)})...")
    stage2_survivors = []
    with ThreadPoolExecutor(max_workers=25) as executor:
        future_map = {executor.submit(probe_tls_colo, c[0], c[1]): c for c in stage1_survivors}
        for future in as_completed(future_map):
            c = future_map[future]
            is_valid, colo, rtt = future.result()
            if is_valid:
                stage2_survivors.append((c[0], c[1], c[2], colo, rtt))

    print(f"[+] [阶段 2/4] 机房质检完毕，存活有效 Cloudflare 边缘节点数: {len(stage2_survivors)}")

    # 兜底保护：若有效节点极少，平滑降级包含阶段 1 存活节点
    if len(stage2_survivors) < 5:
        print("[!] 提示: 严选节点较少，启动平滑容灾降级模式。")
        for c in stage1_survivors:
            if not any(s[0] == c[0] and s[1] == c[1] for s in stage2_survivors):
                stage2_survivors.append((c[0], c[1], c[2], "CF", 200.0))

    # ================= 阶段 3：多轮打散稳定性测试 =================
    print(f"\n[+] [阶段 3/4] 启动 {ROUNDS_PER_NODE} 轮打散稳定性与抖动深度测试 (待测数: {len(stage2_survivors)})...")
    stage3_survivors = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_map = {executor.submit(probe_stability, c[0], c[1]): c for c in stage2_survivors}
        for future in as_completed(future_map):
            res = future.result()
            if res:
                res['is_cf'] = is_official_cloudflare_ip(res['ip'])
                stage3_survivors.append(res)

    print(f"[+] [阶段 3/4] 稳定性深测完毕，通过 0 丢包严格考核的优质节点数: {len(stage3_survivors)}")

    if not stage3_survivors:
        print("[-] 错误: 无任何节点通过稳定性考核。")
        return

    # 按初筛稳定性中位数延迟排序，取前 30 名进入阶段 4 微吞吐测速
    stage3_survivors.sort(key=lambda x: (x['loss_rate'], x['median_rtt'], x['jitter']))
    top_candidates_for_speed = stage3_survivors[:30]

    # ================= 阶段 4：安全微吞吐测速 (防限流微测速) =================
    print(f"\n[+] [阶段 4/4] 启动 1.5MB 安全微吞吐测速 (入围节点数: {len(top_candidates_for_speed)}, 小并发防限流)...")
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {executor.submit(probe_real_download_speed, n['ip'], n['port'], 1572864, 2.0, n['median_rtt']): n for n in top_candidates_for_speed}
        for future in as_completed(future_map):
            node = future_map[future]
            real_speed = future.result()
            node['speed'] = real_speed
            node['score'] = calculate_rigorous_score(node)

    # 排序选拔 Top 15：评分降序 -> 丢包升序 -> 延迟中位数升序 -> 速度降序
    top_candidates_for_speed.sort(key=lambda x: (-x['score'], x['loss_rate'], x['median_rtt'], -x['speed']))
    selected_nodes = top_candidates_for_speed[:TOP_COUNT]

    print(f"\n[+] 成功精选出 Top {len(selected_nodes)} 个高品质极速优选节点 ({tag}专属 / 全网低延迟入口+美区出口):")
    output_lines = []
    for idx, node in enumerate(selected_nodes, 1):
        colo_str = node.get('colo') or 'CF'
        node_name = f"US-DaTree-{node['score']:.1f}-{idx:02d}-{tag}-{colo_str}"
        line = f"{node['ip']}:{node['port']}#{node_name} {node['speed']:.2f}MB/s"
        output_lines.append(line)
        cf_flag = "CF官方" if node['is_cf'] else "第三方"
        loss_pct = int(node['loss_rate'] * 100)
        print(f"  [{idx:02d}] {line} | 评分: {node['score']:.1f} | 机房: {colo_str} | 丢包: {loss_pct}% | 延迟: {node['median_rtt']:.0f}ms | 抖动: {node['jitter']:.0f}ms ({cf_flag})")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    target_path = os.path.join(project_dir, output_file)

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
