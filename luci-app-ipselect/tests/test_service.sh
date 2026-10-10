#!/bin/sh
# tests/test_service.sh - 测试 UCI 默认配置合法性与 init.d 服务的生命周期及 Crontab 托管
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$REPO_ROOT/root/etc/config/ipselect"
SERVICE_SCRIPT="$REPO_ROOT/root/etc/init.d/ipselect"

echo "=========================================="
echo "开始运行 luci-app-ipselect 服务管理测试"
echo "=========================================="

if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ 找不到配置文件: $CONFIG_FILE"
    exit 1
fi

if [ ! -f "$SERVICE_SCRIPT" ]; then
    echo "❌ 找不到服务脚本: $SERVICE_SCRIPT"
    exit 1
fi
chmod +x "$SERVICE_SCRIPT"

TMP_DIR="/tmp/test_ipselect_service"
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR/etc/crontabs" "$TMP_DIR/bin" "$TMP_DIR/var/run"

TEST_CRON_FILE="$TMP_DIR/etc/crontabs/root"
TEST_RUNNER_LOG="$TMP_DIR/mock_runner_calls.log"
TEST_CRON_RELOAD_LOG="$TMP_DIR/mock_cron_reload.log"

export TEST_CRON_FILE
export TEST_RUNNER_LOG
export TEST_CRON_RELOAD_LOG
export TEST_RUNNER="$TMP_DIR/bin/ipselect-runner"
export TEST_CRON_RELOAD_CMD="$TMP_DIR/bin/mock_cron_reload"
export TEST_UCI_CMD="$TMP_DIR/bin/uci"

# 1. 模拟 /usr/bin/ipselect-runner
cat << 'EOF' > "$TEST_RUNNER"
#!/bin/sh
echo "$@" >> "$TEST_RUNNER_LOG"
case "$1" in
    stop)
        exit 0
        ;;
    start)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
EOF
chmod +x "$TEST_RUNNER"

# 2. 模拟 cron reload
cat << 'EOF' > "$TEST_CRON_RELOAD_CMD"
#!/bin/sh
echo "reload" >> "$TEST_CRON_RELOAD_LOG"
exit 0
EOF
chmod +x "$TEST_CRON_RELOAD_CMD"

# 3. 模拟 UCI 命令（默认读取真实 root/etc/config/ipselect，支持环境变量覆盖）
cat << EOF > "$TEST_UCI_CMD"
#!/bin/sh
case "\$1" in
    get)
        OPT="\${2##ipselect.config.}"
        case "\$OPT" in
            enabled) [ -n "\$MOCK_ENABLED" ] && echo "\$MOCK_ENABLED" && exit 0 ;;
            cron_enabled) [ -n "\$MOCK_CRON_ENABLED" ] && echo "\$MOCK_CRON_ENABLED" && exit 0 ;;
            cron_hour) [ -n "\$MOCK_CRON_HOUR" ] && echo "\$MOCK_CRON_HOUR" && exit 0 ;;
            cron_minute) [ -n "\$MOCK_CRON_MINUTE" ] && echo "\$MOCK_CRON_MINUTE" && exit 0 ;;
            cron_mode) [ -n "\$MOCK_CRON_MODE" ] && echo "\$MOCK_CRON_MODE" && exit 0 ;;
            cron_expression) [ "\${MOCK_CRON_EXPRESSION+x}" = "x" ] && echo "\$MOCK_CRON_EXPRESSION" && exit 0 ;;
            workdir) [ -n "\$MOCK_WORKDIR" ] && echo "\$MOCK_WORKDIR" && exit 0 ;;
            env_tag) [ -n "\$MOCK_ENV_TAG" ] && echo "\$MOCK_ENV_TAG" && exit 0 ;;
            output_file) [ -n "\$MOCK_OUTPUT_FILE" ] && echo "\$MOCK_OUTPUT_FILE" && exit 0 ;;
            interface) [ -n "\$MOCK_INTERFACE" ] && echo "\$MOCK_INTERFACE" && exit 0 ;;
            auto_push) [ -n "\$MOCK_AUTO_PUSH" ] && echo "\$MOCK_AUTO_PUSH" && exit 0 ;;
            github_repo) [ -n "\$MOCK_GITHUB_REPO" ] && echo "\$MOCK_GITHUB_REPO" && exit 0 ;;
            github_token) [ -n "\$MOCK_GITHUB_TOKEN" ] && echo "\$MOCK_GITHUB_TOKEN" && exit 0 ;;
            github_branch) [ -n "\$MOCK_GITHUB_BRANCH" ] && echo "\$MOCK_GITHUB_BRANCH" && exit 0 ;;
            github_user) [ -n "\$MOCK_GITHUB_USER" ] && echo "\$MOCK_GITHUB_USER" && exit 0 ;;
            github_email) [ -n "\$MOCK_GITHUB_EMAIL" ] && echo "\$MOCK_GITHUB_EMAIL" && exit 0 ;;
            auto_clone) [ -n "\$MOCK_AUTO_CLONE" ] && echo "\$MOCK_AUTO_CLONE" && exit 0 ;;
            openclash_sync) [ -n "\$MOCK_OPENCLASH_SYNC" ] && echo "\$MOCK_OPENCLASH_SYNC" && exit 0 ;;
            openclash_provider) [ -n "\$MOCK_OPENCLASH_PROVIDER" ] && echo "\$MOCK_OPENCLASH_PROVIDER" && exit 0 ;;
        esac
        VAL=\$(sed -n "s/^[[:space:]]*option[[:space:]]\+\$OPT[[:space:]]\+['\"]\([^'\"]*\)['\"].*/\1/p" "$CONFIG_FILE" | head -n 1)
        if [ -n "\$VAL" ]; then
            echo "\$VAL"
            exit 0
        fi
        exit 1
        ;;
    *)
        exit 1
        ;;
