#!/usr/bin/env python3
"""
tests/verify_router_live.py
端到端真机实测与软路由现场全链路联调验证脚本

连接到 iStoreOS / OpenWrt 软路由实机 (10.10.18.2)，执行 8 大核心维度的全链路真实验证：
1. 现场连通性与底层系统环境探测 (SSH / ubus / uhttpd / OS)
2. 7 大核心组件文件部署与 POSIX 权限校验 (/etc/config, /etc/init.d, /usr/bin, menu.d, acl.d, controller, view)
3. UCI 完整配置项可读性与结构校验 (uci show ipselect)
4. Crontab 定时任务托管规则与 init.d 幂等同步校验
5. LuCI 菜单缓存树 (indexcache) 节点与权限注册校验
6. LuCI Web 路由访问与 HTTP RPC 认证端点全功能响应校验
7. ipselect-runner 执行引擎状态与节点测速解析指令测试
8. 真实任务生命周期实测：启动 -> PID锁生成 -> 增量日志 -> 手动中止 -> 历史记录归档及快照回读
"""

import argparse
import http.cookiejar
import json
import posixpath
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import paramiko

    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


# 确保在 Windows 控制台环境下 UTF-8 输出正常
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# 预期部署的核心组件清单 (相对路径, 权限, 关键特征)
EXPECTED_CORE_FILES = [
    ("/etc/config/ipselect", 0o644, "config ipselect 'config'"),
    ("/etc/init.d/ipselect", 0o755, "#!/bin/sh /etc/rc.common"),
    ("/usr/bin/ipselect-runner", 0o755, "#!/bin/sh"),
    ("/usr/share/luci/menu.d/luci-app-ipselect.json", 0o644, "admin/services/ipselect"),
    ("/usr/share/rpcd/acl.d/luci-app-ipselect.json", 0o644, "luci-app-ipselect"),
    ("/usr/lib/lua/luci/controller/ipselect.lua", 0o644, "luci.controller.ipselect"),
    (
        "/www/luci-static/resources/view/ipselect/overview.js",
        0o644,
        "admin/services/ipselect/get_status",
    ),
]


