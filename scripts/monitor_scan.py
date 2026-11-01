import json
import time
import urllib.request
import sys

VPS_URL = "http://jiakuan.19940407.xyz:8585"

def trigger_scan():
    req_body = json.dumps({
        "country": "JP",
        "max_risk": 7,
        "min_speed_mbps": 5.0,
        "top_n": 10,
        "inherit_existing": False,
        "source_mode": "auto",
        "max_rounds": 3
    }).encode("utf-8")
    
    req = urllib.request.Request(
        f"{VPS_URL}/api/scan",
        data=req_body,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        print("Trigger scan result:", res)

def monitor():
    last_log_count = 0
    print("Starting monitoring...")
    while True:
        try:
            with urllib.request.urlopen(f"{VPS_URL}/api/status", timeout=5) as resp:
                status = json.loads(resp.read().decode("utf-8"))
            
            with urllib.request.urlopen(f"{VPS_URL}/api/logs", timeout=5) as resp:
                logs = json.loads(resp.read().decode("utf-8"))
            
            if len(logs) > last_log_count:
                for line in logs[last_log_count:]:
                    print(line)
                last_log_count = len(logs)
                
            running = status.get("running", False)
            current_step = status.get("current_step", "")
            
            if not running and last_log_count > 5:
                print("\n================== SCAN COMPLETE ==================")
                last_result = status.get("last_result", {})
                picked = last_result.get("nodes", []) or last_result.get("picked_nodes", [])
                print(f"Total picked nodes: {len(picked)}")
                for idx, n in enumerate(picked, 1):
                    print(f"[{idx}] IP: {n.get('ip')} | Speed: {n.get('speed_mbps')} Mbps | Risk: {n.get('risk_score')}% ({n.get('risk_label')}) | Latency: {n.get('latency_ms')} ms (±{n.get('jitter_ms')}ms) | Type: {n.get('ip_type')}")
                break
                
            time.sleep(2.0)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(2.0)

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    trigger_scan()
    monitor()
