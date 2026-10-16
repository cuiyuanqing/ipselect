#!/usr/bin/env python3
"""部署 ipselect 改动到 10.10.18.2"""
import paramiko
import os

HOST = "10.10.18.2"
USER = "root"
PASS = "Xg@2020+"

FILES = [
    (r"D:\nice_wall\ipselect\scripts\filter_us_nodes.py", "/root/ipselect/scripts/filter_us_nodes.py"),
    (r"D:\nice_wall\ipselect\luci-app-ipselect\root\usr\bin\ipselect-runner", "/usr/bin/ipselect-runner"),
    (r"D:\nice_wall\ipselect\luci-app-ipselect\root\etc\config\ipselect", "/etc/config/ipselect"),
]

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[*] 连接 {HOST}...")
    ssh.connect(HOST, username=USER, password=PASS, timeout=15)
    print("[+] SSH 连接成功")

    sftp = ssh.open_sftp()

    # 备份原文件
    print("[*] 备份原文件...")
    for _, remote in FILES:
        backup = remote + ".bak.$(date +%s)"
        ssh.exec_command(f"cp {remote} {backup} 2>/dev/null")

    # 上传文件
    print("[*] 上传文件...")
    for local, remote in FILES:
        sftp.put(local, remote)
        size = os.path.getsize(local)
        print(f"  ✅ {remote} ({size} bytes)")

    sftp.close()

    # 设置执行权限
    print("[*] 设置执行权限...")
    ssh.exec_command("chmod +x /usr/bin/ipselect-runner")
    ssh.exec_command("chmod +x /root/ipselect/scripts/filter_us_nodes.py")

    # 验证配置
    print("\n[*] 验证 UCI 配置:")
    stdin, stdout, stderr = ssh.exec_command("uci show ipselect | grep -E 'vless_|stage|shard|cf_block'")
    print(stdout.read().decode())

    # 验证脚本能正常导入
    print("[*] 验证脚本导入:")
    test_cmd = """cd /root/ipselect && python3 -c "
import sys
sys.path.insert(0, 'scripts')
import filter_us_nodes as f
print('SNI =', f.SNI_HOST)
print('STAGE1_TCP_WORKERS =', f.STAGE1_TCP_WORKERS)
print('STAGE2_COLO_WORKERS =', f.STAGE2_COLO_WORKERS)
print('SHARD_ROTATION_DAYS =', f.SHARD_ROTATION_DAYS)
print('CF_BLOCK_THRESHOLD =', f.CF_BLOCK_THRESHOLD)
result = f.probe_tls_colo('1.1.1.1', 443, timeout=5.0)
print('probe test =>', result)
" 2>&1"""
    stdin, stdout, stderr = ssh.exec_command(test_cmd, timeout=30)
    print(stdout.read().decode())

    ssh.close()
    print("[+] 部署完成！")

if __name__ == "__main__":
    main()
