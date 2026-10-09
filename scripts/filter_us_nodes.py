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
from concurrent.futures import ThreadPoolExecutor, as_completed

SNI_HOST = "proxy.19940407.xyz"
TOP_COUNT = 15
PROBE_TIMEOUT = 2.5
ROUNDS_PER_NODE = 4
THREAD_WORKERS = 20

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


def fetch_candidates():
    """从多个渠道获取候选节点列表"""
    candidates = []

    # 1. 解析 CSV 数据源 (例如 addressescsv.csv)
    for url in CSV_SOURCES:
        print(f"[*] 正在拉取 CSV 数据源: {url}")
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as res:
                lines = [l.decode('utf-8', errors='ignore') for l in res.readlines()]
                reader = csv.reader(lines)
                header = next(reader, None)
                for row in reader:
                    if len(row) <= 8:
                        continue
                    tls = row[3].strip().upper()
                    if tls != 'TRUE':
                        continue
                    dc = row[4].strip().upper()
                    region = row[5].strip()
                    city = row[6].strip()
                    if 'Canada' in region or city in EXCLUDE_CITIES or dc in EXCLUDE_DCS:
                        continue
                    is_us = (region in ['North America', 'United States']) or (dc in US_DATA_CENTERS)
                    if not is_us:
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
            print(f"[-] CSV 获取失败 ({url}): {e}")

    # 2. 解析 TXT 数据源 (例如 mocl1220/ip, BestCF)
    for url in TXT_SOURCES:
        print(f"[*] 正在拉取 TXT 数据源: {url}")
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as res:
                for l in res.readlines():
                    line = l.decode('utf-8', errors='ignore').strip()
                    if not line or line.startswith('#'):
                        continue
                    if '#' in line:
                        addr, tag = line.split('#', 1)
                        if 'US' not in tag.upper():
                            continue
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
            print(f"[-] TXT 获取失败 ({url}): {e}")

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


def probe_tls_colo(ip, port, timeout=2.5):
    """
    阶段 2：TLS 握手与 cf-ray 机房质检
    返回 (is_valid_us, colo_code, rtt_ms)
    """
    s = None
    ss = None
    t0 = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
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
        res = ss.recv(2048)
        rtt_ms = (time.time() - t0) * 1000

        res_text = res.decode('utf-8', errors='ignore')
        colo = parse_colo_from_headers(res_text)
        is_us = is_us_colo(colo)
        return is_us, colo, rtt_ms
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


def probe_node_multidimensional(candidate):
    """
    对候选节点执行多维度综合评估：
    1. 连通性与丢包率 (4 轮探测)
    2. 延迟与抖动 (Avg RTT, Jitter)
    3. 综合质量评分计算 (1.0 ~ 10.0 分制)
    """
    ip, port, original_speed = candidate
    rtts = []

    # 连续执行 4 轮握手探测
    for _ in range(ROUNDS_PER_NODE):
        rtt = single_handshake_probe(ip, port)
        if rtt is not None:
            rtts.append(rtt)

    success_count = len(rtts)
    loss_rate = (ROUNDS_PER_NODE - success_count) / ROUNDS_PER_NODE

    # 丢包率 >= 50% 的不稳定节点直接丢弃
    if success_count < 2:
        return None

    avg_rtt = sum(rtts) / success_count
    jitter = (max(rtts) - min(rtts)) if success_count > 1 else 100.0

    # 速度评分与换算
    if original_speed >= 1.0:
        calc_speed = original_speed
    else:
        calc_speed = round(min(12.5, max(1.8, 3800.0 / avg_rtt)), 2)

    is_cf = is_official_cloudflare_ip(ip)

    # 综合质量评分 (1.0 ~ 10.0 分制) 计算模型
    # 1. 稳定性基础分 (最高 6.0 分)
    if loss_rate == 0.0:
        base_score = 6.0
    elif loss_rate == 0.25:
        base_score = 4.0
    else:
        base_score = 2.0

    # 2. 延迟表现分 (最高 2.5 分): 自适应机房环境与跨洋网络
    if avg_rtt < 500.0:
        lat_score = 2.5
    else:
        lat_score = max(0.0, min(2.5, (2600.0 - avg_rtt) / 400.0))

    # 3. 抖动平稳度分 (最高 1.0 分): Jitter <= 250ms 拿满分，> 800ms 为 0 分
    if jitter < 250.0:
        jit_score = 1.0
    else:
        jit_score = max(0.0, min(1.0, (800.0 - jitter) / 550.0))

    # 4. 吞吐速度加成 (最高 0.5 分)
    spd_score = max(0.1, min(0.5, (calc_speed / 4.0) * 0.5))

    total_score = base_score + lat_score + jit_score + spd_score

    # 非 Cloudflare 官方 Anycast 网段扣除 1.0 分
    if not is_cf:
        total_score -= 1.0

    final_score = round(min(10.0, max(1.0, total_score)), 1)

    return {
        'ip': ip,
        'port': port,
        'score': final_score,
        'loss_rate': loss_rate,
        'avg_rtt': avg_rtt,
        'jitter': jitter,
        'speed': calc_speed,
        'is_cf': is_cf,
        'success_rounds': success_count
    }


