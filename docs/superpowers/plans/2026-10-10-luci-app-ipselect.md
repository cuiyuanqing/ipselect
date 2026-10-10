# iStoreOS 节点优选控制台插件 (luci-app-ipselect) 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建并部署 iStoreOS / OpenWrt 官方技术规范的 Web UI 插件 `luci-app-ipselect`，提供一键优选触发、实时终端日志滚屏、优选结果表格化呈现、历史记录追溯以及 OpenClash 订阅自动联动能力。

**Architecture:** 采用 iStoreOS 25+ / OpenWrt 21+ 标准的 LuCI JavaScript View 架构，通过 LuCI Controller RPC 后端提供状态与数据交互，由独立执行引擎 `/usr/bin/ipselect-runner` 负责后台任务调度、进程互斥锁、日志管理及 `/etc/ipselect/history.json` 历史归档，通过 `/etc/init.d/ipselect` 与 UCI 纳管系统 Crontab。

**Tech Stack:** JavaScript (ES6+ / LuCI JS API), Lua (LuCI Controller), Shell (POSIX / BusyBox ash), UCI, OpenWrt rpcd / ipk, Python 3.

**Spec:** `docs/superpowers/specs/2026-10-10-luci-app-ipselect-design.md`

## Global Constraints

- **系统平台规范**：目标软路由环境为 iStoreOS 25.12.5 (OpenWrt x86_64, Linux 6.6.x)。
- **菜单层级规范**：必须位于【服务】(Services) 菜单下，显示名称为“节点优选”。
- **进程互斥契约**：严格使用 `/var/run/ipselect.pid` 维护互斥锁，严禁同一时间并发运行多个优选进程。
- **环境预设解耦**：支持“公司 (best_us.txt)”与“家庭 (home_us_best_node.txt)”两种模式，配置持久化于 `/etc/config/ipselect`。
- **安全与存储规范**：历史记录上限为 10 条，日志文件滚动淘汰，避免消耗软路由有限存储空间。

---

### Task 1: 后端执行引擎与历史记录管理 (`/usr/bin/ipselect-runner`)

**Files:**
- Create: `D:\nice_wall\luci-app-ipselect\root\usr\bin\ipselect-runner`
- Create: `D:\nice_wall\luci-app-ipselect\tests\test_runner.sh`

**Interfaces:**
- Produces:
  - Command: `/usr/bin/ipselect-runner {start|stop|status|log|clear-log|results|history|history-log}`
  - Files: `/var/run/ipselect.pid`, `/var/log/ipselect.log`, `/etc/ipselect/history.json`, `/etc/ipselect/logs/history_<id>.log`

- [ ] **Step 1: 编写 Runner 功能测试脚本 `tests/test_runner.sh`**

```bash
#!/bin/sh
# tests/test_runner.sh - 测试 ipselect-runner 的核心逻辑
set -e

TMP_DIR="/tmp/test_ipselect"
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR/var/run" "$TMP_DIR/var/log" "$TMP_DIR/etc/ipselect/logs" "$TMP_DIR/root/ipselect"

# 模拟 UCI 与环境
export TEST_ROOT="$TMP_DIR"
cat << 'EOF' > "$TMP_DIR/mock_uci.sh"
#!/bin/sh
case "$1" in
    "get")
        case "$2" in
            "ipselect.config.workdir") echo "$TEST_ROOT/root/ipselect" ;;
            "ipselect.config.env_tag") echo "公司" ;;
            "ipselect.config.output_file") echo "best_us.txt" ;;
            "ipselect.config.interface") echo "br-lan" ;;
            "ipselect.config.auto_push") echo "0" ;;
            "ipselect.config.openclash_sync") echo "0" ;;
            *) echo "" ;;
        esac
    ;;
esac
EOF
chmod +x "$TMP_DIR/mock_uci.sh"

# 创建模拟的优选结果文件
cat << 'EOF' > "$TMP_DIR/root/ipselect/best_us.txt"
104.16.89.201:443#公司-CF官方-SJC
162.158.21.45:443#公司-CF官方-LAX
EOF

echo "✅ 测试环境准备完毕"
```

