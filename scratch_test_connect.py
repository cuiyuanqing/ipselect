import urllib.request, urllib.parse, json, time

def call_aimili(path, data=None):
    url = f"http://127.0.0.1:8501/vuYkz89zKZtf{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8") if data else None,
        headers={"Content-Type": "application/json", "User-Agent": "Test/1.0"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

# 1. 登录
login_resp = call_aimili("/api/login", {"username": "admin", "password": ""})
print("Login:", login_resp)

# 2. 获取当前状态
st = call_aimili("/api/state")
print("Current active_openvpn_node_id:", st.get("active_openvpn_node_id"))
print("Current active_connected_endpoint:", st.get("active_connected_endpoint"))
print("Current proxy_ip:", st.get("proxy_ip"))
print("Current client_proxy_ok:", st.get("client_proxy_ok"))
print("Current tunnel_quality:", st.get("tunnel_quality"))
print("Current main_speed_bps:", st.get("main_speed_bps"))
