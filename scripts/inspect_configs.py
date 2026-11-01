import urllib.request
import json

url = 'http://jiakuan.19940407.xyz:8585/api/debug_aimili?dir=/opt/aimilivpn/vpngate_data/configs'
res = json.loads(urllib.request.urlopen(url).read())
entries = res.get('entries', [])

jp_configs = [e for e in entries if e.startswith('JP_')]
ovpn_configs = [e for e in entries if e.endswith('.ovpn')]
print(f"Total .ovpn: {len(ovpn_configs)}, JP_ prefix: {len(jp_configs)}")
print("Sample 15 JP configs:", jp_configs[:15])