esac
EOF
chmod +x "$TEST_UCI_CMD"

echo "=== [1/8] 测试默认配置文件 root/etc/config/ipselect 的完整性与合法性 ==="
# 验证无 CRLF 换行
if grep -q $'\r' "$CONFIG_FILE"; then
    echo "❌ 配置文件包含 Windows CRLF 换行符，必须为 LF！"
    exit 1
fi
# 验证核心字段与默认值
check_uci_opt() {
    local opt="$1"
    local expected="$2"
    local val
    val=$("$TEST_UCI_CMD" get "ipselect.config.$opt")
    if [ "$val" != "$expected" ]; then
        echo "❌ 配置项 $opt 预期为 '$expected'，实际为 '$val'"
        exit 1
    fi
}
check_uci_opt "enabled" "1"
check_uci_opt "workdir" "/root/ipselect"
check_uci_opt "env_tag" "公司"
check_uci_opt "output_file" "best_us.txt"
check_uci_opt "interface" "br-lan"
check_uci_opt "auto_push" "1"
check_uci_opt "openclash_sync" "1"
check_uci_opt "openclash_provider" "datree"
check_uci_opt "cron_enabled" "1"
check_uci_opt "cron_expression" "30 4 * * *"
check_uci_opt "github_repo" "cuiyuanqing/ipselect"
check_uci_opt "github_branch" "main"
check_uci_opt "github_user" "cuiyuanqing"
check_uci_opt "github_email" "759666247@qq.com"
check_uci_opt "auto_clone" "1"
echo "✅ 默认配置文件语法与字段完整性校验通过"

echo "=== [2/8] 测试服务控制脚本语法与规范 (sh -n & rc.common 规范) ==="
sh -n "$SERVICE_SCRIPT"
if grep -q $'\r' "$SERVICE_SCRIPT"; then
    echo "❌ 服务脚本包含 Windows CRLF 换行符，必须为 LF！"
    exit 1
fi
grep -q "START=95" "$SERVICE_SCRIPT" || (echo "❌ 缺少 START=95" && exit 1)
grep -q "STOP=10" "$SERVICE_SCRIPT" || (echo "❌ 缺少 STOP=10" && exit 1)
grep -q "EXTRA_COMMANDS=\"sync_cron\"" "$SERVICE_SCRIPT" || (echo "❌ 缺少 EXTRA_COMMANDS 声明" && exit 1)
echo "✅ 服务控制脚本语法校验与配置元数据检查通过"

RUN_SERVICE() {
    sh "$SERVICE_SCRIPT" "$@"
}

echo "=== [3/8] 测试默认启用状态下 sync_cron 与 start 写入 Crontab ==="
rm -f "$TEST_CRON_FILE" "$TEST_CRON_RELOAD_LOG"
RUN_SERVICE sync_cron

