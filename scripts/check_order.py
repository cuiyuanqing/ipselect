import urllib.request
import urllib.parse
import json

sql = """
SELECT e.endpoint_id, s.current_ip, e.port, e.protocol, e.transport, e.status, s.country, e.ui_latency_ms,
       COALESCE(json_extract(e.metadata_json, '$.switch_speed_bps'), 0) as switch_spd
FROM endpoints e 
JOIN servers s ON e.server_key=s.server_key 
WHERE s.country IN ('日本', 'JP', 'Japan')
  AND UPPER(e.status) = 'AVAILABLE'
  AND LOWER(e.protocol)='openvpn'
  AND e.port > 0
ORDER BY e.fail_streak ASC,
         (CASE WHEN e.success_count > 0 THEN 1 ELSE 0 END) DESC,
         COALESCE(json_extract(e.metadata_json, '$.switch_speed_bps'), 0) DESC,
         json_extract(s.metadata_json, '$.speed') DESC,
         e.success_streak DESC,
         e.ui_latency_ms ASC
LIMIT 20
"""
res = json.loads(urllib.request.urlopen("http://jiakuan.19940407.xyz:8585/api/debug_aimili?sql=" + urllib.parse.quote(sql)).read())
print("Top 20 from DB query:")
for r in res.get("rows", []):
    eid, ip, port, proto, trans, st, ctry, lat, spd = r
    print(f"  IP: {ip:<15} Port: {port} Proto: {trans} Spd: {round(spd/1e6, 2):>5} Mbps Lat: {lat}ms")
