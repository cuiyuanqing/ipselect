import urllib.request
import json

# Let's test connecting to one JP node from configs
target_id = "JP_115.162.122.158_1538_tcp"
url = "http://jiakuan.19940407.xyz:8585/api/debug_aimili?path=" + urllib.parse.quote("/api/connect")
# Wait, /api/debug_aimili is GET. Can we test via /api/debug_aimili or direct python?
# In /api/debug_aimili, we can query state to see current state
url_st = "http://jiakuan.19940407.xyz:8585/api/status"
res_st = json.loads(urllib.request.urlopen(url_st).read())
print("Picker engine running:", res_st.get("running"))
print("Current step:", res_st.get("current_step"))
