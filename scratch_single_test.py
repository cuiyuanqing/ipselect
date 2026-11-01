import urllib.request, urllib.parse, json, time

VPS_URL = "http://jiakuan.19940407.xyz:8585"

def call_debug(path=None, sql=None, file=None):
    if path:
        url = f"{VPS_URL}/api/debug_aimili?path=" + urllib.parse.quote(path)
    elif sql:
        url = f"{VPS_URL}/api/debug_aimili?sql=" + urllib.parse.quote(sql)
    elif file:
        url = f"{VPS_URL}/api/debug_aimili?file=" + urllib.parse.quote(file)
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

# 1. 查一个 AVAILABLE 的 OpenVPN 节点
res = call_debug(sql="SELECT e.endpoint_id, s.current_ip, e.port, e.protocol FROM endpoints e JOIN servers s ON e.server_key=s.server_key WHERE s.country='日本' AND UPPER(e.status)='AVAILABLE' AND e.protocol='openvpn' LIMIT 1")
node_info = res.get('rows')[0]
eid, ip, port, proto = node_info
print(f"Selected node: {ip}:{port} ({proto}), eid={eid}")

# 2. 读取当前 state.json
st = json.loads(call_debug(file="/opt/aimilivpn/vpngate_data/state.json").get('content'))
print(f"Before switch: active_id={st.get('active_openvpn_node_id')}, endpoint={st.get('active_connected_endpoint')}")
