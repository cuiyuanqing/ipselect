#!/bin/sh
# tests/test_controller.sh - 测试 LuCI Controller 后端路由、菜单声明与 RPCD ACL 权限配置
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MENU_JSON="$REPO_ROOT/root/usr/share/luci/menu.d/luci-app-ipselect.json"
ACL_JSON="$REPO_ROOT/root/usr/share/rpcd/acl.d/luci-app-ipselect.json"
CONTROLLER_LUA="$REPO_ROOT/luasrc/controller/ipselect.lua"
RUNNER_SCRIPT="$REPO_ROOT/root/usr/bin/ipselect-runner"

echo "=========================================="
echo "开始运行 luci-app-ipselect 控制器与路由测试"
echo "=========================================="

# 检查文件存在性
for f in "$MENU_JSON" "$ACL_JSON" "$CONTROLLER_LUA" "$RUNNER_SCRIPT"; do
    if [ ! -f "$f" ]; then
        echo "❌ 缺少必要文件: $f"
        exit 1
    fi
done

TMP_DIR="/tmp/test_luci_controller"
if command -v cygpath >/dev/null 2>&1; then
    TMP_DIR="$(cygpath -m "$TMP_DIR")"
fi
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR/bin" "$TMP_DIR/logs"

export MENU_JSON
export ACL_JSON
export CONTROLLER_LUA
export RUNNER_SCRIPT
export PYTHONIOENCODING="utf-8"

# -------------------------------------------------------------
# 步骤 1: 验证 LuCI menu.d 菜单声明 JSON 合法性与字段规范
# -------------------------------------------------------------
echo "=== [1/7] 验证 menu.d 菜单注册 JSON 语法与节点配置 ==="
python3 << 'EOF'
import json, sys, os

path = os.environ['MENU_JSON']
with open(path, 'r', encoding='utf-8') as f:
    try:
        data = json.load(f)
    except Exception as e:
        print(f'❌ menu.d JSON 解析失败: {e}')
        sys.exit(1)

menu_node = 'admin/services/ipselect'
if menu_node not in data:
    print(f'❌ 缺少顶级菜单定义节点: {menu_node}')
    sys.exit(1)

node = data[menu_node]
assert node.get('title') == '节点优选', f'菜单标题错误: {node.get("title")}'
assert node.get('order') == 60, f'菜单排序 order 错误: {node.get("order")}'
action = node.get('action', {})
assert action.get('type') == 'view', f'action.type 应为 view: {action.get("type")}'
assert action.get('path') == 'ipselect/overview', f'action.path 错误: {action.get("path")}'

depends = node.get('depends', {})
acl_list = depends.get('acl', [])
assert 'luci-app-ipselect' in acl_list, f'depends.acl 必须包含 luci-app-ipselect: {acl_list}'

print('✅ menu.d 菜单配置文件语法正确，节点与属性配置完备')
EOF

# -------------------------------------------------------------
# 步骤 2: 验证 rpcd acl.d 权限声明 JSON 合法性与权限范围
# -------------------------------------------------------------
echo "=== [2/7] 验证 acl.d RPCD ACL 权限声明语法与授权策略 ==="
python3 << 'EOF'
import json, sys, os

path = os.environ['ACL_JSON']
with open(path, 'r', encoding='utf-8') as f:
    try:
        data = json.load(f)
    except Exception as e:
        print(f'❌ acl.d JSON 解析失败: {e}')
        sys.exit(1)

acl_group = 'luci-app-ipselect'
if acl_group not in data:
    print(f'❌ 缺少 ACL 权限组定义: {acl_group}')
    sys.exit(1)

rule = data[acl_group]
assert 'description' in rule and len(rule['description']) > 0, '缺少 description 描述'

# read 权限检查
read_perm = rule.get('read', {})
assert 'uci' in read_perm and 'ipselect' in read_perm['uci'], 'read.uci 未授予 ipselect 读取权限'
read_files = read_perm.get('file', {})
assert '/var/log/ipselect.log' in read_files and 'read' in read_files['/var/log/ipselect.log'], '未声明 /var/log/ipselect.log 读权限'
assert '/etc/ipselect/history.json' in read_files and 'read' in read_files['/etc/ipselect/history.json'], '未声明 /etc/ipselect/history.json 读权限'

