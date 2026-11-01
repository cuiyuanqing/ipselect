import urllib.request
import json

base_url = 'http://jiakuan.19940407.xyz:8585'

# 1. 验证 HTML
req = urllib.request.Request(f'{base_url}/', headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=10) as resp:
    html = resp.read().decode('utf-8')

print('--- HTML 验证 ---')
print('看板存在:', 'Aimili VPN 真实连接出口看板' in html)
print('设为出口按钮存在:', '🚀 设为出口' in html)
print('当前连接标签存在:', '当前连接中' in html)
print('switchAimiliNode 函数存在:', 'switchAimiliNode' in html)
print('可选链已清除:', '?.checked' not in html and '?.nodes' not in html)
print('HTML 长度:', len(html))

# 检查节点行
has_nodes = 'tag-speed' in html
print('HTML 服务端预渲染节点表格存在:', has_nodes)

# 2. 验证 /api/aimili_status
req_aimili = urllib.request.Request(f'{base_url}/api/aimili_status')
with urllib.request.urlopen(req_aimili, timeout=10) as resp:
    aimili_status = json.loads(resp.read().decode('utf-8'))
print('\n--- Aimili 真实状态验证 ---')
print(json.dumps(aimili_status, ensure_ascii=False, indent=2))

# 3. 验证 /api/status
req_status = urllib.request.Request(f'{base_url}/api/status')
with urllib.request.urlopen(req_status, timeout=10) as resp:
    status_data = json.loads(resp.read().decode('utf-8'))
print('\n--- 服务状态验证 ---')
print('running:', status_data.get('running'))
nodes = (status_data.get('last_result') or {}).get('nodes', [])
print('当前节点池数量:', len(nodes))
for i, n in enumerate(nodes[:10]):
    print(f"  [{i+1}] IP: {n.get('ip')}, 纯净度: {n.get('risk_score')}%, 速率: {n.get('speed_mbps')} Mbps, 评级: {n.get('experience_level')}")
