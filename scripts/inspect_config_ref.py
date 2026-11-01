import urllib.request
import urllib.parse
import json

# Let's inspect nodes.json on VPS using sqlite or direct script
# Since we have /api/debug_aimili?sql=... we can check how many in sqlite have config_ref
sql = """
SELECT count(*) 
FROM endpoints e 
JOIN servers s ON e.server_key = s.server_key 
WHERE s.country IN ('日本', 'JP') AND LOWER(e.protocol) = 'openvpn' AND e.config_ref IS NOT NULL
"""
res = json.loads(urllib.request.urlopen("http://jiakuan.19940407.xyz:8585/api/debug_aimili?sql=" + urllib.parse.quote(sql)).read())
print("Endpoints with config_ref:", res)

# Check sample config_ref values
sql2 = """
SELECT e.endpoint_id, s.current_ip, e.port, e.transport, e.config_ref 
FROM endpoints e 
JOIN servers s ON e.server_key = s.server_key 
WHERE s.country IN ('日本', 'JP') AND LOWER(e.protocol) = 'openvpn' AND e.config_ref IS NOT NULL 
LIMIT 10
"""
res2 = json.loads(urllib.request.urlopen("http://jiakuan.19940407.xyz:8585/api/debug_aimili?sql=" + urllib.parse.quote(sql2)).read())
print("Sample config_refs:", json.dumps(res2, indent=2, ensure_ascii=False))