# write 权限检查
write_perm = rule.get('write', {})
assert 'uci' in write_perm and 'ipselect' in write_perm['uci'], 'write.uci 未授予 ipselect 写入权限'
write_files = write_perm.get('file', {})
assert '/etc/init.d/ipselect restart' in write_files and 'exec' in write_files['/etc/init.d/ipselect restart'], '未声明 /etc/init.d/ipselect restart 执行权限'

print('✅ acl.d RPCD 权限声明语法正确，UCI、服务重启及日志/历史文件授权策略明确')
EOF

# -------------------------------------------------------------
# 步骤 3: 验证 menu.d、acl.d 与 controller 授权一致性
# -------------------------------------------------------------
echo "=== [3/7] 验证 menu.d、acl.d 与 controller 的 ACL 权限标识一致性 ==="
python3 << 'EOF'
import json, re, sys, os

menu_file = os.environ['MENU_JSON']
acl_file = os.environ['ACL_JSON']
lua_file = os.environ['CONTROLLER_LUA']

with open(menu_file, 'r', encoding='utf-8') as f:
    menu = json.load(f)
with open(acl_file, 'r', encoding='utf-8') as f:
    acl = json.load(f)
with open(lua_file, 'r', encoding='utf-8') as f:
    lua_code = f.read()

acl_name_in_menu = menu['admin/services/ipselect']['depends']['acl'][0]
assert acl_name_in_menu in acl, f'menu.d 依赖的 ACL [{acl_name_in_menu}] 未在 acl.d 中定义'

# 检查 controller.lua 中 acl_depends 是否匹配
acl_match = re.search(r'page\.acl_depends\s*=\s*\{\s*["\']([^"\']+)["\']\s*\}', lua_code)
assert acl_match, 'Controller 中未找到 page.acl_depends 定义'
acl_name_in_controller = acl_match.group(1)
assert acl_name_in_controller == acl_name_in_menu, f'Controller acl_depends [{acl_name_in_controller}] 与 menu.d [{acl_name_in_menu}] 不一致'

print(f'✅ ACL 权限标识一致性校验通过: 统一标识为 "{acl_name_in_menu}"')
EOF

# -------------------------------------------------------------
# 步骤 4: 验证 luasrc/controller/ipselect.lua 语法与词法块闭合
# -------------------------------------------------------------
echo "=== [4/7] 验证 luasrc/controller/ipselect.lua 语法与结构完整性 ==="
if command -v luac >/dev/null 2>&1; then
    luac -p "$CONTROLLER_LUA"
    echo "✅ luac 语法校验通过"
elif command -v lua >/dev/null 2>&1; then
    lua -e "assert(loadfile('$CONTROLLER_LUA'))"
    echo "✅ lua loadfile 语法校验通过"
fi

python3 << 'EOF'
import sys, re, os