if [ ! -f "$TEST_CRON_FILE" ]; then
    echo "❌ sync_cron 未生成 Crontab 文件: $TEST_CRON_FILE"
    exit 1
fi

CRON_CONTENT=$(cat "$TEST_CRON_FILE")
echo "Crontab content: $CRON_CONTENT"
echo "$CRON_CONTENT" | grep -q "#ipselect-cron-task" || (echo "❌ 未找到 #ipselect-cron-task 标记" && exit 1)
echo "$CRON_CONTENT" | grep -q "30 4 \* \* \*" || (echo "❌ 默认时间应为 4:30 (30 4 * * *)" && exit 1)
echo "$CRON_CONTENT" | grep -q "$TEST_RUNNER start cron" || (echo "❌ 未正确配置 runner 触发命令" && exit 1)

# 验证 cron reload 被调用
[ -s "$TEST_CRON_RELOAD_LOG" ] || (echo "❌ 未触发 cron reload" && exit 1)
echo "✅ 默认配置下 Crontab 托管写入测试通过"

echo "=== [4/8] 测试幂等性与去重机制 (多次调用不产生重复行) ==="
RUN_SERVICE sync_cron
RUN_SERVICE sync_cron
RUN_SERVICE start

TASK_LINE_COUNT=$(grep -c "#ipselect-cron-task" "$TEST_CRON_FILE" || true)
echo "Cron task line count: $TASK_LINE_COUNT"
if [ "$TASK_LINE_COUNT" -ne 1 ]; then
    echo "❌ 多次调用产生重复条目，条目数应严格为 1，实际为 $TASK_LINE_COUNT"
    exit 1
fi
echo "✅ 幂等性与去重机制测试通过"

echo "=== [5/8] 测试自定义定时时间与 reload 同步 (cron_expression 与 回退模式) ==="
# 5.1 测试 cron_expression 自定义多时间点 (如 3, 8, 9, 12 点)
export MOCK_CRON_EXPRESSION="0 3,8,9,12 * * *"
RUN_SERVICE reload
CUSTOM_CRON=$(cat "$TEST_CRON_FILE")
echo "Custom crontab content (expression): $CUSTOM_CRON"
echo "$CUSTOM_CRON" | grep -q "0 3,8,9,12 \* \* \*" || (echo "❌ 自定义表达式 0 3,8,9,12 * * * 未生效" && exit 1)

# 5.2 测试 cron_expression 为空时无缝回退到 cron_hour/cron_minute
export MOCK_CRON_EXPRESSION=""
export MOCK_CRON_HOUR="2"
export MOCK_CRON_MINUTE="15"
RUN_SERVICE reload
CUSTOM_CRON_FALLBACK=$(cat "$TEST_CRON_FILE")
echo "Custom crontab content (fallback): $CUSTOM_CRON_FALLBACK"
echo "$CUSTOM_CRON_FALLBACK" | grep -q "15 2 \* \* \*" || (echo "❌ 回退自定义时间 2:15 未生效" && exit 1)
unset MOCK_CRON_EXPRESSION MOCK_CRON_HOUR MOCK_CRON_MINUTE
echo "✅ 自定义时间更新与 reload 同步测试通过"

echo "=== [6/8] 测试定时任务禁用与总开关禁用 (移除 Crontab 条目) ==="
# 6.1 cron_enabled=0 禁用定时
export MOCK_CRON_ENABLED="0"
RUN_SERVICE sync_cron
if grep -q "#ipselect-cron-task" "$TEST_CRON_FILE" 2>/dev/null; then
    echo "❌ cron_enabled=0 时未移除定时任务"
    exit 1
fi
unset MOCK_CRON_ENABLED

# 重新启用验证恢复
RUN_SERVICE sync_cron
grep -q "#ipselect-cron-task" "$TEST_CRON_FILE" || (echo "❌ 重新启用时未恢复定时任务" && exit 1)

# 6.2 enabled=0 总开关关闭
export MOCK_ENABLED="0"
RUN_SERVICE sync_cron
if grep -q "#ipselect-cron-task" "$TEST_CRON_FILE" 2>/dev/null; then
    echo "❌ enabled=0 时未移除定时任务"
    exit 1
fi
unset MOCK_ENABLED
echo "✅ 定时禁用与总开关禁用清理测试通过"

