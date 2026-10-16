#!/usr/bin/env python3
"""远程跑完整优选流程"""
import paramiko
import os
import time

HOST = "10.10.18.2"
USER = "root"
PASS = "Xg@2020+"

LOCAL_FILTER = r"D:\nice_wall\ipselect\scripts\filter_us_nodes.py"
REMOTE_FILTER = "/root/ipselect/scripts/filter_us_nodes.py"

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[*] 连接 {HOST}...")
    ssh.connect(HOST, username=USER, password=PASS, timeout=15)
    print("[+] SSH 连接成功")

    # 上传新代码
    sftp = ssh.open_sftp()
    sftp.put(LOCAL_FILTER, REMOTE_FILTER)
    print(f"[+] 代码已上传: {os.path.getsize(LOCAL_FILTER)} bytes")
    sftp.close()

    # 更新 UCI
    for cmd in [
        "uci set ipselect.config.vless_sni='wall.19940404.xyz'",
        "uci set ipselect.config.vless_uuid='dd151c1d-1db8-8172-0213-b624cbaa39fd'",
        "uci set ipselect.config.vless_path='/'",
        "uci set ipselect.config.vless_port='443'",
        "uci commit ipselect",
    ]:
        stdin, stdout, stderr = ssh.exec_command(cmd)
        stdout.read()
    print("[+] UCI 配置已更新: wall.19940404.xyz / dd151c1d...")

    # 先杀掉旧进程
    print("[*] 清理旧进程...")
    ssh.exec_command("pkill -f filter_us_nodes.py 2>/dev/null; sleep 1")

    # 探针冒烟测试
    print("\n[*] 探针冒烟测试...")
    test_cmd = """cd /root/ipselect && python3 -c "
import sys
sys.path.insert(0, 'scripts')
import filter_us_nodes as f
print('SNI =', f.SNI_HOST)
result = f.probe_tls_colo('1.1.1.1', 443, timeout=5.0)
print('probe 1.1.1.1:443 =>', result)
" 2>&1"""
    stdin, stdout, stderr = ssh.exec_command(test_cmd, timeout=30)
    print(stdout.read().decode())

    # 跑完整流程
    print("\n[*] 启动完整优选流程...")
    run_cmd = (
        "cd /root/ipselect && "
        "VLESS_SNI=wall.19940404.xyz "
        "VLESS_UUID=dd151c1d-1db8-8172-0213-b624cbaa39fd "
        "VLESS_PATH=/ "
        "VLESS_PORT=443 "
        "python3 -u scripts/filter_us_nodes.py "
        "--tag 公司 --output best_us.txt --sandbox 1 --reuse-history 1 "
        "> /tmp/ipselect_run2.log 2>&1 & echo PID=$!"
    )
    stdin, stdout, stderr = ssh.exec_command(run_cmd)
    pid_line = stdout.read().decode().strip()
    print(f"[+] {pid_line}")
    pid = pid_line.split("=")[-1]

    # 轮询日志
    last_size = 0
    for i in range(60):  # 最多 60 * 15 = 900 秒
        time.sleep(15)

        stdin, stdout, stderr = ssh.exec_command(f"ps -p {pid} > /dev/null && echo RUNNING || echo DONE")
        status = stdout.read().decode().strip()

        stdin, stdout, stderr = ssh.exec_command(f"wc -c < /tmp/ipselect_run2.log 2>/dev/null")
        size = int(stdout.read().decode().strip() or 0)

        if size > last_size:
            stdin, stdout, stderr = ssh.exec_command(f"tail -n 10 /tmp/ipselect_run2.log 2>/dev/null")
            new_log = stdout.read().decode()
            print(f"\n--- [{i+1}] 状态:{status} ---")
            print(new_log)
            last_size = size

        if status == "DONE":
            print("\n[+] 流程结束！")
            break

    # 最终结果
    print("\n" + "="*60)
    print("[*] best_us.txt 内容:")
    stdin, stdout, stderr = ssh.exec_command("cat /root/ipselect/best_us.txt 2>/dev/null")
    print(stdout.read().decode())

    ssh.close()
    print("[+] 完成")

if __name__ == "__main__":
    main()
