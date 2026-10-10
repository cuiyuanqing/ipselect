/**
 * tests/test_view_syntax.js
 * 
 * 校验 luci-app-ipselect 前端 JavaScript 视图 (overview.js) 的：
 * 1. JavaScript 语法合规性 (node -c 校验)
 * 2. 模块依赖与规范结构完整性 (LuCI require 指令, view.extend, load/render 导出)
 * 3. 8 大 LuCI Controller RPC 后端接口契约绑定
 * 4. 4 大选项卡 (控制台日志、优选结果、历史档案、UCI 基础设置) 结构与组件
 * 5. UCI 表单 11 个核心字段与 /etc/init.d/ipselect restart 联动
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const assert = require('assert');

console.log('====================================================');
console.log('开始执行 luci-app-ipselect 前端视图 (overview.js) 校验');
console.log('====================================================');

const viewFilePath = path.resolve(__dirname, '../htdocs/luci-static/resources/view/ipselect/overview.js');

// -------------------------------------------------------------
// 检查项 1: 文件存在性与 node -c 语法合规性
// -------------------------------------------------------------
console.log('\n[1/5] 验证文件存在性与 JavaScript 语法合规性...');
assert(fs.existsSync(viewFilePath), `未找到视图文件: ${viewFilePath}`);

try {
	execFileSync(process.execPath, ['-c', viewFilePath], { stdio: 'pipe' });
	console.log('  ✅ node -c 语法静态分析通过 (Exit Code 0，无语法错误)');
} catch (err) {
	console.error('  ❌ node -c 语法校验失败:', err.stderr ? err.stderr.toString() : err.message);
	process.exit(1);
}

// -------------------------------------------------------------
// 检查项 2: LuCI 规范指令与依赖声明分析
// -------------------------------------------------------------
console.log('\n[2/5] 验证 LuCI 标准依赖声明指令 (use strict & require directives)...');
const fileContent = fs.readFileSync(viewFilePath, 'utf8');

const requiredDirectives = [
	'use strict',
	'require view',
	'require dom',
	'require fs',
	'require ui',
	'require uci',
	'require form',
	'require request'
];

requiredDirectives.forEach(dir => {
	const regex = new RegExp(`['"]${dir}['"]`);
	assert(regex.test(fileContent), `缺少必要的 LuCI 标准声明: '${dir}'`);
	console.log(`  ✅ 依赖声明通过: '${dir}'`);
});

// -------------------------------------------------------------
// 检查项 3: 8 大 LuCI Controller RPC 后端端点绑定验证
// -------------------------------------------------------------
console.log('\n[3/5] 验证 9 个 LuCI Controller RPC 端点路由与调用绑定...');
const expectedEndpoints = [
	'get_status',
	'start',
	'stop',
	'get_log',
	'clear_log',
	'get_results',
	'get_history',
	'get_history_log',
	'test_github'
];

expectedEndpoints.forEach(ep => {
	const regex = new RegExp(`['"]admin/services/ipselect/${ep}['"]|API\\.${ep}\\b|getApiUrl\\(['"]${ep}['"]\\)`);
	assert(regex.test(fileContent), `overview.js 中未找到端点绑定或调用: ${ep}`);
	console.log(`  ✅ RPC 端点匹配: admin/services/ipselect/${ep}`);
});

// -------------------------------------------------------------
// 检查项 4: 仿真 LuCI 运行环境并评估视图模块构造与导出
// -------------------------------------------------------------
console.log('\n[4/5] 仿真 LuCI 执行沙箱，加载并实例化视图组件...');

// 创建 DOM 与 LuCI 模拟桩
function MockElement(tagName, attrs, children) {
	this.tagName = String(tagName).toLowerCase();
	this.attrs = attrs || {};
	this.children = [];
	this.childNodes = this.children;
	this.classList = {
		classes: new Set((this.attrs.class || '').split(/\s+/).filter(Boolean)),
		add: (cls) => this.classList.classes.add(cls),
		remove: (cls) => this.classList.classes.delete(cls),
		contains: (cls) => this.classList.classes.has(cls)
	};
	this.style = {};
	this.textContent = '';
	this.scrollTop = 0;
	this.scrollHeight = 100;
	this.disabled = false;

	if (children) {
		if (Array.isArray(children)) {
			children.forEach(c => this.appendChild(c));
		} else {
			this.appendChild(children);
		}
	}
}

MockElement.prototype.setAttribute = function(k, v) { this.attrs[k] = String(v); };
MockElement.prototype.getAttribute = function(k) { return this.attrs[k]; };
MockElement.prototype.appendChild = function(child) {
	if (child === null || child === undefined) return;
	if (typeof child === 'string' || typeof child === 'number') {
		const textNode = new MockElement('#text');
		textNode.textContent = String(child);
		textNode.parentNode = this;
		this.children.push(textNode);
		this.textContent += textNode.textContent;
	} else if (child instanceof MockElement) {
		child.parentNode = this;
		this.children.push(child);
		this.textContent += child.textContent;
	}
	return child;
};
MockElement.prototype.insertBefore = function(newNode, refNode) {
	if (!newNode) return;
	newNode.parentNode = this;
	const idx = this.children.indexOf(refNode);
	if (idx >= 0) {
		this.children.splice(idx, 0, newNode);
	} else {
		this.children.push(newNode);
	}
	return newNode;
};

MockElement.prototype.removeChild = function(child) {
	const idx = this.children.indexOf(child);
	if (idx >= 0) this.children.splice(idx, 1);
	return child;
};
MockElement.prototype.querySelector = function(selector) {
	if (selector === 'tbody') {
		for (let c of this.children) {
			if (c.tagName === 'tbody') return c;
			const found = c.querySelector('tbody');
			if (found) return found;
		}
	}
	return null;
};
MockElement.prototype.querySelectorAll = function(selector) {
	const res = [];
	const findIn = (node) => {
		for (let c of node.children) {
			if (selector.startsWith('.') && c.classList.contains(selector.slice(1))) {
				res.push(c);
			}
			findIn(c);
		}
	};
	findIn(this);
	return res;
};

const mockDOM = {
	content: function(el, content) {
		if (!el) return;
		el.children = [];
		el.textContent = '';
		if (content) el.appendChild(content);
	}
};

const mockE = function(tagName, attrs, children) {
	return new MockElement(tagName, attrs, children);
};

const mockUi = {
	addNotification: function(title, node, type) {
		mockUi.lastNotification = { title, node, type };
	},
	showModal: function(title, nodes, className) {
		mockUi.lastModal = { title, nodes, className };
	},
	hideModal: function() {
		mockUi.lastModal = null;
	},
	createHandlerFn: function(ctx, fnName, ...args) {
		return function(ev) {
			if (typeof fnName === 'function') return fnName.apply(ctx, [ev, ...args]);
			if (typeof ctx[fnName] === 'function') return ctx[fnName].apply(ctx, [ev, ...args]);
		};
	},
	tabs: {
		initTabGroup: function(childNodes) {
			mockUi.tabsInitialized = true;
			mockUi.registeredTabNodes = childNodes;
			if (childNodes && childNodes.length > 0) {
				const group = childNodes[0].parentNode;
				if (!group) {
					throw new TypeError("Cannot read properties of null (evaluating 'group.parentNode')");
				}
				if (!group.parentNode) {
					throw new TypeError("null is not an object (evaluating 'group.parentNode.insertBefore')");
				}
				const menu = new MockElement('ul', { 'class': 'cbi-tabmenu' });
				group.parentNode.insertBefore(menu, group);
				group.setAttribute('data-initialized', 'true');
			}
		}
	}

};

const mockUci = {
	load: function(pkg) {
		mockUci.loadedPackage = pkg;
		return Promise.resolve();
	},
	get: function(pkg, section, opt) {
		if (pkg === 'ipselect' && section === 'config') {
			if (opt === 'env_tag') return '公司';
			if (opt === 'output_file') return 'best_us.txt';
			if (opt === 'enabled') return '1';
		}
		return null;
	}
};

const rpcCallsRecorded = [];
const mockRequest = {
	get: function(url, options) {
		rpcCallsRecorded.push({ method: 'GET', url, options });
		return Promise.resolve({
			json: function() {
				if (url.includes('get_status')) return { running: false, pid: 0, last_run: '2026-10-10 08:00:00' };
				if (url.includes('get_results')) return { file: 'best_us.txt', update_time: '2026-10-10 08:00:00', count: 2, nodes: [
					{ index: 1, ip: '104.16.89.201', port: '443', remark: '公司-SJC' },
					{ index: 2, ip: '162.158.21.45', port: '443', remark: '公司-LAX' }
				] };
				if (url.includes('get_history')) return [
					{ id: '20261010-080000', timestamp: '2026-10-10 08:00:00', trigger: 'cron', duration: 125, nodeCount: 15, gitStatus: 'pushed', openclashStatus: 'success', status: 'success' }
				];
				return {};
			},
			text: function() {
				if (url.includes('get_log')) return 'MOCK_LOG_STREAM_OUTPUT\n';
				if (url.includes('get_history_log')) return 'MOCK_HISTORY_LOG_SNAPSHOT\n';
				return '';
			}
		});
	},
	post: function(url, data, options) {
		rpcCallsRecorded.push({ method: 'POST', url, data, options });
		return Promise.resolve({
			json: function() {
				if (url.includes('start')) return { success: true, message: '任务已启动' };
				if (url.includes('stop')) return { success: true, message: '任务已终止' };
				if (url.includes('clear_log')) return { success: true };
				return { success: true };
			}
		});
	}
};

const fsCallsRecorded = [];
const mockFs = {
	exec: function(cmd, args) {
		fsCallsRecorded.push({ cmd, args });
		return Promise.resolve({ code: 0, stdout: '', stderr: '' });
	}
};

const formOptionsRecorded = {};
class MockCBISection {
	constructor(map, sectionId, type) {
		this.map = map;
		this.sectionId = sectionId;
		this.type = type;
		this.options = [];
	}
	option(cls, optName, title, desc) {
		const optObj = { cls, optName, title, desc, choices: [], dependsList: [], defaultVal: null };
		optObj.value = function(k, v) { optObj.choices.push({ k, v }); };
		optObj.depends = function(depOpt, depVal) { optObj.dependsList.push({ depOpt, depVal }); };
		formOptionsRecorded[optName] = optObj;
		this.options.push(optObj);
		return optObj;
	}
}

class MockCBIMap {
	constructor(config, title, desc) {
		this.config = config;
		this.title = title;
		this.desc = desc;
		this.sections = [];
	}
	section(cls, sectionId, type) {
		const s = new MockCBISection(this, sectionId, type);
		this.sections.push(s);
		return s;
	}
	render() {
		const el = new MockElement('div', { class: 'cbi-map' });
		return Promise.resolve(el);
	}
	save() {
		return Promise.resolve();
	}
}

const mockForm = {
	Map: MockCBIMap,
	NamedSection: MockCBISection,
	Flag: function() {},
	Value: function() {},
	ListValue: function() {}
};

const mockView = {
	extend: function(def) {
		function ViewConstructor() {
			Object.assign(this, def);
		}
		ViewConstructor.prototype = def;
		return ViewConstructor;
	}
};

const mockL = {
	url: function(...parts) {
		return '/cgi-bin/luci/' + parts.join('/');
	},
	bind: function(fn, ctx, ...args) {
		return fn.bind(ctx, ...args);
	}
};

let intervalCallback = null;
const mockWindow = {
	setInterval: function(fn, ms) {
		intervalCallback = fn;
		return 999;
	},
	clearInterval: function(id) {
		intervalCallback = null;
	},
	setTimeout: function(fn, ms) {
		return setTimeout(fn, 1);
	}
};

const mockDocument = {
	body: new MockElement('body')
};

// 执行包装后的工厂代码
const factoryFn = new Function(
	'window', 'document', 'L', 'view', 'fs', 'ui', 'uci', 'form', 'request', 'E', '_', 'dom',
	fileContent
);

const ViewClass = factoryFn(
	mockWindow,
	mockDocument,
	mockL,
	mockView,
	mockFs,
	mockUi,
	mockUci,
	mockForm,
	mockRequest,
	mockE,
	(s) => s,
	mockDOM
);

assert(typeof ViewClass === 'function', '视图模块未导出由 view.extend 生成的构造函数');
const viewInstance = new ViewClass();
console.log('  ✅ 成功实例化 View 类组件');

// 校验核心生命周期与功能方法定义
const requiredMethods = [
	'load',
	'render',
	'handleStart',
	'handleStop',
	'handleClearLog',
	'refreshResults',
	'refreshHistory',
	'showHistoryLogModal',
	'handleSaveApply'
];

requiredMethods.forEach(method => {
	assert(typeof viewInstance[method] === 'function', `View 原型上缺少方法: ${method}`);
	console.log(`  ✅ 核心方法存在: View.${method}()`);
});

// -------------------------------------------------------------
// 检查项 5: 运行 load() 与 render() 流程，校验数据流与选项卡结构
// -------------------------------------------------------------
console.log('\n[5/5] 执行 load() 与 render() 数据流，验证 4 大选项卡与 UCI 控件...');

(async () => {
	// 测试 load()
	rpcCallsRecorded.length = 0;
	const loadResult = await viewInstance.load();
	assert(Array.isArray(loadResult) && loadResult.length === 4, 'load() 必须并行返回 4 个数据元素 [uci, status, results, history]');
	assert.strictEqual(mockUci.loadedPackage, 'ipselect', 'load() 必须调用 uci.load("ipselect")');
	
	const loadedUrls = rpcCallsRecorded.map(r => r.url);
	assert(loadedUrls.some(u => u.includes('get_status')), 'load() 必须并发请求 get_status');
	assert(loadedUrls.some(u => u.includes('get_results')), 'load() 必须并发请求 get_results');
	assert(loadedUrls.some(u => u.includes('get_history')), 'load() 必须并发请求 get_history');
	console.log('  ✅ load() 并发读取 UCI、状态、结果与历史成功');

	// 测试 render()
	const renderedDom = await viewInstance.render(loadResult);
	assert(renderedDom instanceof MockElement, 'render() 返回必须解析为 MockElement DOM 根节点');

	// 验证选项卡初始化
	assert(mockUi.tabsInitialized === true, 'render() 必须调用 ui.tabs.initTabGroup 初始化选项卡组');
	const tabNodes = mockUi.registeredTabNodes || [];
	assert(tabNodes.length >= 4, `选项卡数量应至少为 4，实际为: ${tabNodes.length}`);

	const tabKeys = tabNodes.map(t => t.getAttribute('data-tab'));
	assert(tabKeys.includes('console'), '必须包含控制台选项卡 data-tab="console"');
	assert(tabKeys.includes('results'), '必须包含优选结果选项卡 data-tab="results"');
	assert(tabKeys.includes('history'), '必须包含历史档案选项卡 data-tab="history"');
	assert(tabKeys.includes('settings'), '必须包含基础设置选项卡 data-tab="settings"');
	console.log('  ✅ 4 大核心选项卡 (console, results, history, settings) 结构完整初始化');

	// 验证 UCI 核心配置项 (包含 GitHub Token 与 可视化 Cron 新增项)
	const expectedUciOptions = [
		'enabled',
		'workdir',
		'env_tag',
		'output_file',
		'interface',
		'auto_push',
		'github_repo',
		'github_token',
		'github_branch',
		'github_user',
		'github_email',
		'auto_clone',
		'_test_github',
		'openclash_sync',
		'openclash_provider',
		'cron_enabled',
		'_cron_generator',
		'cron_expression'
	];

	expectedUciOptions.forEach(optName => {
		assert(formOptionsRecorded[optName] != null, `UCI 设置表单缺少配置项: ${optName}`);
		console.log(`  ✅ UCI 表单配置项就绪: ${optName}`);
	});

	// 验证 auto_push, openclash, cron 联动依赖
	assert(formOptionsRecorded['github_token'].dependsList.some(d => d.depOpt === 'auto_push' && d.depVal === '1'), 'github_token 必须 depends 于 auto_push=1');
	assert(formOptionsRecorded['openclash_provider'].dependsList.some(d => d.depOpt === 'openclash_sync' && d.depVal === '1'), 'openclash_provider 必须 depends 于 openclash_sync=1');
	assert(formOptionsRecorded['cron_expression'].dependsList.some(d => d.depOpt === 'cron_enabled' && d.depVal === '1'), 'cron_expression 必须 depends 于 cron_enabled=1');
	console.log('  ✅ 表单动态联动依赖条件 (auto_push, openclash_sync, cron_enabled) 配置完备');

	// 测试 handleSaveApply 调用与服务重载联动
	fsCallsRecorded.length = 0;
	viewInstance.handleSave = function() { return Promise.resolve(); };
	mockUi.changes = { apply: function() { return Promise.resolve(); } };

	await viewInstance.handleSaveApply({}, '0');
	const restarted = fsCallsRecorded.some(c => c.cmd === '/etc/init.d/ipselect' && c.args && c.args[0] === 'restart');
	assert(restarted, 'handleSaveApply 必须触发 fs.exec("/etc/init.d/ipselect", ["restart"]) 重载系统服务与 Crontab');
	console.log('  ✅ handleSaveApply 成功触发 /etc/init.d/ipselect restart 联动');

	// 测试 start / stop / clearLog 操作触发
	rpcCallsRecorded.length = 0;
	viewInstance.handleStart();
	assert(rpcCallsRecorded.some(r => r.method === 'POST' && r.url.includes('start')), 'handleStart 必须触发 POST start');
	console.log('  ✅ handleStart 触发 POST start 验证通过');

	rpcCallsRecorded.length = 0;
	viewInstance.handleClearLog();
	assert(rpcCallsRecorded.some(r => r.method === 'POST' && r.url.includes('clear_log')), 'handleClearLog 必须触发 POST clear_log');
	console.log('  ✅ handleClearLog 触发 POST clear_log 验证通过');

	// 验证 1.5s 轮询中 updateDashboardStatus 实时联动与 URL.revokeObjectURL 异步释放
	let statusUpdatedInPolling = false;
	const originalUpdateDashboardStatus = viewInstance.updateDashboardStatus;
	viewInstance.updateDashboardStatus = function(st, hist) {
		statusUpdatedInPolling = true;
		if (originalUpdateDashboardStatus) originalUpdateDashboardStatus.apply(this, arguments);
	};

	viewInstance.startLogPolling();
	assert(typeof intervalCallback === 'function', 'startLogPolling 必须注册 window.setInterval 定时回调');
	intervalCallback();
	await new Promise(r => setTimeout(r, 20));
	assert(statusUpdatedInPolling, 'startLogPolling 轮询中必须实时调用 updateDashboardStatus(st) 联动仪表盘状态指示灯');
	console.log('  ✅ 1.5s 轮询中 updateDashboardStatus(st) 状态指示灯实时联动验证通过');
	viewInstance.stopLogPolling();

	// 验证 downloadTextFile 采用 setTimeout 延迟注销 Object URL
	assert(fileContent.includes('window.setTimeout(function() {') && fileContent.includes('URL.revokeObjectURL(url);'), 'downloadTextFile 必须使用 setTimeout 延迟释放 Object URL');
	console.log('  ✅ downloadTextFile 异步延迟释放 Object URL 规避竞态风险验证通过');

	// 测试 查看历史日志弹窗 showHistoryLogModal
	rpcCallsRecorded.length = 0;
	mockUi.lastModal = null;
	viewInstance.showHistoryLogModal({ id: '20261010-080000', timestamp: '2026-10-10 08:00:00' });
	await new Promise(r => setTimeout(r, 10));
	assert(rpcCallsRecorded.some(r => r.method === 'GET' && r.url.includes('get_history_log')), 'showHistoryLogModal 必须触发 GET get_history_log');
	assert(mockUi.lastModal != null, 'showHistoryLogModal 必须调用 ui.showModal 弹窗展示日志');
	console.log('  ✅ showHistoryLogModal 弹窗与历史日志查询验证通过');

	console.log('\n====================================================');
	console.log('🎉 所有 5 大类测试全部通过！JavaScript 视图逻辑校验成功！');
	console.log('====================================================');
})().catch(err => {
	console.error('❌ 测试运行失败:', err);
	process.exit(1);
});