echo "=== [7/8] 测试与其他系统定时任务共存 (不破坏非 ipselect 条目) ==="
cat << 'EOF' > "$TEST_CRON_FILE"
0 2 * * * /usr/bin/sysupgrade-backup >/dev/null 2>&1
*/10 * * * * /usr/sbin/logrotate
EOF

# 恢复默认设置并同步
RUN_SERVICE sync_cron
grep -q "/usr/bin/sysupgrade-backup" "$TEST_CRON_FILE" || (echo "❌ 系统备份任务被误删" && exit 1)
grep -q "/usr/sbin/logrotate" "$TEST_CRON_FILE" || (echo "❌ 日志轮转任务被误删" && exit 1)
grep -q "#ipselect-cron-task" "$TEST_CRON_FILE" || (echo "❌ 未成功追加 ipselect 任务" && exit 1)

# 验证调用 stop 时仅清理 ipselect 任务，保留其他系统任务
RUN_SERVICE stop
grep -q "/usr/bin/sysupgrade-backup" "$TEST_CRON_FILE" || (echo "❌ stop 操作误删了系统备份任务" && exit 1)
grep -q "/usr/sbin/logrotate" "$TEST_CRON_FILE" || (echo "❌ stop 操作误删了日志轮转任务" && exit 1)
if grep -q "#ipselect-cron-task" "$TEST_CRON_FILE"; then
    echo "❌ stop 操作未清理 #ipselect-cron-task"
    exit 1
fi

# 验证 stop 调用了 runner 的 stop 指令
grep -q "stop" "$TEST_RUNNER_LOG" || (echo "❌ stop 未调用 runner stop 指令" && exit 1)
echo "✅ 多任务共存与 stop 独立清理测试通过"

echo "=== [8/8] 测试仿真 OpenWrt rc.common 环境与边界行为 ==="
# 构造仿真 OpenWrt rc.common 解释器环境
cat << 'EOF' > "$TMP_DIR/mock_rc_common.sh"
#!/bin/sh
initscript="$1"
action="$2"
shift 2

# 仿真 OpenWrt 函数
config_load() { :; }
config_get_bool() {
    local _v="$1" _s="$2" _o="$3" _d="${4:-0}"
    local val
    val=$("$TEST_UCI_CMD" get "ipselect.${_s}.${_o}" 2>/dev/null || echo "$_d")
    case "$val" in
        1|on|true|yes) eval "$_v=1" ;;
        *) eval "$_v=0" ;;
    esac
}
config_get() {
    local _v="$1" _s="$2" _o="$3" _d="$4"
    local val
    val=$("$TEST_UCI_CMD" get "ipselect.${_s}.${_o}" 2>/dev/null || echo "$_d")
    eval "$_v=\"\$val\""
}

. "$initscript"
case "$action" in
    start) start "$@" ;;
    stop) stop "$@" ;;
    restart) stop "$@"; start "$@" ;;
    reload) reload "$@" ;;
    sync_cron) sync_cron "$@" ;;
    *) echo "Unknown action"; exit 1 ;;
esac
EOF
chmod +x "$TMP_DIR/mock_rc_common.sh"

# 在仿真 rc.common 下执行 start
/bin/sh "$TMP_DIR/mock_rc_common.sh" "$SERVICE_SCRIPT" start
grep -q "#ipselect-cron-task" "$TEST_CRON_FILE" || (echo "❌ 仿真 rc.common 下 start 失败" && exit 1)

# 在仿真 rc.common 下执行 stop
/bin/sh "$TMP_DIR/mock_rc_common.sh" "$SERVICE_SCRIPT" stop
if grep -q "#ipselect-cron-task" "$TEST_CRON_FILE"; then
    echo "❌ 仿真 rc.common 下 stop 失败"
    exit 1
fi

# 测试非法参数返回用法提示且退出码非 0
INVALID_RET=0
RUN_SERVICE invalid_action >/dev/null 2>&1 || INVALID_RET=$?
if [ "$INVALID_RET" -eq 0 ]; then
    echo "❌ 非法命令退出码应非 0"
    exit 1
fi
echo "✅ 仿真 OpenWrt rc.common 环境与边界行为测试通过"

echo "=========================================="
echo "🎉 所有 8 项服务测试全部通过！"
echo "=========================================="
