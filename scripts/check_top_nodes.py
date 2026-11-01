import urllib.request
import urllib.parse
import json

sql = """
SELECT s.current_ip, e.port, e.transport, e.success_count, e.fail_streak, json_extract(e.metadata_json, '$.switch_speed_bps') as last_speed 
FROM endpoints e 
JOIN servers s ON e.server_key = s.server_key 
WHERE s.country IN ('日本', 'JP') AND LOWER(e.protocol)='openvpn' AND e.fail_streak = 0
ORDER BY COALESCE(json_extract(e.metadata_json, '$.switch_speed_bps'), 0) DESC, e.success_count DESC
LIMIT 30
"""
res = json.loads(urllib.request.urlopen("http://jiakuan.19940407.xyz:8585/api/debug_aimili?sql=" + urllib.parse.quote(sql)).read())

# Check against configs
res_configs = json.loads(urllib.request.urlopen("http://jiakuan.19940407.xyz:8585/api/debug_aimili?dir=/opt/aimilivpn/vpngate_data/configs").read())
configs_set = set(res_configs.get("entries", []))

print(f"Top 30 nodes with fail_streak=0:")
matched_count = 0
for row in res.get("rows", []):
    ip, port, proto, sc, fs, sp = row
    speed_mbps = round((sp or 0) / 1e6, 2)
    # Check possible config names
    cand_names = [f"JP_{ip}_{port}_{proto}.ovpn", f"{ip}_{port}_{proto}.ovpn", f"日本_{ip}_{port}_{proto}.ovpn"]
    matched = [c for c in cand_names if c in configs_set]
    if matched:
        matched_count += 1
        print(f"  [OK] IP: {ip:<15} Port: {port} Proto: {proto} Speed: {speed_mbps:>5} Mbps (success={sc}) -> Config: {matched[0]}")
    else:
        print(f"  [NO CONFIG] IP: {ip:<15} Port: {port} Proto: {proto} Speed: {speed_mbps:>5} Mbps")
print(f"Total matched configs in Top 30: {matched_count}/30")
