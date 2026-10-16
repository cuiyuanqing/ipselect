#!/usr/bin/env python3
"""远程测试脚本"""
import sys
sys.path.insert(0, '/root/ipselect/scripts')
import filter_us_nodes as f

print('SNI_HOST =', f.SNI_HOST)
print('STAGE1_TCP_WORKERS =', f.STAGE1_TCP_WORKERS)
print('STAGE2_COLO_WORKERS =', f.STAGE2_COLO_WORKERS)

print('\n测试 fast_tcp_ping...')
candidates = [('1.1.1.1', 443), ('8.8.8.8', 443)]
for ip, port in candidates:
    try:
        result = f.fast_tcp_ping(ip, port)
        print(f'  {ip}:{port} => {result}')
    except Exception as e:
        print(f'  {ip}:{port} => ERROR: {e}')

print('\n测试 probe_tls_colo...')
for ip, port in candidates:
    try:
        result = f.probe_tls_colo(ip, port, timeout=5.0)
        print(f'  {ip}:{port} => {result}')
    except Exception as e:
        print(f'  {ip}:{port} => ERROR: {e}')

print('\n所有测试完成')
