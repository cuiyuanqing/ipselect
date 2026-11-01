import urllib.request, json

def test_proxy(proxy_str):
    url = 'http://jiakuan.19940407.xyz:8585/api/debug_aimili?proxy_test=' + proxy_str
    print(f"Testing {proxy_str}...")

# 看看 debug_aimili 能否帮我们测试 127.0.0.1:8500