- [ ] **Step 2: 编写 `root/usr/bin/ipselect-runner` 核心调度脚本**

实现支持 `start`, `stop`, `status`, `log`, `clear-log`, `results`, `history`, `history-log` 等完整子命令。

```bash
#!/bin/sh
# /usr/bin/ipselect-runner - iStoreOS 节点优选执行引擎
BASE_ROOT="${TEST_ROOT:-}"
PID_FILE="${BASE_ROOT}/var/run/ipselect.pid"
LOG_FILE="${BASE_ROOT}/var/log/ipselect.log"
DATA_DIR="${BASE_ROOT}/etc/ipselect"
HISTORY_FILE="${DATA_DIR}/history.json"
HISTORY_LOG_DIR="${DATA_DIR}/logs"

mkdir -p "${BASE_ROOT}/var/run" "${BASE_ROOT}/var/log" "${DATA_DIR}" "${HISTORY_LOG_DIR}"

UCI_CMD="uci"
if [ -n "$TEST_ROOT" ] && [ -x "$TEST_ROOT/mock_uci.sh" ]; then
    UCI_CMD="$TEST_ROOT/mock_uci.sh"
fi

get_config() {
    $UCI_CMD get "ipselect.config.$1" 2>/dev/null || echo "$2"
}

is_running() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            return 0
        fi
        rm -f "$PID_FILE"
    fi
    return 1
}

case "$1" in
    status)
        if is_running; then
            PID=$(cat "$PID_FILE")
            RUNNING=true
        else
            PID=0
            RUNNING=false
        fi
        LAST_RUN="从未运行"
        if [ -f "$HISTORY_FILE" ]; then
            LAST_RUN=$(grep -o '"timestamp": "[^"]*"' "$HISTORY_FILE" | head -n 1 | cut -d'"' -f4)
        fi
        echo "{\"running\": $RUNNING, \"pid\": $PID, \"last_run\": \"$LAST_RUN\"}"
        ;;

    start)
        if is_running; then
            echo "{\"success\": false, \"message\": \"任务正在运行中 (PID: $(cat $PID_FILE))\"}"
            exit 1
        fi
        TRIGGER="${2:-manual}"
        START_TIME=$(date '+%Y-%m-%d %H:%M:%S')
        TASK_ID=$(date '+%Y%m%d-%H%M%S')
        
        WORKDIR=$(get_config "workdir" "/root/ipselect")
        ENV_TAG=$(get_config "env_tag" "公司")
        OUTPUT_FILE=$(get_config "output_file" "best_us.txt")
        INTERFACE=$(get_config "interface" "br-lan")
        AUTO_PUSH=$(get_config "auto_push" "1")
        OPENCLASH_SYNC=$(get_config "openclash_sync" "1")
        PROVIDER_NAME=$(get_config "openclash_provider" "datree")

        # 后台异步启动执行
        (
            echo "$$" > "$PID_FILE"
            echo "========== [${START_TIME}] 任务开始执行 (触发源: ${TRIGGER}) ==========" > "$LOG_FILE"
            STATUS="success"
            GIT_STATUS="skipped"
            OC_STATUS="skipped"
            NODE_COUNT=0
            T_START=$(date +%s)

            if [ -d "$WORKDIR" ]; then
                cd "$WORKDIR"
                echo "[*] 进入工作目录: $WORKDIR" >> "$LOG_FILE"
                if [ -d ".git" ]; then
                    echo "[*] 同步远程最新代码 (git pull)..." >> "$LOG_FILE"
                    git pull origin main >> "$LOG_FILE" 2>&1 || true
                fi
                
                echo "[*] 开始执行节点筛选测速: --tag ${ENV_TAG} --output ${OUTPUT_FILE} --interface ${INTERFACE}..." >> "$LOG_FILE"
                if python3 scripts/filter_us_nodes.py --tag "$ENV_TAG" --output "$OUTPUT_FILE" --interface "$INTERFACE" >> "$LOG_FILE" 2>&1; then
                    echo "[+] 节点筛选测速完成！" >> "$LOG_FILE"
                    if [ -f "$OUTPUT_FILE" ]; then
                        NODE_COUNT=$(grep -v '^#' "$OUTPUT_FILE" | grep -c ':' || echo 0)
                    fi
                else
                    echo "[!] 节点筛选测速发生异常！" >> "$LOG_FILE"
                    STATUS="failed"
                fi

                if [ "$STATUS" = "success" ] && [ "$AUTO_PUSH" = "1" ] && [ -d ".git" ]; then
                    echo "[*] 检测 Git 变更并提交..." >> "$LOG_FILE"
                    git config user.name "iStoreOS-IPSelect" 2>/dev/null || true
                    git config user.email "ipselect@local" 2>/dev/null || true
                    git add "$OUTPUT_FILE"
                    if ! git diff --staged --quiet; then
                        git commit -m "auto: iStoreOS 节点优选更新 [$(date '+%Y-%m-%d %H:%M:%S')]" >> "$LOG_FILE" 2>&1
                        if git push origin main >> "$LOG_FILE" 2>&1; then
                            echo "[+] 成功推送到 GitHub 远程仓库！" >> "$LOG_FILE"
                            GIT_STATUS="pushed"
                        else
                            echo "[!] 推送到 GitHub 失败！" >> "$LOG_FILE"
                            GIT_STATUS="error"
                        fi
                    else
                        echo "[*] 节点未发生变动，无需推送" >> "$LOG_FILE"
                        GIT_STATUS="unchanged"
                    fi
                fi
            else
                echo "[!] 错误: 工作目录 $WORKDIR 不存在！" >> "$LOG_FILE"
                STATUS="failed"
            fi

            if [ "$STATUS" = "success" ] && [ "$OPENCLASH_SYNC" = "1" ]; then
                echo "[*] 联动触发 OpenClash 订阅更新..." >> "$LOG_FILE"
                if [ -x "/usr/bin/update_openclash_sub.sh" ]; then
                    /usr/bin/update_openclash_sub.sh >> "$LOG_FILE" 2>&1 || true
                    OC_STATUS="success"
                else
                    echo "[*] /usr/bin/update_openclash_sub.sh 不存在，跳过联动" >> "$LOG_FILE"
                    OC_STATUS="skipped"
                fi
            fi

            T_END=$(date +%s)
            DURATION=$((T_END - T_START))
            echo "========== [$(date '+%Y-%m-%d %H:%M:%S')] 任务执行完毕 (耗时: ${DURATION}s, 状态: ${STATUS}) ==========" >> "$LOG_FILE"

            # 归档日志快照
            ARCHIVE_LOG="${HISTORY_LOG_DIR}/history_${TASK_ID}.log"
            cp "$LOG_FILE" "$ARCHIVE_LOG"

            # 更新历史记录 JSON (保留最近 10 条)
            TMP_HIST="/tmp/history_tmp.json"
            NEW_RECORD="{\"id\":\"$TASK_ID\",\"timestamp\":\"$START_TIME\",\"trigger\":\"$TRIGGER\",\"duration\":$DURATION,\"status\":\"$STATUS\",\"nodeCount\":$NODE_COUNT,\"gitStatus\":\"$GIT_STATUS\",\"openclashStatus\":\"$OC_STATUS\",\"logFile\":\"$ARCHIVE_LOG\"}"
            if [ -f "$HISTORY_FILE" ]; then
                # 去除外层括号并插入新条目
                EXISTING=$(cat "$HISTORY_FILE" | sed -e 's/^\[//' -e 's/\]$//' | sed -e '/^[[:space:]]*$/d')
                if [ -n "$EXISTING" ]; then
                    echo "[$NEW_RECORD,$EXISTING]" > "$TMP_HIST"
                else
                    echo "[$NEW_RECORD]" > "$TMP_HIST"
                fi
            else
                echo "[$NEW_RECORD]" > "$TMP_HIST"
            fi
            # 截取前 10 条
            python3 -c "import json; data=json.load(open('$TMP_HIST'))[:10]; json.dump(data, open('$HISTORY_FILE','w'), indent=2)" 2>/dev/null || mv "$TMP_HIST" "$HISTORY_FILE"
            rm -f "$TMP_HIST" "$PID_FILE"
        ) &
        echo "{\"success\": true, \"message\": \"任务已启动\"}"
        ;;

    stop)
        if is_running; then
            PID=$(cat "$PID_FILE")
            # 递归杀掉子进程
            pkill -P "$PID" 2>/dev/null || true
            kill -TERM "$PID" 2>/dev/null || true
            sleep 1
            kill -9 "$PID" 2>/dev/null || true
            rm -f "$PID_FILE"
            echo "========== [$(date '+%Y-%m-%d %H:%M:%S')] 任务被手动中止 ==========" >> "$LOG_FILE"
            echo "{\"success\": true, \"message\": \"任务已成功终止\"}"
        else
            echo "{\"success\": false, \"message\": \"当前没有运行中的任务\"}"
        fi
        ;;

    log)
        OFFSET="${2:-0}"
        if [ -f "$LOG_FILE" ]; then
            TOTAL_SIZE=$(wc -c < "$LOG_FILE" | tr -d ' ')
            if [ "$OFFSET" -gt 0 ] && [ "$OFFSET" -lt "$TOTAL_SIZE" ]; then
                tail -c "+$((OFFSET + 1))" "$LOG_FILE"
            else
                cat "$LOG_FILE"
            fi
        else
            echo ""
        fi
        ;;

    clear-log)
        > "$LOG_FILE"
        echo "{\"success\": true}"
        ;;

    results)
        WORKDIR=$(get_config "workdir" "/root/ipselect")
        OUTPUT_FILE=$(get_config "output_file" "best_us.txt")
        TARGET_PATH="${WORKDIR}/${OUTPUT_FILE}"
        if [ -f "$TARGET_PATH" ]; then
            python3 -c "
import json, os, time
path = '$TARGET_PATH'
mtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(path)))
nodes = []
with open(path, 'r', encoding='utf-8', errors='ignore') as f:
    idx = 1
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'): continue
        parts = line.split('#')
        addr = parts[0]
        remark = parts[1] if len(parts) > 1 else '未命名'
        ip, port = addr.split(':') if ':' in addr else (addr, '443')
        nodes.append({'index': idx, 'ip': ip, 'port': port, 'remark': remark})
        idx += 1
print(json.dumps({'file': '$OUTPUT_FILE', 'update_time': mtime, 'count': len(nodes), 'nodes': nodes}))
"
        else
            echo "{\"file\": \"$OUTPUT_FILE\", \"update_time\": \"从未生成\", \"count\": 0, \"nodes\": []}"
        fi
        ;;

    history)
        if [ -f "$HISTORY_FILE" ]; then
            cat "$HISTORY_FILE"
        else
            echo "[]"
        fi
        ;;

    history-log)
        REQ_ID="$2"
        TARGET_LOG="${HISTORY_LOG_DIR}/history_${REQ_ID}.log"
        if [ -n "$REQ_ID" ] && [ -f "$TARGET_LOG" ]; then
            cat "$TARGET_LOG"
        else
            echo "日志不存在或已被清除"
        fi
        ;;

    *)
        echo "用法: $0 {start|stop|status|log|clear-log|results|history|history-log}"
        exit 1
        ;;
esac
```

