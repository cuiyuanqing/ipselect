#!/bin/sh
# tests/test_runner.sh - 测试 ipselect-runner 的核心调度与历史管理逻辑
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER="$REPO_ROOT/root/usr/bin/ipselect-runner"

if [ ! -f "$RUNNER" ]; then
    echo "❌ 找不到 $RUNNER"
    exit 1
fi
chmod +x "$RUNNER"

TMP_DIR="/tmp/test_ipselect"
if command -v cygpath >/dev/null 2>&1; then
    TMP_DIR="$(cygpath -m "$TMP_DIR")"
fi
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR/var/run" "$TMP_DIR/var/log" "$TMP_DIR/etc/ipselect/logs" "$TMP_DIR/root/ipselect/scripts"

export TEST_ROOT="$TMP_DIR"

# 1. 模拟 UCI
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

# 2. 模拟优选输出文件
cat << 'EOF' > "$TMP_DIR/root/ipselect/best_us.txt"
104.16.89.201:443#公司-CF官方-SJC
162.158.21.45:443#公司-CF官方-LAX
EOF

# 3. 模拟 filter_us_nodes.py
cat << 'EOF' > "$TMP_DIR/root/ipselect/scripts/filter_us_nodes.py"
#!/usr/bin/env python3
import sys, time, os

slow_flag = os.environ.get("TEST_MOCK_SLOW", "0")
if slow_flag == "1":
    time.sleep(3)

out_file = "best_us.txt"
for i in range(len(sys.argv)):
    if sys.argv[i] == "--output" and i + 1 < len(sys.argv):
        out_file = sys.argv[i + 1]

with open(out_file, "w", encoding="utf-8") as f:
    f.write("# 优选结果节点\n")
    f.write("104.16.89.201:443#公司-CF官方-SJC\n")
    f.write("162.158.21.45:443#公司-CF官方-LAX\n")
    f.write("104.17.10.1:443#公司-CF官方-SFO\n")

print("Mock 节点筛选完成，写入 3 个节点")
EOF
chmod +x "$TMP_DIR/root/ipselect/scripts/filter_us_nodes.py"

echo "=== [1/9] 测试初始状态 (status) ==="
STATUS_OUT=$("$RUNNER" status)
echo "Status output: $STATUS_OUT"
echo "$STATUS_OUT" | grep -q '"running": false' || (echo "❌ 初始 running 应为 false" && exit 1)
echo "$STATUS_OUT" | grep -q '"pid": 0' || (echo "❌ 初始 pid 应为 0" && exit 1)
echo "$STATUS_OUT" | grep -q '"last_run": "从未运行"' || (echo "❌ 初始 last_run 应为 从未运行" && exit 1)
echo "✅ 初始状态测试通过"

echo "=== [2/9] 测试优选结果查询 (results) ==="
RESULTS_OUT=$("$RUNNER" results)
echo "Results output: $RESULTS_OUT"
echo "$RESULTS_OUT" | grep -q '"file": "best_us.txt"' || (echo "❌ 结果未返回正确 file" && exit 1)
echo "$RESULTS_OUT" | grep -q '"count": 2' || (echo "❌ 初始节点数应为 2" && exit 1)
echo "$RESULTS_OUT" | grep -q '104.16.89.201' || (echo "❌ 结果缺少节点 IP" && exit 1)
echo "✅ 结果解析测试通过"

echo "=== [3/9] 测试任务启动与运行锁 (start & status & 重复启动互斥) ==="
export TEST_MOCK_SLOW="1"
START_OUT=$("$RUNNER" start manual)
echo "Start output: $START_OUT"
echo "$START_OUT" | grep -q '"success": true' || (echo "❌ 启动失败" && exit 1)

# 短暂等待进程启动写入 PID
sleep 0.2
RUNNING_STATUS=$("$RUNNER" status)
echo "Running status: $RUNNING_STATUS"
echo "$RUNNING_STATUS" | grep -q '"running": true' || (echo "❌ 运行时 running 应为 true" && exit 1)

# 测试并发锁互斥 (重复调用 start 应失败)
DUP_START=$("$RUNNER" start manual 2>&1 || true)
echo "Duplicate start: $DUP_START"
echo "$DUP_START" | grep -q '"success": false' || (echo "❌ 重复启动未被阻止" && exit 1)
echo "✅ 任务启动与互斥锁测试通过"

echo "=== [4/9] 测试任务中止与 terminated 历史记录归档 (stop) ==="
STOP_OUT=$("$RUNNER" stop)
echo "Stop output: $STOP_OUT"
echo "$STOP_OUT" | grep -q '"success": true' || (echo "❌ 停止失败" && exit 1)