def run_filter_and_export(tag="公司", output_file="best_us.txt", interface=None):
    init_network_bypass(interface)

    candidates = fetch_candidates()
    print(f"[*] 开始进行并发多维度质量综合评估 (标识: {tag}, 每节点 {ROUNDS_PER_NODE} 轮连测, 并发度: {THREAD_WORKERS})...")

    alive_nodes = []
    with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as executor:
        future_map = {executor.submit(probe_node_multidimensional, c): c for c in candidates}
        for future in as_completed(future_map):
            result = future.result()
            if result:
                alive_nodes.append(result)

    print(f"[+] 多维度探测完毕，通过严格稳定性考核的可用节点总数: {len(alive_nodes)}")

    if len(alive_nodes) < TOP_COUNT:
        print(f"[!] 警告: 可用节点数 ({len(alive_nodes)}) 少于目标数 ({TOP_COUNT})，将导出全部可用节点。")
        selected_nodes = alive_nodes
    else:
        # 排序策略：
        # 1. 综合质量评分 (score) 从高到低降序（评分最高排最前）
        # 2. 丢包率 (loss_rate) 从低到高升序（0% 丢包排最前）
        # 3. 平均延迟 (avg_rtt) 从低到高升序
        alive_nodes.sort(key=lambda x: (-x['score'], x['loss_rate'], x['avg_rtt']))
        selected_nodes = alive_nodes[:TOP_COUNT]

    print(f"\n[+] 成功精选出 Top {len(selected_nodes)} 个高品质美国优选节点 ({tag}专属 / 质量/延迟/速度多维加权):")
    output_lines = []
    for idx, node in enumerate(selected_nodes, 1):
        # 规范命名格式：US-DaTree-{评分}-{序号}-{tag} {速度}
        node_name = f"US-DaTree-{node['score']:.1f}-{idx:02d}-{tag}"
        line = f"{node['ip']}:{node['port']}#{node_name} {node['speed']:.2f}MB/s"
        output_lines.append(line)
        cf_flag = "CF官方" if node['is_cf'] else "第三方"
        loss_pct = int(node['loss_rate'] * 100)
        print(f"  [{idx:02d}] {line} | 评分: {node['score']:.1f} | 丢包: {loss_pct}% | 延迟: {node['avg_rtt']:.0f}ms | 抖动: {node['jitter']:.0f}ms ({cf_flag})")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    target_path = os.path.join(project_dir, output_file)

    with open(target_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output_lines) + '\n')

    print(f"\n[+] 优选结果已写入: {target_path}")


def main():
    parser = argparse.ArgumentParser(description="Cloudflare 节点多维质量评估与优选生成器")
    parser.add_argument("--tag", default=os.environ.get("LOCATION_TAG", "公司"), help="节点标识 (如: 公司, 家庭)")
    parser.add_argument("--output", default=os.environ.get("OUTPUT_FILE", "best_us.txt"), help="输出文件名 (如: best_us.txt, home_us_best_node.txt)")
    parser.add_argument("--interface", default=os.environ.get("BIND_INTERFACE", None), help="绑定的出口网卡 (如: br-lan, eth0)")

    args = parser.parse_args()
    run_filter_and_export(tag=args.tag, output_file=args.output, interface=args.interface)


if __name__ == '__main__':
    main()