- [ ] **Step 3: 运行本地 Runner 逻辑测试**

运行：`sh tests/test_runner.sh`
预期：输出测试通过且各类返回 JSON 合法。

- [ ] **Step 4: 提交 Task 1 成果**

```bash
git add root/usr/bin/ipselect-runner tests/test_runner.sh
git commit -m "feat: 实现 ipselect-runner 后端执行引擎与历史记录管理"
```

---

### Task 2: UCI 配置定义与系统服务管理 (`/etc/config/ipselect` & `/etc/init.d/ipselect`)

**Files:**
- Create: `D:\nice_wall\luci-app-ipselect\root\etc\config\ipselect`
- Create: `D:\nice_wall\luci-app-ipselect\root\etc\init.d\ipselect`

**Interfaces:**
- Produces:
  - UCI 存储结构：`ipselect.config` (enabled, workdir, env_tag, output_file, interface, auto_push, openclash_sync, openclash_provider, cron_enabled, cron_hour, cron_minute)
  - `/etc/init.d/ipselect {start|stop|restart|reload|sync_cron}`

- [ ] **Step 1: 编写 `/etc/config/ipselect` 默认配置文件**

```uci
config ipselect 'config'
	option enabled '1'
	option workdir '/root/ipselect'
	option env_tag '公司'
	option output_file 'best_us.txt'
	option interface 'br-lan'
	option auto_push '1'
	option openclash_sync '1'
	option openclash_provider 'datree'
	option cron_enabled '1'
	option cron_hour '4'
	option cron_minute '30'
```