class RouterLiveVerifier:
    def __init__(
        self,
        host: str = "10.10.18.2",
        port: int = 22,
        user: str = "root",
        password: str = "Xg@2020+",
        http_port: int = 80,
    ):
        if not HAS_PARAMIKO:
            raise RuntimeError(
                "缺少 paramiko 库，请使用 pip install paramiko 安装"
            )

        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.http_port = http_port

        self.ssh_client = None
        self.sftp_client = None
        self.http_opener = None
        self.cookie_jar = None

        self.passed_checks = 0
        self.failed_checks = 0
        self.warnings = 0

    def log_section(self, title: str):
        print(f"\n{'='*70}\n[TEST SECTION] {title}\n{'='*70}")

    def assert_check(self, condition: bool, message: str) -> bool:
        if condition:
            print(f"  [PASS] {message}")
            self.passed_checks += 1
            return True
        else:
            print(f"  [FAIL] {message}")
            self.failed_checks += 1
            return False

    def log_info(self, message: str):
        print(f"  [INFO] {message}")

    def log_warn(self, message: str):
        print(f"  [WARN] {message}")
        self.warnings += 1

    def run_cmd(self, cmd: str) -> tuple[int, str, str]:
        """通过 SSHClient 执行命令并返回 (exit_code, stdout, stderr)"""
        stdin, stdout, stderr = self.ssh_client.exec_command(cmd)
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        return code, out, err

    def connect(self):
        """建立 SSH 和 SFTP 连接"""
        self.log_info(f"正在建立 SSH 连接到 {self.user}@{self.host}:{self.port} ...")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        self.ssh_client = client
        self.sftp_client = client.open_sftp()
        self.log_info("SSH & SFTP 连接建立成功！")

    def close(self):
        """释放资源"""
        if self.sftp_client:
            self.sftp_client.close()
        if self.ssh_client:
            self.ssh_client.close()

    # -------------------------------------------------------------
    # 步骤 1: 现场连通性与底层系统环境探测
    # -------------------------------------------------------------
    def verify_system_environment(self):
        self.log_section("1/8 现场连通性与底层系统环境探测")

        code, uname, _ = self.run_cmd("uname -a")
        self.assert_check(
            code == 0, f"内核版本检测通过: {uname.splitlines()[0] if uname else '未知'}"
        )

        code, os_release, _ = self.run_cmd("cat /etc/os-release | grep -E '^PRETTY_NAME=' || cat /etc/openwrt_release")
        self.assert_check(
            code == 0 and len(os_release) > 0,
            f"操作系统发行版识别: {os_release.splitlines()[0]}",
        )

        code, _, _ = self.run_cmd("which ubus uci python3")
        self.assert_check(code == 0, "基础执行依赖 (ubus, uci, python3) 均就绪")

        code, uhttpd_st, _ = self.run_cmd("/etc/init.d/uhttpd status || pidof uhttpd")
        self.assert_check(code == 0, "LuCI Web 服务器 (uhttpd) 处于正常运行状态")

    # -------------------------------------------------------------
    # 步骤 2: 7 大核心组件文件部署与 POSIX 权限校验
    # -------------------------------------------------------------
    def verify_file_deployments(self):
        self.log_section("2/8 核心组件文件部署与 POSIX 权限校验")

        for rpath, exp_mode, keyword in EXPECTED_CORE_FILES:
            try:
                st = self.sftp_client.stat(rpath)
                mode_oct = oct(st.st_mode & 0o777)
                mode_ok = (st.st_mode & 0o777) == exp_mode

                # 验证可执行位特别针对 runner 和 init.d
                if "bin" in rpath or "init.d" in rpath:
                    has_exec = bool(st.st_mode & 0o111)
                    self.assert_check(
                        has_exec,
                        f"可执行组件 {rpath} 具备执行权限 (mode: {mode_oct})",
                    )
                else:
                    self.assert_check(
                        st.st_size > 0,
                        f"核心文件 {rpath} 已存在且非空 (size: {st.st_size} bytes, mode: {mode_oct})",
                    )

                # 读取文件并校验关键文本签名
                with self.sftp_client.open(rpath, "r") as rf:
                    content = rf.read().decode("utf-8", errors="replace")
                    self.assert_check(
                        keyword in content,
                        f"文件 {rpath} 包含有效代码签名: '{keyword}'",
                    )

            except IOError as e:
                self.assert_check(False, f"目标路径未找到或不可读: {rpath} ({e})")

    # -------------------------------------------------------------
    # 步骤 3: UCI 完整配置项可读性与结构校验
    # -------------------------------------------------------------
    def verify_uci_configuration(self):
        self.log_section("3/8 UCI 完整配置项可读性与结构校验")

        code, uci_out, err = self.run_cmd("uci show ipselect")
        self.assert_check(
            code == 0 and len(uci_out) > 0, "成功通过 uci show ipselect 读取配置"
        )

        expected_keys = [
            "ipselect.config=ipselect",
            "ipselect.config.enabled",
            "ipselect.config.workdir",
            "ipselect.config.env_tag",
            "ipselect.config.output_file",
            "ipselect.config.interface",
            "ipselect.config.auto_push",
            "ipselect.config.openclash_sync",
            "ipselect.config.openclash_provider",
            "ipselect.config.cron_enabled",
            "ipselect.config.cron_expression",
        ]

        for k in expected_keys:
            self.assert_check(k in uci_out, f"UCI 配置项包含: {k}")

        self.log_info(f"当前生效工作目录 workdir: {self._get_uci_val('workdir')}")
        self.log_info(f"当前生效输出文件 output_file: {self._get_uci_val('output_file')}")
        self.log_info(f"当前生效筛选标签 env_tag: {self._get_uci_val('env_tag')}")

    def _get_uci_val(self, key: str) -> str:
        _, out, _ = self.run_cmd(f"uci get ipselect.config.{key} 2>/dev/null")
        return out.strip()

    # -------------------------------------------------------------
    # 步骤 4: Crontab 定时任务托管规则与 init.d 幂等同步校验
    # -------------------------------------------------------------
    def verify_crontab_hosting(self):
        self.log_section("4/8 Crontab 托管规则与 init.d 服务同步校验")

        # 触发一次同步确保最新
        code, _, err = self.run_cmd("/etc/init.d/ipselect sync_cron")
        self.assert_check(code == 0, "/etc/init.d/ipselect sync_cron 执行成功")

        code, cron_content, _ = self.run_cmd("cat /etc/crontabs/root")
        self.assert_check(code == 0, "成功读取系统定时任务表 /etc/crontabs/root")

        has_marker = "#ipselect-cron-task" in cron_content
        self.assert_check(
            has_marker, "系统定时任务中包含 #ipselect-cron-task 专用托管标记"
        )

        # 验证定时任务行数唯一性（幂等去重防膨胀）
        matching_lines = [
            l for l in cron_content.splitlines() if "#ipselect-cron-task" in l
        ]
        self.assert_check(
            len(matching_lines) == 1,
            f"定时任务规则严格单一（防重复叠加），当前条目: {matching_lines[0] if matching_lines else '无'}",
        )

        # 验证定时时间与 UCI 配置一致
        cron_expr = self._get_uci_val("cron_expression") or "30 4 * * *"
        expected_cron_pattern = f"{cron_expr} /usr/bin/ipselect-runner start cron"
        self.assert_check(
            any(expected_cron_pattern in l for l in matching_lines),
            f"定时规则与 UCI 配置保持精确同步 ({cron_expr})",
        )

    # -------------------------------------------------------------
    # 步骤 5: LuCI 菜单缓存树 (indexcache) 节点与权限注册校验
    # -------------------------------------------------------------
    def verify_luci_menu_cache(self):
        self.log_section("5/8 LuCI 菜单缓存树 (indexcache) 节点与权限注册校验")

        # 在软路由上使用 python3 检查 indexcache
        py_check = """python3 -c "
import glob, json, sys
cache_files = glob.glob('/tmp/luci-indexcache*.json')
if not cache_files:
    print('NO_CACHE')
    sys.exit(1)
latest = max(cache_files)
with open(latest, 'r', encoding='utf-8') as f:
    data = json.load(f)
services = data.get('children', {}).get('admin', {}).get('children', {}).get('services', {}).get('children', {})
if 'ipselect' not in services:
    print('NO_IPSELECT')
    sys.exit(2)
node = services['ipselect']
print(json.dumps({
    'title': node.get('title'),
    'order': node.get('order'),
    'action_type': node.get('action', {}).get('type'),
    'action_path': node.get('action', {}).get('path'),
    'children_keys': list(node.get('children', {}).keys())
}))
" """
        code, out, err = self.run_cmd(py_check)
        if code != 0:
            # 若缓存刚被清理，尝试重新读取
            self.run_cmd("rm -f /tmp/luci-indexcache* && /etc/init.d/uhttpd restart")
            time.sleep(2)
            code, out, err = self.run_cmd(py_check)

        self.assert_check(
            code == 0 and "NO_" not in out,
            "LuCI 菜单缓存树成功加载并索引 admin/services/ipselect",
        )

        try:
            info = json.loads(out)
            self.assert_check(
                info.get("order") == 60,
                f"菜单排序权重 order 校验正确 ({info.get('order')})",
            )
            # 校验包含 RPC 子路由
            expected_rpc = [
                "get_status",
                "start",
                "stop",
                "get_log",
                "clear_log",
                "get_results",
                "get_history",
                "get_history_log",
            ]
            children = info.get("children_keys", [])
            all_rpc_found = all(k in children for k in expected_rpc)
            self.assert_check(
                all_rpc_found,
                f"Controller RPC 子动作全部正确注册在路由树中 ({len(children)} 个子节点)",
            )
        except Exception as e:
            self.assert_check(False, f"解析菜单索引 JSON 失败: {e}")

    # -------------------------------------------------------------
    # 步骤 6: LuCI Web 路由访问与 HTTP RPC 认证端点全功能响应校验
    # -------------------------------------------------------------
    def verify_http_web_and_rpc(self):
        self.log_section("6/8 LuCI Web 路由访问与 HTTP RPC 认证端点全功能响应校验")

        # 1. 模拟登录获取会话 Cookie
        self.cookie_jar = http.cookiejar.CookieJar()
        self.http_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

        base_url = f"http://{self.host}:{self.http_port}"
        login_url = f"{base_url}/cgi-bin/luci/"
        login_data = urllib.parse.urlencode(
            {"luci_username": self.user, "luci_password": self.password}
        ).encode("utf-8")

        try:
            req = urllib.request.Request(login_url, data=login_data)
            resp = self.http_opener.open(req, timeout=10)
            self.assert_check(
                resp.status == 200, f"HTTP POST 登录 LuCI 成功 (状态码: {resp.status})"
            )

            cookie_names = [c.name for c in self.cookie_jar]
            has_auth_cookie = any("sysauth" in name for name in cookie_names)
            self.assert_check(
                has_auth_cookie,
                f"成功获取授权 Session Cookie ({cookie_names})",
            )
        except Exception as e:
            self.assert_check(False, f"LuCI HTTP 登录失败: {e}")
            return

        # 2. 访问静态 JavaScript 视图文件
        js_url = f"{base_url}/luci-static/resources/view/ipselect/overview.js"
        try:
            js_resp = self.http_opener.open(js_url, timeout=10)
            js_bytes = js_resp.read()
            self.assert_check(
                js_resp.status == 200 and len(js_bytes) > 10000,
                f"前端现代 JavaScript 视图静态资源可访问 (HTTP 200, 大小: {len(js_bytes)} bytes)",
            )
        except Exception as e:
            self.assert_check(False, f"获取 overview.js 失败: {e}")

        # 3. 访问 Web 页面主路由
        view_url = f"{base_url}/cgi-bin/luci/admin/services/ipselect"
        try:
            page_resp = self.http_opener.open(view_url, timeout=10)
            page_content = page_resp.read().decode("utf-8", errors="replace")
            self.assert_check(
                page_resp.status == 200,
                f"Web 入口路由 /admin/services/ipselect 正常响应 (HTTP 200, 大小: {len(page_content)} bytes)",
            )
            self.assert_check(
                "ipselect" in page_content,
                "页面 HTML 骨架包含 ipselect 视图容器",
            )
        except Exception as e:
            self.assert_check(False, f"访问 Web 概览页面异常: {e}")

        # 4. 测试各 RPC 端点 HTTP 响应
        rpc_endpoints = [
            ("get_status", "application/json", ["running", "pid", "last_run"]),
            ("get_results", "application/json", ["file", "count", "nodes"]),
            ("get_history", "application/json", None),
            ("get_log", "text/plain", None),
        ]

        for ep, expected_ctype, required_keys in rpc_endpoints:
            url = f"{base_url}/cgi-bin/luci/admin/services/ipselect/{ep}"
            try:
                r = self.http_opener.open(url, timeout=10)
                content_type = r.headers.get("content-type", "")
                body = r.read().decode("utf-8", errors="replace")

                status_ok = r.status == 200
                ctype_ok = expected_ctype in content_type
                self.assert_check(
                    status_ok and ctype_ok,
                    f"RPC 端点 {ep} 正常响应 (HTTP {r.status}, Content-Type: {content_type})",
                )

                if required_keys:
                    data = json.loads(body)
                    keys_ok = all(k in data for k in required_keys)
                    self.assert_check(
                        keys_ok, f"RPC 端点 {ep} JSON 字段完整包含: {required_keys}"
                    )
            except Exception as e:
                self.assert_check(False, f"RPC 端点 {ep} 访问异常: {e}")

    # -------------------------------------------------------------
    # 步骤 7: 执行引擎运行命令测试 (status & results)
    # -------------------------------------------------------------
    def verify_runner_commands(self):
        self.log_section("7/8 ipselect-runner 执行引擎 CLI 与数据解析测试")

        # 1. 测试 status 命令
        code, out, err = self.run_cmd("/usr/bin/ipselect-runner status")
        self.assert_check(code == 0, "/usr/bin/ipselect-runner status 退出码为 0")
        try:
            st = json.loads(out)
            self.assert_check(
                isinstance(st.get("running"), bool) and "pid" in st and "last_run" in st,
                f"status 输出合法 JSON: running={st.get('running')}, pid={st.get('pid')}, last_run={st.get('last_run')}",
            )
        except json.JSONDecodeError:
            self.assert_check(False, f"status 输出不是合法 JSON: {out}")

        # 2. 测试 results 命令
        code, out, err = self.run_cmd("/usr/bin/ipselect-runner results")
        self.assert_check(code == 0, "/usr/bin/ipselect-runner results 退出码为 0")
        try:
            res = json.loads(out)
            self.assert_check(
                "file" in res and "count" in res and "nodes" in res,
                f"results 输出合法结构: file={res.get('file')}, 节点数量={res.get('count')}",
            )
            if res.get("count", 0) > 0:
                first_node = res["nodes"][0]
                self.assert_check(
                    "ip" in first_node and "port" in first_node and "remark" in first_node,
                    f"解析首个测速优选节点: {first_node['ip']}:{first_node['port']} ({first_node.get('remark', '')})",
                )
        except json.JSONDecodeError:
            self.assert_check(False, f"results 输出不是合法 JSON: {out}")

    # -------------------------------------------------------------
    # 步骤 8: 真实生命周期实测：启动 -> PID锁 -> 中止 -> 历史归档
    # -------------------------------------------------------------
    def verify_execution_lifecycle(self):
        self.log_section("8/8 真实任务生命周期实测：启动、PID锁、日志增量、中止与历史归档")

        # 确保初始无挂起任务
        self.run_cmd("/usr/bin/ipselect-runner stop")
        time.sleep(1)

        # 1. 触发任务启动
        self.log_info("正在触发任务启动: /usr/bin/ipselect-runner start manual ...")
        code, out, _ = self.run_cmd("/usr/bin/ipselect-runner start manual")
        self.assert_check(code == 0, "启动指令成功返回")
        try:
            resp = json.loads(out)
            self.assert_check(resp.get("success") is True, "启动返回 success: true")
        except Exception:
            self.assert_check(False, f"启动返回非 JSON: {out}")

        time.sleep(1.5)

        # 2. 验证 PID 锁生成与进程存活
        code, out, _ = self.run_cmd("/usr/bin/ipselect-runner status")
        try:
            st = json.loads(out)
            pid = st.get("pid", 0)
            self.assert_check(
                st.get("running") is True and pid > 0,
                f"PID 锁文件生效且任务处于运行态 (PID: {pid})",
            )
            # 校验软路由进程表中实际存在该 PID
            code_ps, _, _ = self.run_cmd(f"kill -0 {pid} 2>/dev/null")
            self.assert_check(code_ps == 0, f"内核确认 PID {pid} 真实在系统中运行")
        except Exception as e:
            self.assert_check(False, f"状态读取失败: {e}")

        # 3. 验证日志增量写入
        code, log_content, _ = self.run_cmd("/usr/bin/ipselect-runner log 0")
        self.assert_check(
            code == 0 and len(log_content) > 0,
            f"成功读取任务执行日志 (/var/log/ipselect.log, 长度: {len(log_content)} 字符)",
        )
        self.assert_check(
            "任务开始执行" in log_content or "工作目录" in log_content,
            "日志正确记录任务启动元数据与工作路径",
        )

        # 4. 触发手动中止
        self.log_info("正在触发任务中止: /usr/bin/ipselect-runner stop ...")
        code, stop_out, _ = self.run_cmd("/usr/bin/ipselect-runner stop")
        self.assert_check(code == 0, "中止指令成功返回")
        try:
            resp = json.loads(stop_out)
            self.assert_check(
                resp.get("success") is True, "中止返回 success: true"
            )
        except Exception:
            self.assert_check(False, f"中止返回非 JSON: {stop_out}")

        time.sleep(1)

        # 5. 验证进程已回收，PID 文件已清理
        code, st_after, _ = self.run_cmd("/usr/bin/ipselect-runner status")
        try:
            st = json.loads(st_after)
            self.assert_check(
                st.get("running") is False and st.get("pid") == 0,
                "任务已彻底终止，PID 锁文件已清理，状态恢复为 idle",
            )
        except Exception as e:
            self.assert_check(False, f"中止后状态异常: {e}")

        # 6. 验证历史记录已写入 /etc/ipselect/history.json
        code, hist_out, _ = self.run_cmd("/usr/bin/ipselect-runner history")
        self.assert_check(
            code == 0 and len(hist_out) > 0,
            "成功读取历史记录表 /etc/ipselect/history.json",
        )
        try:
            hist_list = json.loads(hist_out)
            self.assert_check(
                isinstance(hist_list, list) and len(hist_list) > 0,
                f"历史记录非空，当前已归档 {len(hist_list)} 条记录",
            )
            latest = hist_list[0]
            self.assert_check(
                latest.get("status") in ("terminated", "success", "failed")
                and "id" in latest
                and "timestamp" in latest
                and "duration" in latest,
                f"最新历史条目归档完整: ID={latest.get('id')}, 状态={latest.get('status')}, 耗时={latest.get('duration')}s",
            )

            # 7. 验证对应历史日志快照可通过 history-log 读取
            hist_id = latest["id"]
            code, hlog_out, _ = self.run_cmd(
                f"/usr/bin/ipselect-runner history-log {hist_id}"
            )
            self.assert_check(
                code == 0 and "任务" in hlog_out,
                f"通过 history-log {hist_id} 成功检索归档历史日志快照 (长度: {len(hlog_out)} 字符)",
            )
        except Exception as e:
            self.assert_check(False, f"历史记录解析失败: {e}")

    # -------------------------------------------------------------
    # 汇总报告
    # -------------------------------------------------------------
    def run_all(self) -> bool:
        start_time = time.time()
        print(f"\n{'#'*70}")
        print(f"# 开始执行 luci-app-ipselect 软路由现场全链路联调实测")
        print(f"# 目标主机: {self.user}@{self.host}:{self.port}")
        print(f"{'#'*70}")

        try:
            self.connect()

            self.verify_system_environment()
            self.verify_file_deployments()
            self.verify_uci_configuration()
            self.verify_crontab_hosting()
            self.verify_luci_menu_cache()
            self.verify_http_web_and_rpc()
            self.verify_runner_commands()
            self.verify_execution_lifecycle()

        except Exception as e:
            print(f"\n[CRITICAL ERROR] 联调过程中发生未捕获异常: {e}")
            import traceback

            traceback.print_exc()
            self.failed_checks += 1
        finally:
            self.close()

        duration = time.time() - start_time
        print(f"\n{'='*70}")
        print(f"软路由现场真机实测联调完成！耗时: {duration:.2f}s")
        print(
            f"结果汇总: 通过 = {self.passed_checks}, 失败 = {self.failed_checks}, 警告 = {self.warnings}"
        )
        print(f"{'='*70}")

        if self.failed_checks == 0:
            print("[SUCCESS] 所有真机联调测试全部通过！luci-app-ipselect 现场验证圆满成功！\n")
            return True
        else:
            print(f"[FAILED] 共有 {self.failed_checks} 项检查未通过，请排查！\n")
            return False


def main():
    parser = argparse.ArgumentParser(
        description="Verify luci-app-ipselect live on iStoreOS router"
    )
    parser.add_argument("--host", default="10.10.18.2", help="Target router IP")
    parser.add_argument("--port", type=int, default=22, help="SSH port")
    parser.add_argument("-u", "--user", default="root", help="SSH user")
    parser.add_argument("-p", "--password", default="Xg@2020+", help="SSH password")
    parser.add_argument(
        "--http-port", type=int, default=80, help="HTTP web port"
    )

    args = parser.parse_args()

    verifier = RouterLiveVerifier(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        http_port=args.http_port,
    )

    success = verifier.run_all()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