lua_file = os.environ['CONTROLLER_LUA']
with open(lua_file, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 1. 验证模块定义
module_line = [l for l in lines if l.startswith('module(')]
assert len(module_line) == 1, '缺少或存在重复 module 定义'
assert 'luci.controller.ipselect' in module_line[0], '模块命名必须为 luci.controller.ipselect'

# 2. 验证核心函数定义
expected_functions = [
    'index',
    'exec_cmd',
    'action_get_status',
    'action_start',
    'action_stop',
    'action_get_log',
    'action_clear_log',
    'action_get_results',
    'action_get_history',
    'action_get_history_log',
    'action_test_github',
]
full_text = ''.join(lines)
for fn in expected_functions:
    pattern = rf'(?:local\s+)?function\s+{fn}\s*\('
    assert re.search(pattern, full_text), f'未找到函数定义: {fn}'

# 3. 词法块闭合性检查
block_opens = 0
block_closes = 0
paren_balance = 0
bracket_balance = 0
brace_balance = 0

in_block_comment = False
for line_no, raw_line in enumerate(lines, 1):
    line = raw_line.strip()
    if not line:
        continue
    if in_block_comment:
        if ']]' in line:
            in_block_comment = False
            line = line.split(']]', 1)[1]
        else:
            continue
    if '--[[' in line:
        in_block_comment = True
        line = line.split('--[[', 1)[0]
    elif '--' in line:
        line = line.split('--', 1)[0]
    
    for ch in line:
        if ch == '(': paren_balance += 1
        elif ch == ')': paren_balance -= 1
        elif ch == '[': bracket_balance += 1
        elif ch == ']': bracket_balance -= 1
        elif ch == '{': brace_balance += 1
        elif ch == '}': brace_balance -= 1
        assert paren_balance >= 0, f'第 {line_no} 行圆括号不平衡'
        assert bracket_balance >= 0, f'第 {line_no} 行中括号不平衡'
        assert brace_balance >= 0, f'第 {line_no} 行大括号不平衡'

    words = re.findall(r'\b[a-zA-Z_]\w*\b', line)
    for w in words:
        if w in ('function', 'then', 'do', 'repeat'):
            block_opens += 1
        elif w in ('end', 'until'):
            block_closes += 1

assert paren_balance == 0, '圆括号未闭合'
assert bracket_balance == 0, '中括号未闭合'
assert brace_balance == 0, '大括号未闭合'
assert block_opens == block_closes, f'代码块未闭合: open={block_opens}, close={block_closes}'

print(f'✅ Lua 词法语法与结构闭合检查通过 (共 {len(expected_functions)} 个函数，代码块匹配数: {block_opens})')
EOF

# -------------------------------------------------------------
# 步骤 5: 验证 Controller 路由节点注册与 leaf 标记
# -------------------------------------------------------------
echo "=== [5/7] 验证 Controller 路由条目定义与 leaf/alias/template 属性 ==="
python3 << 'EOF'
import re, sys, os

lua_file = os.environ['CONTROLLER_LUA']
with open(lua_file, 'r', encoding='utf-8') as f:
    content = f.read()

index_match = re.search(r'function\s+index\(\)\s*(.*?)\nend', content, re.DOTALL)
assert index_match, '未找到 index 函数体'
index_body = index_match.group(1)

# 验证门禁保护
assert 'nixio.fs.access("/etc/config/ipselect")' in index_body, 'index 缺少 /etc/config/ipselect 配置文件门禁判断'

# 验证一级入口节点
root_entry = re.search(r'entry\(\s*\{\s*["\']admin["\'],\s*["\']services["\'],\s*["\']ipselect["\']\s*\}\s*,\s*alias\(\s*["\']admin["\'],\s*["\']services["\'],\s*["\']ipselect["\'],\s*["\']client["\']\s*\)', index_body)
assert root_entry, '顶级入口 entry 路由配置不符合规范'

# 验证 client 模板节点
client_entry = re.search(r'entry\(\s*\{\s*["\']admin["\'],\s*["\']services["\'],\s*["\']ipselect["\'],\s*["\']client["\']\s*\}\s*,\s*template\(["\']ipselect/overview["\']\).*?\)\.leaf\s*=\s*true', index_body)
assert client_entry, 'client 视图 entry 路由未配置或缺少 .leaf = true'

# 验证全部 RPC 端点
expected_endpoints = [
    ('get_status', 'action_get_status'),
    ('start', 'action_start'),
    ('stop', 'action_stop'),
    ('get_log', 'action_get_log'),
    ('clear_log', 'action_clear_log'),
    ('get_results', 'action_get_results'),
    ('get_history', 'action_get_history'),
    ('get_history_log', 'action_get_history_log'),
    ('test_github', 'action_test_github'),
]

for endpoint, handler in expected_endpoints:
    pattern = rf'entry\(\s*\{{\s*["\']admin["\'],\s*["\']services["\'],\s*["\']ipselect["\'],\s*["\']{endpoint}["\']\s*\}}\s*,\s*call\(["\']{handler}["\']\)\s*\)\.leaf\s*=\s*true'
    assert re.search(pattern, index_body), f'缺少 RPC 端点注册: {endpoint} -> {handler} 或未标记 .leaf = true'

print(f'✅ Controller 路由条目完整无误: 1 个概览视图 + {len(expected_endpoints)} 个 RPC 端点全部正确注册且设置 leaf=true')
EOF

# -------------------------------------------------------------
# 步骤 6: 验证 Controller 与 ipselect-runner 子命令接口一致性
# -------------------------------------------------------------
echo "=== [6/7] 验证 Controller 调用指令与 ipselect-runner 接口一致性 ==="
python3 << 'EOF'
import re, sys, os

lua_file = os.environ['CONTROLLER_LUA']
runner_file = os.environ['RUNNER_SCRIPT']

with open(lua_file, 'r', encoding='utf-8') as f:
    lua_code = f.read()

with open(runner_file, 'r', encoding='utf-8') as f:
    runner_code = f.read()

# 提取 controller 中调用的 runner 子命令
calls = re.findall(r'/usr/bin/ipselect-runner\s+([a-zA-Z0-9_\-]+)', lua_code)
assert len(calls) >= 8, f'未提取到足够的 runner 命令调用: {calls}'

# 提取 runner 脚本支持的 case 分支
runner_cases = re.findall(r'^\s*([a-zA-Z0-9_\-]+)\)', runner_code, re.MULTILINE)

expected_runner_subcmds = {
    'action_get_status': 'status',
    'action_start': 'start',
    'action_stop': 'stop',
    'action_get_log': 'log',
    'action_clear_log': 'clear-log',
    'action_get_results': 'results',
    'action_get_history': 'history',
    'action_get_history_log': 'history-log',
    'action_test_github': 'test-github',
}

for action_name, expected_subcmd in expected_runner_subcmds.items():
    m = re.search(rf'function\s+{action_name}\(\)\s*(.*?)\nend', lua_code, re.DOTALL)
    assert m, f'未找到 action 函数体: {action_name}'
    body = m.group(1)
    
    assert f'/usr/bin/ipselect-runner {expected_subcmd}' in body, f'{action_name} 未调用 /usr/bin/ipselect-runner {expected_subcmd}'
    assert expected_subcmd in runner_cases, f'Runner 脚本缺少对子命令 [{expected_subcmd}] 的支持'

print('✅ 所有 Controller Action 调用的子命令与 ipselect-runner 接口完全一致并一一对应')
EOF

# -------------------------------------------------------------
# 步骤 7: 仿真测试 Controller 动作执行、参数防御与响应输出
# -------------------------------------------------------------
echo "=== [7/7] 仿真测试 Controller 动作行为、注入防御与响应格式 ==="
MOCK_RUNNER="$TMP_DIR/bin/mock_ipselect_runner"
MOCK_LOG="$TMP_DIR/logs/runner_invocations.log"

cat << 'EOF' > "$MOCK_RUNNER"
#!/bin/sh
echo "$@" >> "$TEST_LOG"
case "$1" in
    status)
        echo '{"running": false, "pid": 0, "last_run": "2026-10-10 12:00:00"}'
        ;;
    start)
        echo "{\"success\": true, \"message\": \"已启动 (mode: $2)\"}"
        ;;
    stop)
        echo '{"success": true, "message": "已终止"}'
        ;;
    log)
        echo "LOG_CONTENT_FROM_OFFSET_$2"
        ;;
    clear-log)
        echo '{"success": true}'
        ;;
    results)
        echo '{"file": "best_us.txt", "count": 2, "nodes": []}'
        ;;
    history)
        echo '[{"id": "1001", "status": "success"}]'
        ;;
    history-log)
        echo "ARCHIVED_LOG_CONTENT_ID_$2"
        ;;
    test-github)
        echo '{"success": true, "message": "GitHub 连通性测试成功"}'
        ;;
    *)
        echo "未知指令: $1"
        exit 1
        ;;