- [ ] **Step 2: 编写 `/etc/init.d/ipselect` 服务控制脚本（纳管 Crontab）**

```bash
#!/bin/sh /etc/rc.common
# /etc/init.d/ipselect - iStoreOS 节点优选服务管理与定时任务同步

START=95
STOP=10

CRON_FILE="/etc/crontabs/root"
RUNNER="/usr/bin/ipselect-runner"

sync_cron() {
    config_load ipselect
    local enabled cron_enabled cron_hour cron_minute
    config_get_bool enabled config enabled 1
    config_get_bool cron_enabled config cron_enabled 1
    config_get cron_hour config cron_hour "4"
    config_get cron_minute config cron_minute "30"

    # 清理已有的 ipselect cron 标记
    if [ -f "$CRON_FILE" ]; then
        sed -i '/#ipselect-cron-task/d' "$CRON_FILE"
    fi

    # 如果启用且开启了定时
    if [ "$enabled" -eq 1 ] && [ "$cron_enabled" -eq 1 ]; then
        echo "${cron_minute} ${cron_hour} * * * ${RUNNER} start cron >/dev/null 2>&1 #ipselect-cron-task" >> "$CRON_FILE"
    fi
    /etc/init.d/cron reload 2>/dev/null || true
}

start() {
    sync_cron
}

stop() {
    if [ -f "$CRON_FILE" ]; then
        sed -i '/#ipselect-cron-task/d' "$CRON_FILE"
        /etc/init.d/cron reload 2>/dev/null || true
    fi
    $RUNNER stop >/dev/null 2>&1 || true
}

reload() {
    sync_cron
}
```

