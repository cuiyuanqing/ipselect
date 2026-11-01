import urllib.request, urllib.parse, json

def query_sql(sql):
    url = 'http://jiakuan.19940407.xyz:8585/api/debug_aimili?sql=' + urllib.parse.quote(sql)
    return json.loads(urllib.request.urlopen(url, timeout=10).read())

# 查询日本地区不同协议的 endpoint 数量
stats = query_sql("SELECT e.protocol, e.status, count(*) FROM endpoints e JOIN servers s ON e.server_key=s.server_key WHERE s.country='日本' GROUP BY e.protocol, e.status")
print("JP Protocol & Status stats:")
for r in stats.get('rows', []):
    print(r)

# 查询日本可用的 OpenVPN 节点
jp_openvpn = query_sql("SELECT e.endpoint_id, e.protocol, e.transport, e.port, e.status, s.current_ip FROM endpoints e JOIN servers s ON e.server_key=s.server_key WHERE s.country='日本' AND UPPER(e.status)='AVAILABLE' AND e.protocol='openvpn' LIMIT 15")
print(f"JP Available OpenVPN count: {len(jp_openvpn.get('rows', []))}")
for r in jp_openvpn.get('rows', []):
    print(r)