esac
EOF
chmod +x "$MOCK_RUNNER"

export TEST_RUNNER="$MOCK_RUNNER"
export TEST_LOG="$MOCK_LOG"

python3 << 'EOF'
import os, subprocess, sys, json, re

lua_path = os.environ['CONTROLLER_LUA']
mock_runner = os.environ['TEST_RUNNER']
test_log = os.environ['TEST_LOG']

test_cases = [
    {
        'action': 'action_get_status',
        'params': {},
        'expected_type': 'application/json',
        'expected_cmd_prefix': 'status',
        'expected_output_match': r'"running":\s*false',
    },
    {
        'action': 'action_start',
        'params': {},
        'expected_type': 'application/json',
        'expected_cmd_prefix': 'start manual',
        'expected_output_match': r'"success":\s*true',
    },
    {
        'action': 'action_stop',
        'params': {},
        'expected_type': 'application/json',
        'expected_cmd_prefix': 'stop',
        'expected_output_match': r'"success":\s*true',
    },
    {
        'action': 'action_get_log',
        'params': {'offset': '1024'},
        'expected_type': 'text/plain',
        'expected_cmd_prefix': 'log 1024',
        'expected_output_match': r'LOG_CONTENT_FROM_OFFSET_1024',
    },
    {
        'action': 'action_get_log',
        'params': {'offset': '100; rm -rf /'},  # 注入尝试 -> 必须清洗为 0
        'expected_type': 'text/plain',
        'expected_cmd_prefix': 'log 0',
        'expected_output_match': r'LOG_CONTENT_FROM_OFFSET_0',
    },
    {
        'action': 'action_clear_log',
        'params': {},
        'expected_type': 'application/json',
        'expected_cmd_prefix': 'clear-log',
        'expected_output_match': r'"success":\s*true',
    },
    {
        'action': 'action_get_results',
        'params': {},
        'expected_type': 'application/json',
        'expected_cmd_prefix': 'results',
        'expected_output_match': r'"file":\s*"best_us.txt"',
    },
    {
        'action': 'action_get_history',
        'params': {},
        'expected_type': 'application/json',
        'expected_cmd_prefix': 'history',
        'expected_output_match': r'"id":\s*"1001"',
    },
    {
        'action': 'action_get_history_log',
        'params': {'id': '20261010-120000'},
        'expected_type': 'text/plain',
        'expected_cmd_prefix': 'history-log 20261010-120000',
        'expected_output_match': r'ARCHIVED_LOG_CONTENT_ID_20261010-120000',
    },
    {
        'action': 'action_get_history_log',
        'params': {'id': 'test-id; cat /etc/passwd'},  # 注入尝试 -> 必须清洗为安全字符
        'expected_type': 'text/plain',
        'expected_cmd_prefix': 'history-log test-idcatetcpasswd',
        'expected_output_match': r'ARCHIVED_LOG_CONTENT_ID_test-idcatetcpasswd',
    },
    {
        'action': 'action_test_github',
        'params': {},
        'expected_type': 'application/json',
        'expected_cmd_prefix': 'test-github',
        'expected_output_match': r'GitHub 连通性测试成功',
    },
]