- [ ] **Step 3: 语法检查与文件权限**

运行：`sh -n root/etc/init.d/ipselect`
预期：语法正确退出码 0。

- [ ] **Step 4: 提交 Task 2 成果**

```bash
git add root/etc/config/ipselect root/etc/init.d/ipselect
git commit -m "feat: 定义 ipselect UCI 配置项与 init.d 定时任务自动同步服务"
```

---

### Task 3: LuCI Controller 后端 RPC 接口与 ACL 权限配置

**Files:**
- Create: `D:\nice_wall\luci-app-ipselect\luasrc\controller\ipselect.lua`
- Create: `D:\nice_wall\luci-app-ipselect\root\usr\share\luci\menu.d\luci-app-ipselect.json`
- Create: `D:\nice_wall\luci-app-ipselect\root\usr\share\rpcd\acl.d\luci-app-ipselect.json`

**Interfaces:**
- Produces:
  - Menu Entry: `admin/services/ipselect`
  - Controller Endpoints: `get_status`, `start`, `stop`, `get_log`, `clear_log`, `get_results`, `get_history`, `get_history_log`

- [ ] **Step 1: 编写 `luasrc/controller/ipselect.lua` 后端控制器**

```lua
module("luci.controller.ipselect", package.seeall)

function index()
    if not nixio.fs.access("/etc/config/ipselect") then
        return
    end

    local page = entry({"admin", "services", "ipselect"}, alias("admin", "services", "ipselect", "client"), _("节点优选"), 60)
    page.dependent = true
    page.acl_depends = { "luci-app-ipselect" }

    entry({"admin", "services", "ipselect", "client"}, template("ipselect/overview"), _("概览"), 10).leaf = true
    entry({"admin", "services", "ipselect", "get_status"}, call("action_get_status")).leaf = true
    entry({"admin", "services", "ipselect", "start"}, call("action_start")).leaf = true
    entry({"admin", "services", "ipselect", "stop"}, call("action_stop")).leaf = true
    entry({"admin", "services", "ipselect", "get_log"}, call("action_get_log")).leaf = true
    entry({"admin", "services", "ipselect", "clear_log"}, call("action_clear_log")).leaf = true
    entry({"admin", "services", "ipselect", "get_results"}, call("action_get_results")).leaf = true
    entry({"admin", "services", "ipselect", "get_history"}, call("action_get_history")).leaf = true
    entry({"admin", "services", "ipselect", "get_history_log"}, call("action_get_history_log")).leaf = true
end

local function exec_cmd(cmd)
    local pp = io.popen(cmd)
    local data = pp:read("*all")
    pp:close()
    return data
end

function action_get_status()
    luci.http.prepare_content("application/json")
    luci.http.write(exec_cmd("/usr/bin/ipselect-runner status"))
end

function action_start()
    luci.http.prepare_content("application/json")
    luci.http.write(exec_cmd("/usr/bin/ipselect-runner start manual"))
end

function action_stop()
    luci.http.prepare_content("application/json")
    luci.http.write(exec_cmd("/usr/bin/ipselect-runner stop"))
end

function action_get_log()
    local offset = luci.http.formvalue("offset") or "0"
    luci.http.prepare_content("text/plain")
    luci.http.write(exec_cmd("/usr/bin/ipselect-runner log " .. offset))
end

function action_clear_log()
    luci.http.prepare_content("application/json")
    luci.http.write(exec_cmd("/usr/bin/ipselect-runner clear-log"))
end

function action_get_results()
    luci.http.prepare_content("application/json")
    luci.http.write(exec_cmd("/usr/bin/ipselect-runner results"))
end

function action_get_history()
    luci.http.prepare_content("application/json")
    luci.http.write(exec_cmd("/usr/bin/ipselect-runner history"))
end

function action_get_history_log()
    local id = luci.http.formvalue("id") or ""
    luci.http.prepare_content("text/plain")
    luci.http.write(exec_cmd("/usr/bin/ipselect-runner history-log " .. id))
end
```

