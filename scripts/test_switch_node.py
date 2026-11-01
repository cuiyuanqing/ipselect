import urllib.request
import json
import sys

# 设置 stdout 为 utf-8 避免 Windows GBK 报错
sys.stdout.reconfigure(encoding='utf-8')

base_url = 'http://jiakuan.19940407.xyz:8585'

# 1. 获取当前节点列表中的第一个非当前节点
req_status = urllib.request.Request(f'{base_url}/api/status')
with urllib.request.urlopen(req_status, timeout=10) as resp:
    status_data = json.loads(resp.read().decode('utf-8'))

nodes = (status_data.get('last_result') or {}).get('nodes', [])
cur_active_ip = status_data.get('last_result', {}).get('active_node_ip')
print(f"当前活动 IP: {cur_active_ip}")

target_node = None
for n in nodes:
    if n.get('ip') != cur_active_ip:
        target_node = n
        break

if not target_node:
    print("未找到备选节点进行测试切换")
    sys.exit(0)

print(f"准备切换到目标测试节点: IP={target_node.get('ip')}, ID={target_node.get('id')}")

payload = json.dumps({
    "ip": target_node.get('ip'),
    "id": target_node.get('id')
}).encode('utf-8')

req_switch = urllib.request.Request(
    f'{base_url}/api/switch_node',
    data=payload,
    headers={'Content-Type': 'application/json'}
)

try:
    with urllib.request.urlopen(req_switch, timeout=20) as resp:
        res = json.loads(resp.read().decode('utf-8'))
        print("切换接口调用响应:")
        print(json.dumps(res, ensure_ascii=False, indent=2))
except Exception as e:
    print(f"切换接口调用异常: {e}")

# 再次检查 Aimili 状态
req_aimili = urllib.request.Request(f'{base_url}/api/aimili_status')
with urllib.request.urlopen(req_aimili, timeout=10) as resp:
    aimili_status = json.loads(resp.read().decode('utf-8'))
print("\n切换后 Aimili 实时状态:")
print(f"  active_ip: {aimili_status.get('active_ip')}")
print(f"  active_id: {aimili_status.get('active_id')}")
print(f"  status_label: {aimili_status.get('status_label')}")
print(f"  message: {aimili_status.get('message')}")