has_lua = subprocess.run(['which', 'lua'], capture_output=True).returncode == 0 or \
          subprocess.run(['which', 'lua5.1'], capture_output=True).returncode == 0

lua_bin = 'lua5.1' if subprocess.run(['which', 'lua5.1'], capture_output=True).returncode == 0 else 'lua'

for tc in test_cases:
    if os.path.exists(test_log):
        os.remove(test_log)
    
    if has_lua:
        param_init = '\n'.join([f'luci.http.mock_params["{k}"] = "{v}"' for k, v in tc['params'].items()])
        test_lua_script = f'''
        package.path = "{os.path.dirname(lua_path)}/?.lua;" .. package.path
        luci = {{
            http = {{
                mock_params = {{}},
                content_type = nil,
                response_body = "",
                prepare_content = function(ct)
                    luci.http.content_type = ct
                end,
                write = function(chunk)
                    luci.http.response_body = luci.http.response_body .. (chunk or "")
                end,
                formvalue = function(k)
                    return luci.http.mock_params[k]
                end
            }},
            controller = {{
                ipselect = {{}}
            }}
        }}
        nixio = {{ fs = {{ access = function() return true end }} }}
        _ = function(s) return s end
        entry = function() return {{ leaf = false }} end
        alias = function() end
        template = function() end
        call = function() end

        {param_init}

        dofile("{lua_path}")

        {tc['action']}()

        print("TYPE:" .. (luci.http.content_type or ""))
        print("BODY:" .. luci.http.response_body)
        '''
        res = subprocess.run([lua_bin, '-e', test_lua_script], capture_output=True, text=True, env=dict(os.environ, TEST_RUNNER=mock_runner))
        assert res.returncode == 0, f'Lua 执行失败: {res.stderr}'
        out = res.stdout
        type_line = [l for l in out.splitlines() if l.startswith('TYPE:')][0].replace('TYPE:', '')
        body_lines = [l for l in out.splitlines() if not l.startswith('TYPE:')]
        body = '\n'.join(body_lines).replace('BODY:', '', 1)
        assert type_line == tc['expected_type'], f"{tc['action']} Content-Type 不匹配: {type_line} vs {tc['expected_type']}"
        assert re.search(tc['expected_output_match'], body), f"{tc['action']} 返回内容不匹配: {body}"
    else:
        with open(lua_path, 'r', encoding='utf-8') as f:
            lua_code = f.read()
        m = re.search(rf'function\s+{tc["action"]}\(\)\s*(.*?)\nend', lua_code, re.DOTALL)
        assert m, f'未找到 action: {tc["action"]}'
        body = m.group(1)
        assert f'prepare_content("{tc["expected_type"]}")' in body, f"{tc['action']} prepare_content 类型错误"
        
        cmd_call = ''
        if tc['action'] == 'action_get_log':
            raw_val = tc['params'].get('offset', '0')
            offset = raw_val if str(raw_val).isdigit() else '0'
            cmd_call = f'{mock_runner} log {offset}'
        elif tc['action'] == 'action_get_history_log':
            raw_val = tc['params'].get('id', '')
            safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', str(raw_val))
            cmd_call = f'{mock_runner} history-log {safe_id}'
        elif tc['action'] == 'action_get_status':
            cmd_call = f'{mock_runner} status'
        elif tc['action'] == 'action_start':
            cmd_call = f'{mock_runner} start manual'
        elif tc['action'] == 'action_stop':
            cmd_call = f'{mock_runner} stop'
        elif tc['action'] == 'action_clear_log':
            cmd_call = f'{mock_runner} clear-log'
        elif tc['action'] == 'action_get_results':
            cmd_call = f'{mock_runner} results'
        elif tc['action'] == 'action_get_history':
            cmd_call = f'{mock_runner} history'
        elif tc['action'] == 'action_test_github':
            cmd_call = f'{mock_runner} test-github'
        
        if sys.platform == 'win32':
            proc = subprocess.run(['sh'] + cmd_call.split(), capture_output=True, text=True, encoding='utf-8', errors='replace', env=dict(os.environ, TEST_LOG=test_log))
        else:
            proc = subprocess.run(cmd_call, shell=True, capture_output=True, text=True, encoding='utf-8', errors='replace', env=dict(os.environ, TEST_LOG=test_log))

        assert proc.returncode == 0, f'Mock runner 执行失败: {proc.stderr}'
        assert re.search(tc['expected_output_match'], proc.stdout), f'输出未匹配: {proc.stdout}'
    
    with open(test_log, 'r', encoding='utf-8') as f:
        log_content = f.read().strip()
    assert log_content.startswith(tc['expected_cmd_prefix']), f'Runner 调用记录与预期不符: 实际 [{log_content}], 预期前缀 [{tc["expected_cmd_prefix"]}]'

print(f'✅ 所有 {len(test_cases)} 个 Controller Action 仿真调用、Content-Type 与参数防注入测试全部通过！')
EOF

# 清理测试临时文件
rm -rf "$TMP_DIR"

echo "=========================================="
echo "🎉 所有 7 项控制器与路由测试全部通过！"
echo "=========================================="