sleep 0.3
STOPPED_STATUS=$("$RUNNER" status)
echo "Stopped status: $STOPPED_STATUS"
echo "$STOPPED_STATUS" | grep -q '"running": false' || (echo "❌ 停止后 running 应为 false" && exit 1)

LOG_OUT=$("$RUNNER" log)
echo "$LOG_OUT" | grep -q "任务被手动中止" || (echo "❌ 日志未记录中止状态" && exit 1)

# 验证 stop 生成了 status 为 terminated 的历史记录
STOP_HIST=$("$RUNNER" history)
echo "History after stop: $STOP_HIST"
echo "$STOP_HIST" | grep -q '"status": "terminated"' || (echo "❌ stop 未记录 terminated 历史" && exit 1)

# 验证通过 history-log 可获取中止日志
TERM_TASK_ID=$(echo "$STOP_HIST" | grep -o '"id": "[^"]*"' | head -n 1 | cut -d'"' -f4)
TERM_LOG_OUT=$("$RUNNER" history-log "$TERM_TASK_ID")
echo "$TERM_LOG_OUT" | grep -q "任务被手动中止" || (echo "❌ 中止归档日志内容不匹配" && exit 1)
echo "✅ 任务中止与 terminated 历史归档测试通过"

echo "=== [5/9] 测试日志读取与清空 (log & clear-log) ==="
CLEAR_OUT=$("$RUNNER" clear-log)
echo "Clear log output: $CLEAR_OUT"
echo "$CLEAR_OUT" | grep -q '"success": true' || (echo "❌ 清理日志失败" && exit 1)

LOG_EMPTY=$("$RUNNER" log)
if [ -n "$LOG_EMPTY" ]; then
    echo "❌ 清空后日志应为空"
    exit 1
fi
echo "✅ 日志清空测试通过"

echo "=== [6/9] 测试完整任务执行与历史归档 (start fast -> history) ==="
export TEST_MOCK_SLOW="0"
"$RUNNER" start manual
# 等待脚本执行完毕 (通常 < 1s)
for i in $(seq 1 30); do
    if [ "$("$RUNNER" status | grep -o '"running": [^,]*' | cut -d' ' -f2)" = "false" ]; then
        break
    fi
    sleep 0.2
done

# 验证状态与历史
FINAL_STATUS=$("$RUNNER" status)
echo "Final status: $FINAL_STATUS"
echo "$FINAL_STATUS" | grep -q '"running": false' || (echo "❌ 任务超时未完成" && exit 1)
echo "$FINAL_STATUS" | grep -v -q '"last_run": "从未运行"' || (echo "❌ last_run 应更新" && exit 1)

HISTORY_OUT=$("$RUNNER" history)
echo "History output: $HISTORY_OUT"
echo "$HISTORY_OUT" | grep -q '"status": "success"' || (echo "❌ 历史记录状态应为 success" && exit 1)
echo "$HISTORY_OUT" | grep -q '"nodeCount": 3' || (echo "❌ 节点数应更新为 3" && exit 1)

# 提取 ID 并测试 history-log
TASK_ID=$(echo "$HISTORY_OUT" | grep -o '"id": "[^"]*"' | head -n 1 | cut -d'"' -f4)
echo "Extracted Task ID: $TASK_ID"
HLOG_OUT=$("$RUNNER" history-log "$TASK_ID")
echo "$HLOG_OUT" | grep -q "节点筛选测速完成" || (echo "❌ 历史日志内容不匹配" && exit 1)
echo "✅ 完整执行与历史归档测试通过"

echo "=== [7/9] 测试历史记录条数上限 (10条截断) 与淘汰旧日志物理清理 ==="
# 构造具有 12 条记录的历史文件及对应的 12 个日志文件
python3 -c "
import json, os
log_dir = '$TMP_DIR/etc/ipselect/logs'
records = []
for i in range(12):
    log_file = os.path.join(log_dir, f'history_20261010-0000{i:02d}.log')
    with open(log_file, 'w') as f:
        f.write(f'Log content for run {i}\n')
    records.insert(0, {
        'id': f'20261010-0000{i:02d}',
        'timestamp': f'2026-10-10 00:00:{i:02d}',
        'trigger': 'manual',
        'duration': 1,
        'status': 'success',
        'nodeCount': 3,
        'gitStatus': 'skipped',
        'openclashStatus': 'skipped',
        'logFile': log_file
    })
with open('$TMP_DIR/etc/ipselect/history.json', 'w') as f:
    json.dump(records, f)