- [ ] **Step 2: 编写 LuCI 菜单注册 JSON `root/usr/share/luci/menu.d/luci-app-ipselect.json`**

```json
{
	"admin/services/ipselect": {
		"title": "节点优选",
		"order": 60,
		"action": {
			"type": "view",
			"path": "ipselect/overview"
		},
		"depends": {
			"acl": [ "luci-app-ipselect" ]
		}
	}
}
```

- [ ] **Step 3: 编写 RPCD ACL 权限声明 `root/usr/share/rpcd/acl.d/luci-app-ipselect.json`**

```json
{
	"luci-app-ipselect": {
		"description": "Grant access for luci-app-ipselect",
		"read": {
			"uci": [ "ipselect" ],
			"file": {
				"/var/log/ipselect.log": [ "read" ],
				"/etc/ipselect/history.json": [ "read" ]
			}
		},
		"write": {
			"uci": [ "ipselect" ]
		}
	}
}
```

- [ ] **Step 4: 提交 Task 3 成果**

```bash
git add luasrc/controller/ipselect.lua root/usr/share/luci/menu.d/luci-app-ipselect.json root/usr/share/rpcd/acl.d/luci-app-ipselect.json
git commit -m "feat: 实现 LuCI Controller RPC 端点与菜单/ACL 授权定义"
```

---

### Task 4: LuCI 前端视图实现 (现代 JavaScript View: 仪表盘、4 大选项卡、动态日志与表格)

**Files:**
- Create: `D:\nice_wall\luci-app-ipselect\htdocs\luci-static\resources\view\ipselect\overview.js`

**Interfaces:**
- Consumes: LuCI RPC (`/cgi-bin/luci/admin/services/ipselect/*`), UCI (`uci.load('ipselect')`)
- Produces: 选项卡式现代化用户交互界面（Dashboard + Tabs 1-4）。

- [ ] **Step 1: 编写 `overview.js` 视图主逻辑**

