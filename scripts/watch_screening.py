import urllib.request
import json
import time
import sys

last_len = 0
while True:
    try:
        req = urllib.request.urlopen("http://jiakuan.19940407.xyz:8585/api/logs", timeout=5)
        logs = json.loads(req.read().decode("utf-8"))
        if len(logs) > last_len:
            for l in logs[last_len:]:
                # Print important logs
                if any(k in l for k in ["实测开始", "下发节点", "握手成功", "测速成功", "实测达标", "新晋入选", "入选", "满员", "SCAN COMPLETE", "永久拉黑"]):
                    print(l, flush=True)
            last_len = len(logs)
        
        req_st = urllib.request.urlopen("http://jiakuan.19940407.xyz:8585/api/status", timeout=5)
        st = json.loads(req_st.read().decode("utf-8"))
        if not st.get("running"):
            print("Scan finished or idle!", flush=True)
            break
        time.sleep(2.0)
    except Exception as e:
        time.sleep(2.0)