"
# 再次触发一次执行以触发滚动淘汰
"$RUNNER" start cron
for i in $(seq 1 30); do
    if [ "$("$RUNNER" status | grep -o '"running": [^,]*' | cut -d' ' -f2)" = "false" ]; then
        break
    fi
    sleep 0.2
done

HIST_COUNT=$(python3 -c "import json; print(len(json.load(open('$TMP_DIR/etc/ipselect/history.json'))))")
echo "History record count: $HIST_COUNT"
if [ "$HIST_COUNT" -ne 10 ]; then
    echo "❌ 历史记录条数应严格限制为 10，实际为: $HIST_COUNT"
    exit 1
fi

# 验证被淘汰的旧日志文件已被物理删除，保留的日志仍然存在
LOG_FILES_COUNT=$(ls -1 "$TMP_DIR/etc/ipselect/logs"/history_*.log | wc -l)
echo "Physical log files count: $LOG_FILES_COUNT"
if [ "$LOG_FILES_COUNT" -ne 10 ]; then
    echo "❌ 物理日志文件数量应淘汰至 10，实际为: $LOG_FILES_COUNT"
    exit 1
fi
if [ -f "$TMP_DIR/etc/ipselect/logs/history_20261010-000000.log" ]; then
    echo "❌ 最旧的淘汰日志 history_20261010-000000.log 应已被物理删除"
    exit 1
fi
echo "✅ 历史记录 10 条上限与物理日志滚动淘汰测试通过"

echo "=== [8/9] 测试增量日志读取 (log <offset>) 与文件截断重置回退 ==="
echo "Line 1" > "$TMP_DIR/var/log/ipselect.log"
OFFSET=$(wc -c < "$TMP_DIR/var/log/ipselect.log" | tr -d ' ')
echo "Line 2" >> "$TMP_DIR/var/log/ipselect.log"

DELTA_LOG=$("$RUNNER" log "$OFFSET")
echo "Delta log: [$DELTA_LOG]"
echo "$DELTA_LOG" | grep -q "Line 2" || (echo "❌ 增量日志未读到 Line 2" && exit 1)
echo "$DELTA_LOG" | grep -v -q "Line 1" || (echo "❌ 增量日志不应包含 Line 1" && exit 1)

TOTAL_OFFSET=$(wc -c < "$TMP_DIR/var/log/ipselect.log" | tr -d ' ')
EMPTY_LOG=$("$RUNNER" log "$TOTAL_OFFSET")
if [ -n "$EMPTY_LOG" ]; then
    echo "❌ 偏移量等于文件大小时应返回空字符串"
    exit 1
fi

# 测试文件被截断/覆盖时 (OFFSET > TOTAL_SIZE)，应重置并输出全部新内容
echo "Short new file" > "$TMP_DIR/var/log/ipselect.log"
RESET_LOG=$("$RUNNER" log 99999)
echo "Reset log output: [$RESET_LOG]"
echo "$RESET_LOG" | grep -q "Short new file" || (echo "❌ OFFSET > TOTAL_SIZE 时未正确回退输出新内容" && exit 1)
echo "✅ 增量日志读取与文件截断重置测试通过"

echo "=== [9/9] 测试边界条件 (空闲 stop, 不存在的 history-log, 空 results, 未知参数) ==="
IDLE_STOP=$("$RUNNER" stop)
echo "$IDLE_STOP" | grep -q '"success": false' || (echo "❌ 空闲状态 stop 应返回 false" && exit 1)

MISSING_HLOG=$("$RUNNER" history-log "non_existent_id")
echo "$MISSING_HLOG" | grep -q "日志不存在或已被清除" || (echo "❌ 不存在的 history-log 应提示清除" && exit 1)

rm -f "$TMP_DIR/root/ipselect/best_us.txt"
EMPTY_RESULTS=$("$RUNNER" results)
echo "$EMPTY_RESULTS" | grep -q '"update_time": "从未生成"' || (echo "❌ 结果文件不存在时 update_time 应为 从未生成" && exit 1)

echo "=== [10/10] 测试 test-github 子命令 (未配置返回与参数检测) ==="
NO_CFG_OUT=$("$RUNNER" test-github)
echo "No config output: $NO_CFG_OUT"
echo "$NO_CFG_OUT" | grep -q '"success": false' || (echo "❌ 未配置时 test-github 应返回 false" && exit 1)
echo "$NO_CFG_OUT" | grep -q "未配置 GitHub 仓库名或访问令牌" || (echo "❌ 未配置提示信息不正确" && exit 1)
echo "✅ test-github 未配置验证测试通过"

echo "=========================================="
echo "🎉 所有 10 项测试全部通过！"
echo "=========================================="