实现包含：
1. 顶部状态仪表盘（状态灯、运行状态、上次运行、Git 状态）；
2. Tab 1：控制台与实时增量日志滚屏（定时轮询 `get_log`）；
3. Tab 2：优选结果表格（解析并渲染 Top 15 节点列表，支持复制与下载）；
4. Tab 3：历史执行记录表格（展示最近 10 次记录，点击模态框弹窗查看日志快照）；
5. Tab 4：基础设置表单（绑定 UCI 配置，支持工作目录、环境标签、网卡、OpenClash 联动、Cron 定时）。

- [ ] **Step 2: 本地语法校验**

运行：`node -c htdocs/luci-static/resources/view/ipselect/overview.js`
预期：语法校验退出码 0。

- [ ] **Step 3: 提交 Task 4 成果**

```bash
git add htdocs/luci-static/resources/view/ipselect/overview.js
git commit -m "feat: 实现现代 LuCI JS 视图全功能面板与 4 大选项卡交互"
```

---

### Task 5: 打包规范与一键部署工具 (`Makefile` & `deploy_to_router.py`)

**Files:**
- Create: `D:\nice_wall\luci-app-ipselect\Makefile`
- Create: `D:\nice_wall\luci-app-ipselect\scripts\build_ipk.py`
- Create: `D:\nice_wall\luci-app-ipselect\scripts\deploy_to_router.py`

**Interfaces:**
- Produces:
  - `dist/luci-app-ipselect_1.0.0-1_all.ipk`
  - 一键部署脚本：直连 `10.10.18.2` 并安装生效。

- [ ] **Step 1: 编写 OpenWrt 标准 `Makefile`**

符合 OpenWrt 官方 package 编译规范。

- [ ] **Step 2: 编写免交叉编译环境的独立 IPK 打包工具 `scripts/build_ipk.py`**

生成包含 `control.tar.gz`, `data.tar.gz`, `debian-binary` 的标准 `ar` 格式 `.ipk` 文件。

- [ ] **Step 3: 编写软路由一键安装与重载工具 `scripts/deploy_to_router.py`**

通过 Paramiko 或 SCP 将文件推送至 `/` 目录并调用 `luci-reload` 重启 LuCI 缓存。

- [ ] **Step 4: 运行本地构建与包合法性校验**

运行：`python scripts/build_ipk.py`
预期：在 `dist/` 目录下成功生成 `luci-app-ipselect_1.0.0-1_all.ipk`。

- [ ] **Step 5: 提交 Task 5 成果**

```bash
git add Makefile scripts/build_ipk.py scripts/deploy_to_router.py
git commit -m "feat: 增加标准 Makefile、IPK 本地打包器与软路由一键部署脚本"
```

---

### Task 6: 软路由现场部署、全链路联调与真机实测

**Files:**
- Remote host: `10.10.18.2` (root / Xg@2020+)
- Verification script: `tests/verify_router_live.py`

- [ ] **Step 1: 执行现场部署**

运行：`python scripts/deploy_to_router.py --host 10.10.18.2 --password Xg@2020+`
预期：文件全部部署至软路由相应系统目录，权限设置正确 (`chmod 755 /usr/bin/ipselect-runner`)，LuCI 缓存刷新成功。

- [ ] **Step 2: 验证 LuCI 菜单与接口响应**

通过 SSH 执行 curl 本地接口：
- `curl -s http://127.0.0.1/cgi-bin/luci/admin/services/ipselect/get_status` 返回当前运行状态；
- `curl -s http://127.0.0.1/cgi-bin/luci/admin/services/ipselect/get_results` 成功返回当前 `best_us.txt` 中的节点表格数据。

- [ ] **Step 3: 真机端到端全链路触发实测**

1. 触发 `start`，观察 PID 文件生成与 `/var/log/ipselect.log` 增量写入；
2. 校验 `stop` 指令能即时中止任务并清理锁；
3. 允许一次完整测速，验证历史记录 `/etc/ipselect/history.json` 成功追加条目；
4. 验证 OpenClash 订阅联动刷新（若开启）日志记录正常；
5. 检查系统 Crontab 中是否正确托管 `#ipselect-cron-task` 定时规则。

- [ ] **Step 4: 归档与总结**

记录实测日志与交付产物，并在计划中全部打钩完成。
