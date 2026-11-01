import socket
import time
import urllib.parse

def test_socks5(host="127.0.0.1", port=8500, target_url="http://cachefly.cachefly.net/1mb.test", auth=None):
    parsed = urllib.parse.urlparse(target_url)
    target_host = parsed.hostname
    target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target_path = parsed.path or "/"

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect((host, port))

    # SOCKS5 认证协商
    if auth:
        s.sendall(b"\x05\x02\x00\x02") # 支持无认证和用户名密码
    else:
        s.sendall(b"\x05\x01\x00") # 仅无认证
    
    resp = s.recv(2)
    print("SOCKS5 auth response:", resp.hex())
    if len(resp) < 2 or resp[0] != 5:
        s.close()
        return False, "Not socks5"

    if resp[1] == 2: # 用户名密码认证
        u, p = auth
        auth_payload = bytes([1, len(u)]) + u.encode() + bytes([len(p)]) + p.encode()
        s.sendall(auth_payload)
        auth_resp = s.recv(2)
        print("SOCKS5 user/pass auth response:", auth_resp.hex())
        if auth_resp[1] != 0:
            s.close()
            return False, "Auth failed"
    elif resp[1] != 0:
        s.close()
        return False, f"Unsupported auth method {resp[1]}"

    # 发起 CONNECT
    req = b"\x05\x01\x00\x03" + bytes([len(target_host)]) + target_host.encode() + target_port.to_bytes(2, "big")
    s.sendall(req)
    conn_resp = s.recv(10)
    print("SOCKS5 connect response:", conn_resp[:4].hex())
    if len(conn_resp) < 4 or conn_resp[1] != 0:
        s.close()
        return False, f"Connect error: {conn_resp[1] if len(conn_resp)>1 else 'empty'}"

    # 发送 HTTP GET
    http_req = f"GET {target_path} HTTP/1.1\r\nHost: {target_host}\r\nUser-Agent: SpeedTest/1.0\r\nConnection: close\r\n\r\n".encode()
    s.sendall(http_req)

    start = time.time()
    total_bytes = 0
    while True:
        chunk = s.recv(16384)
        if not chunk:
            break
        total_bytes += len(chunk)
        if time.time() - start > 3.0:
            break
    duration = time.time() - start
    s.close()
    speed_mbps = (total_bytes * 8) / (duration * 1e6)
    return True, f"Received {total_bytes} bytes in {duration:.2f}s ({speed_mbps:.2f} Mbps)"

if __name__ == "__main__":
    # 无认证测试
    ok, res = test_socks5(auth=None)
    print("No auth test:", ok, res)
    # 带认证测试
    ok, res = test_socks5(auth=("socks5", "ilovestudy"))
    print("With auth test:", ok, res)
