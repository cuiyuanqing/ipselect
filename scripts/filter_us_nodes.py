#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动从测速 CSV 数据源筛选最快 15 个美国 (US) TLS 节点，并输出为规范优选格式
"""

import csv
import urllib.request
import os

CSV_URLS = [
    "https://raw.githubusercontent.com/cmliu/WorkerVless2sub/main/addressescsv.csv",
]

US_DATA_CENTERS = {
    'SJC', 'LAX', 'SFO', 'SEA', 'DFW', 'ORD', 'IAD',
    'ATL', 'MIA', 'EWR', 'JFK', 'PHX', 'DEN', 'IAH',
    'BOS', 'PDX', 'MSP', 'DTW', 'CLT', 'LAS', 'SLC'
}

EXCLUDE_CITIES = {'Toronto', 'Montreal', 'Vancouver', 'Calgary', 'Ottawa'}
EXCLUDE_DCS = {'YYZ', 'YVR', 'YUL', 'YYC'}

OUTPUT_FILE = "best_us.txt"
TOP_COUNT = 15

def fetch_and_parse():
    nodes = []
    
    for url in CSV_URLS:
        print(f"正在从数据源获取测速数据: {url}")
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20) as res:
                lines = [l.decode('utf-8', errors='ignore') for l in res.readlines()]
        except Exception as e:
            print(f"获取失败: {e}")
            continue

        if not lines:
            continue

        reader = csv.reader(lines)
        try:
            header = next(reader)
        except StopIteration:
            continue

        # 列定义参考: IP地址,端口,回源端口,TLS,数据中心,地区,城市,TCP延迟(ms),速度(MB/s)
        tls_idx = 3
        dc_idx = 4
        region_idx = 5
        city_idx = 6
        speed_idx = 8

        for row in reader:
            if len(row) <= speed_idx:
                continue

            tls = row[tls_idx].strip().upper()
            if tls != 'TRUE':
                continue

            dc = row[dc_idx].strip().upper()
            region = row[region_idx].strip()
            city = row[city_idx].strip()

            # 排除非美国 (如加拿大北美节点)
            if 'Canada' in region or city in EXCLUDE_CITIES or dc in EXCLUDE_DCS:
                continue

            is_us = (region in ['North America', 'United States']) or (dc in US_DATA_CENTERS)
            if not is_us:
                continue

            try:
                speed = float(row[speed_idx].strip())
            except ValueError:
                speed = 0.0

            ip = row[0].strip()
            port = row[1].strip()

            nodes.append({
                'ip': ip,
                'port': port,
                'dc': dc,
                'speed': speed
            })

    # 按 IP:端口 去重，保留速度最高的记录
    dedup = {}
    for n in nodes:
        key = f"{n['ip']}:{n['port']}"
        if key not in dedup or n['speed'] > dedup[key]['speed']:
            dedup[key] = n

    unique_nodes = list(dedup.values())
    unique_nodes.sort(key=lambda x: x['speed'], reverse=True)

    top_nodes = unique_nodes[:TOP_COUNT]
    print(f"成功筛选出最快 {len(top_nodes)} 个美国 TLS 节点:")

    output_lines = []
    for idx, node in enumerate(top_nodes, 1):
        line = f"{node['ip']}:{node['port']}#US-DaTree-{idx:02d} {node['speed']:.2f}MB/s"
        output_lines.append(line)
        print(f" [{idx:02d}] {line}")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    target_path = os.path.join(project_dir, OUTPUT_FILE)

    with open(target_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output_lines) + '\n')

    print(f"\n已成功写入文件: {target_path}")

if __name__ == '__main__':
    fetch_and_parse()
