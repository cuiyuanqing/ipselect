#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VPS 一键部署与热更新脚本
=======================
功能：
1. 检查并推送本地 Git 提交到远程 GitHub 仓库；
2. 触发 VPS (jiakuan.19940407.xyz:8585) 上的热更新接口 (/api/deploy/sync_and_restart)；
3. VPS 自动执行 git fetch --all && git reset --hard origin/main，并执行 sudo systemctl restart aimili-picker；
4. 轮询健康检查，确保 VPS 上的服务正常重启并恢复在线。
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

VPS_URL = "http://jiakuan.19940407.xyz:8585"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def run_cmd(cmd, cwd=REPO_DIR):
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return res.returncode == 0, res.stdout.strip(), res.stderr.strip()

def main():
    print("=" * 60)
    print("🚀 开始执行 VPS 一键部署与热更新流程")
    print(f"📁 本地仓库: {REPO_DIR}")
    print(f"🌐 目标 VPS: {VPS_URL}")
    print("=" * 60)

    # 1. 检查本地是否有未提交的修改
    ok, out, _ = run_cmd(["git", "status", "--porcelain"])
    if out:
        msg = sys.argv[1] if len(sys.argv) > 1 else "feat: update aimili picker"
        print(f"📦 检测到未提交的改动，正在自动提交 (commit: {msg})...")
        run_cmd(["git", "add", "-A"])
        ok_c, _, err_c = run_cmd(["git", "commit", "-m", msg])
        if not ok_c:
            print(f"⚠️ Git commit 提示: {err_c}")

    # 2. 推送到远端
    print("📤 正在推送到远端 GitHub (git push origin main)...")
    ok_p, out_p, err_p = run_cmd(["git", "push", "origin", "main"])
    if not ok_p:
        print(f"❌ Git push 失败:\n{err_p}")
        sys.exit(1)
    print("✅ 成功推送到 GitHub 远端仓库！")

    # 3. 触发 VPS 远程更新并重启
    deploy_api = f"{VPS_URL}/api/deploy/sync_and_restart"
    print(f"\n📡 正在调用 VPS 部署接口: {deploy_api} ...")
    try:
        req = urllib.request.Request(deploy_api, data=b"{}", headers={"User-Agent": "DeployScript/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"✅ VPS 响应: {json.dumps(data, ensure_ascii=False, indent=2)}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"⚠️ VPS 接口返回 HTTP {e.code}: {body}")
        if e.code == 404:
            print("💡 提示: VPS 当前运行的版本尚未加载该新接口，请先在 VPS 上手动执行一次拉取并重启:")
            print("   cd /home/chuiyuanwo/ipselect && git fetch --all && git reset --hard origin/main && sudo systemctl restart aimili-picker")
            return
    except Exception as e:
        print(f"⚠️ 无法直连 VPS 接口 ({e})，若服务刚重启属正常现象。")

    # 4. 轮询健康检查
    print("\n⏳ 正在轮询检查 VPS 服务重启状态...")
    status_api = f"{VPS_URL}/api/status"
    recovered = False
    for i in range(1, 15):
        time.sleep(2)
        try:
            req = urllib.request.Request(status_api, headers={"User-Agent": "DeployScript/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                print(f"✅ [{i}/15] VPS 服务已恢复在线！当前运行状态: running={data.get('running')}")
                recovered = True
                break
        except Exception:
            print(f"   [{i}/15] 服务重启中，继续等待...")

    if recovered:
        print("\n🎉 全部流程执行成功！VPS 已成功同步最新代码并正常运行。")
    else:
        print("\n⚠️ VPS 重启时间较长，请稍后手动访问控制台检查: " + VPS_URL)

if __name__ == "__main__":
    main()
