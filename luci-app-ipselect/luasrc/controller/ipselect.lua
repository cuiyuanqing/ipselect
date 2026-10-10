module("luci.controller.ipselect", package.seeall)

function index()
	if not nixio.fs.access("/etc/config/ipselect") then
		return
	end

	-- 在现代 LuCI 环境中，如果已通过 menu.d 注册了 JS View，则避免覆盖顶级入口，仅作旧版 LuCI 回退
	if not nixio.fs.access("/usr/share/luci/menu.d/luci-app-ipselect.json") then
		local page = entry({"admin", "services", "ipselect"}, alias("admin", "services", "ipselect", "client"), _("节点优选"), 60)
		page.dependent = true
		page.acl_depends = { "luci-app-ipselect" }

		entry({"admin", "services", "ipselect", "client"}, template("ipselect/overview"), _("概览"), 10).leaf = true
	end
	entry({"admin", "services", "ipselect", "get_status"}, call("action_get_status")).leaf = true
	entry({"admin", "services", "ipselect", "start"}, call("action_start")).leaf = true
	entry({"admin", "services", "ipselect", "stop"}, call("action_stop")).leaf = true
	entry({"admin", "services", "ipselect", "get_log"}, call("action_get_log")).leaf = true
	entry({"admin", "services", "ipselect", "clear_log"}, call("action_clear_log")).leaf = true
	entry({"admin", "services", "ipselect", "get_results"}, call("action_get_results")).leaf = true
	entry({"admin", "services", "ipselect", "get_history"}, call("action_get_history")).leaf = true
	entry({"admin", "services", "ipselect", "get_history_log"}, call("action_get_history_log")).leaf = true
	entry({"admin", "services", "ipselect", "test_github"}, call("action_test_github")).leaf = true
end

local function exec_cmd(cmd)
	local runner = os.getenv("TEST_RUNNER")
	if runner then
		cmd = cmd:gsub("^/usr/bin/ipselect%-runner", runner)
	end
	local pp = io.popen(cmd)
	if not pp then return "" end
	local data = pp:read("*all")
	pp:close()
	return data or ""
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
	if not tostring(offset):match("^%d+$") then
		offset = "0"
	end
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
	id = tostring(id):gsub("[^%w%-_]", "")
	luci.http.prepare_content("text/plain")
	luci.http.write(exec_cmd("/usr/bin/ipselect-runner history-log " .. id))
end

function action_test_github()
	luci.http.prepare_content("application/json")
	luci.http.write(exec_cmd("/usr/bin/ipselect-runner test-github"))
end
