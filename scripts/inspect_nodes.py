import urllib.request
import urllib.parse
import json
import re

url = 'http://jiakuan.19940407.xyz:8585/api/debug_aimili?file=/opt/aimilivpn/vpngate_data/nodes.json'
res = json.loads(urllib.request.urlopen(url).read())
content = res.get('content', '')
print("Read bytes from nodes.json:", len(content))

ids = re.findall(r'"id"\s*:\s*"([^"]+)"', content)
print("Found IDs in first 50KB:", len(ids))

url_nodes = 'http://jiakuan.19940407.xyz:8585/api/debug_aimili?path=' + urllib.parse.quote('/api/nodes?limit=100')
try:
    res_nodes = json.loads(urllib.request.urlopen(url_nodes).read())
    raw_nodes = res_nodes.get('raw_response', {}).get('nodes', [])
    print("Nodes from /api/nodes?limit=100:", len(raw_nodes))
    if raw_nodes:
        print("First 3 node IDs from /api/nodes:", [n.get('id') for n in raw_nodes[:3]])
except Exception as e:
    print("Error calling /api/nodes:", e)
