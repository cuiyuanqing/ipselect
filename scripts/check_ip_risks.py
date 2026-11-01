import urllib.request
import urllib.parse
import json

ips = [
    "116.82.127.94",
    "123.1.104.5",
    "221.245.60.19",
    "124.32.30.64",
    "119.83.242.105",
    "60.75.38.101",
    "60.61.28.14",
    "153.180.162.195",
    "133.218.57.54",
    "110.67.127.117",
    "14.101.10.239",
    "119.171.255.242",
    "14.101.50.181",
    "121.118.241.33",
    "59.158.19.186",
    "124.18.70.251",
    "61.21.196.8"
]

print("Checking IP risk for top 17 reliable nodes:")
for ip in ips:
    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,message,country,isp,org,as,hosting,proxy"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        is_hosting = data.get("hosting", False)
        is_proxy = data.get("proxy", False)
        isp = data.get("isp", "")
        risk = 85 if (is_hosting or is_proxy) else 3
        print(f"  IP: {ip:<15} | ISP: {isp:<30} | Hosting: {is_hosting} | Risk: {risk}%")
    except Exception as e:
        print(f"  IP: {ip:<15} | Error: {e}")
