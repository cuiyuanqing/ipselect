'use strict';
'require view';
'require dom';
'require fs';
'require ui';
'require uci';
'require form';
'require request';

/* API 接口路由常量表 (对应 luasrc/controller/ipselect.lua RPC 端点) */
var API = {
	get_status: 'admin/services/ipselect/get_status',
	start: 'admin/services/ipselect/start',
	stop: 'admin/services/ipselect/stop',
	get_log: 'admin/services/ipselect/get_log',
	clear_log: 'admin/services/ipselect/clear_log',
	get_results: 'admin/services/ipselect/get_results',
	get_history: 'admin/services/ipselect/get_history',
	get_history_log: 'admin/services/ipselect/get_history_log',
	test_github: 'admin/services/ipselect/test_github'
};

function getApiUrl(endpoint) {
	return L.url(API[endpoint] || ('admin/services/ipselect/' + endpoint));
}

function describeCron(cronStr) {
	if (!cronStr || typeof cronStr !== 'string') return _('未设置调度计划');
	var parts = cronStr.trim().split(/\s+/);
	if (parts.length < 5) return _('Cron 表达式格式不完整 (需 5 段: 分 时 日 月 周)');
	var m = parts[0], h = parts[1], dom = parts[2], mon = parts[3], dow = parts[4];

	if (dom === '*' && mon === '*' && dow === '*') {
		if (m.indexOf('*/') === 0 && h === '*') {
			return _('每隔 ') + m.substring(2) + _(' 分钟执行一次优选');
		}
		if (m === '0' && h.indexOf('*/') === 0) {
			return _('每隔 ') + h.substring(3) + _(' 小时整点执行一次优选');
		}
		if (h.indexOf('*/') === 0) {
			return _('每隔 ') + h.substring(3) + _(' 小时的第 ') + m + _(' 分执行');
		}
		if (!h.includes('/') && !h.includes('*')) {
			var hours = h.split(',').sort(function(a, b) { return parseInt(a, 10) - parseInt(b, 10); });
			var times = hours.map(function(hh) {
				var hhNum = parseInt(hh, 10);
				var mNum = parseInt(m, 10) || 0;
				return (hhNum < 10 ? '0' + hhNum : hhNum) + ':' + (mNum < 10 ? '0' + mNum : mNum);
			});
			return _('每天在 ') + times.join(', ') + _(' 准时执行优选');
		}
	}
	return _('自定义表达式: ') + cronStr;
}

function formatDuration(sec) {
	var s = parseInt(sec, 10) || 0;
	if (s < 60)
		return s + ' ' + _('秒');
	var m = Math.floor(s / 60);
	var rem = s % 60;
	return m + ' ' + _('分') + (rem > 0 ? (' ' + rem + ' ' + _('秒')) : '');
}

function getStatusBadge(status) {
	switch (status) {
	case 'success':
		return E('span', { 'class': 'badge', 'style': 'background:#28a745;color:#fff;padding:2px 8px;border-radius:3px;font-size:12px;' }, [_('成功')]);
	case 'failed':
		return E('span', { 'class': 'badge', 'style': 'background:#dc3545;color:#fff;padding:2px 8px;border-radius:3px;font-size:12px;' }, [_('失败')]);
	case 'terminated':
		return E('span', { 'class': 'badge', 'style': 'background:#ffc107;color:#212529;padding:2px 8px;border-radius:3px;font-size:12px;' }, [_('手动中止')]);
	default:
		return E('span', { 'class': 'badge', 'style': 'background:#6c757d;color:#fff;padding:2px 8px;border-radius:3px;font-size:12px;' }, [status || _('未知')]);
	}
}

function getGitBadge(gitStatus) {
	switch (gitStatus) {
	case 'pushed':
		return E('span', { 'class': 'badge', 'style': 'background:#28a745;color:#fff;padding:2px 6px;border-radius:3px;font-size:12px;' }, ['🟢 ' + _('成功推仓')]);
	case 'unchanged':
		return E('span', { 'class': 'badge', 'style': 'background:#17a2b8;color:#fff;padding:2px 6px;border-radius:3px;font-size:12px;' }, ['🟡 ' + _('无变动免推')]);
	case 'error':
		return E('span', { 'class': 'badge', 'style': 'background:#dc3545;color:#fff;padding:2px 6px;border-radius:3px;font-size:12px;' }, ['🔴 ' + _('推送失败')]);
	case 'skipped':
	default:
		return E('span', { 'class': 'badge', 'style': 'background:#6c757d;color:#fff;padding:2px 6px;border-radius:3px;font-size:12px;' }, ['⚪ ' + _('跳过')]);
	}
}

function getOcBadge(ocStatus) {
	switch (ocStatus) {
	case 'success':
		return E('span', { 'class': 'badge', 'style': 'background:#28a745;color:#fff;padding:2px 6px;border-radius:3px;font-size:12px;' }, ['🟢 ' + _('成功刷新')]);
	case 'failed':
		return E('span', { 'class': 'badge', 'style': 'background:#dc3545;color:#fff;padding:2px 6px;border-radius:3px;font-size:12px;' }, ['🔴 ' + _('刷新失败')]);
	case 'skipped':
	default:
		return E('span', { 'class': 'badge', 'style': 'background:#6c757d;color:#fff;padding:2px 6px;border-radius:3px;font-size:12px;' }, ['⚪ ' + _('跳过/未开启')]);
	}
}

function copyToClipboard(text, successMsg) {
	if (navigator.clipboard && navigator.clipboard.writeText) {
		navigator.clipboard.writeText(text).then(function() {
			ui.addNotification(null, E('p', successMsg || _('已成功复制到剪贴板！')), 'info');
		}).catch(function() {
			fallbackCopy(text, successMsg);
		});
	} else {
		fallbackCopy(text, successMsg);
	}
}

function fallbackCopy(text, successMsg) {
	var ta = E('textarea', { 'style': 'position:fixed;top:-1000px;left:-1000px;' }, [text]);
	document.body.appendChild(ta);
	ta.focus();
	ta.select();
	try {
		document.execCommand('copy');
		ui.addNotification(null, E('p', successMsg || _('已成功复制到剪贴板！')), 'info');
	} catch (e) {
		ui.addNotification(null, E('p', _('复制失败，请手动选取复制')), 'warning');
	}
	document.body.removeChild(ta);
}

function downloadTextFile(filename, content) {
	var blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
	var url = URL.createObjectURL(blob);
	var a = E('a', { 'href': url, 'download': filename, 'style': 'display:none;' });
	document.body.appendChild(a);
	a.click();
	document.body.removeChild(a);
	window.setTimeout(function() {
		URL.revokeObjectURL(url);
	}, 1000);
}

return view.extend({
	pollTimer: null,
	logOffset: 0,
	isTaskRunning: false,

	load: function() {
		return Promise.all([
			uci.load('ipselect'),
			request.get(getApiUrl('get_status')).then(function(res) {
				return res.json();
			}).catch(function() {
				return { running: false, pid: 0, last_run: _('从未运行') };
			}),
			request.get(getApiUrl('get_results')).then(function(res) {
				return res.json();
			}).catch(function() {
				return { file: 'best_us.txt', update_time: _('从未生成'), count: 0, nodes: [] };
			}),
			request.get(getApiUrl('get_history')).then(function(res) {
				return res.json();
			}).catch(function() {
				return [];
			})
		]);
	},

	render: function(data) {
		var self = this;
		var uciConfig = data[0];
		var statusData = data[1] || { running: false, pid: 0, last_run: _('从未运行') };
		var resultsData = data[2] || { file: 'best_us.txt', update_time: _('从未生成'), count: 0, nodes: [] };
		var historyData = Array.isArray(data[3]) ? data[3] : [];

		self.isTaskRunning = !!statusData.running;
		self.logOffset = 0;

		/* ==================== 顶部 Header 与 仪表盘 ==================== */
		var headerTitle = E('h2', { 'style': 'margin-bottom:4px;' }, [_('节点优选控制台 (luci-app-ipselect)')]);
		var headerDesc = E('p', { 'class': 'cbi-map-descr', 'style': 'margin-bottom:16px;' }, [
			_('基于 Cloudflare Anycast 网络的测速与节点优选调度中心，支持终端日志实时滚屏、结果可视化、历史审计及 OpenClash 订阅自动联动。')
		]);

		/* 仪表盘关键指标指示器 */
		var dashStatusBadge = E('span', {}, []);
		var dashEnvTag = E('span', { 'style': 'font-weight:600;' }, [
			(uci.get('ipselect', 'config', 'env_tag') || '公司') + ' (' + (uci.get('ipselect', 'config', 'output_file') || 'best_us.txt') + ')'
		]);
		var dashLastRun = E('span', { 'style': 'font-weight:600;' }, [statusData.last_run || _('从未运行')]);
		var dashGitStatus = E('span', {}, []);
		var dashOcStatus = E('span', {}, []);

		function updateDashboardStatus(st, hist) {
			/* 更新状态指示灯 */
			if (st && st.running) {
				dashStatusBadge.className = 'badge';
				dashStatusBadge.setAttribute('style', 'background:#0072c6;color:#fff;padding:4px 10px;border-radius:4px;font-size:13px;');
				dashStatusBadge.textContent = '🔄 ' + _('正在优选测速中 (PID: ') + (st.pid != null ? st.pid : '-') + ')';
			} else {
				dashStatusBadge.className = 'badge';
				dashStatusBadge.setAttribute('style', 'background:#28a745;color:#fff;padding:4px 10px;border-radius:4px;font-size:13px;');
				dashStatusBadge.textContent = '🟢 ' + _('空闲就绪 (Idle)');
			}

			if (st && st.last_run)
				dashLastRun.textContent = st.last_run;

			if (hist !== undefined) {
				var latest = (Array.isArray(hist) && hist.length > 0) ? hist[0] : null;
				if (latest) {
					dom.content(dashGitStatus, getGitBadge(latest.gitStatus));
					dom.content(dashOcStatus, getOcBadge(latest.openclashStatus));
				} else {
					dom.content(dashGitStatus, E('span', { 'style': 'color:#888;' }, [_('暂无记录')]));
					dom.content(dashOcStatus, E('span', { 'style': 'color:#888;' }, [_('暂无记录')]));
				}
			}
		}

		updateDashboardStatus(statusData, historyData);

		var dashboardCard = E('div', {
			'class': 'cbi-section',
			'style': 'background:#f8f9fa; border:1px solid #e9ecef; border-radius:6px; padding:16px 20px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,0.05);'
		}, [
			E('div', { 'style': 'display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:16px;' }, [
				E('div', { 'style': 'display:flex; align-items:center; gap:10px;' }, [
					E('strong', { 'style': 'font-size:14px;' }, [_('当前系统状态：')]),
					dashStatusBadge
				]),
				E('div', { 'style': 'display:flex; flex-wrap:wrap; gap:24px; font-size:13px;' }, [
					E('div', {}, [
						E('span', { 'style': 'color:#666;' }, [_('测速环境：')]),
						dashEnvTag
					]),
					E('div', {}, [
						E('span', { 'style': 'color:#666;' }, [_('最近执行时间：')]),
						dashLastRun
					]),
					E('div', {}, [
						E('span', { 'style': 'color:#666;' }, [_('GitHub 推送：')]),
						dashGitStatus
					]),
					E('div', {}, [
						E('span', { 'style': 'color:#666;' }, [_('OpenClash 联动：')]),
						dashOcStatus
					])
				])
			])
		]);

		/* ==================== Tab 1: 控制台与实时日志 ==================== */
		var btnStart = E('button', {
			'class': 'btn cbi-button-action important',
			'style': 'margin-right:8px;',
			'click': ui.createHandlerFn(self, 'handleStart')
		}, ['▶ ' + _('立即开始优选')]);

		var btnStop = E('button', {
			'class': 'btn cbi-button-negative',
			'style': 'margin-right:8px;',
			'click': ui.createHandlerFn(self, 'handleStop')
		}, ['⏹ ' + _('终止当前任务')]);

		var btnClear = E('button', {
			'class': 'btn cbi-button-neutral',
			'style': 'margin-right:12px;',
			'click': ui.createHandlerFn(self, 'handleClearLog')
		}, ['🗑 ' + _('清空日志')]);

		var chkAutoScroll = E('input', {
			'type': 'checkbox',
			'id': 'ipselect-autoscroll-chk',
			'checked': true,
			'style': 'vertical-align:middle; margin-right:4px;'
		});

		var logWindow = E('pre', {
			'id': 'ipselect-log-pre',
			'class': 'ipselect-terminal',
			'style': 'background:#1e1e1e; color:#d4d4d4; font-family:"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size:12px; line-height:1.5; padding:14px; border-radius:5px; height:450px; overflow-y:auto; white-space:pre-wrap; word-break:break-all; border:1px solid #333; margin-top:12px; box-shadow:inset 0 2px 6px rgba(0,0,0,0.6);'
		}, [_('正在准备日志终端...')]);

		function updateButtonsState(running) {
			btnStart.disabled = !!running;
			btnStop.disabled = !running;
			if (running) {
				btnStart.classList.add('disabled');
				btnStop.classList.remove('disabled');
			} else {
				btnStart.classList.remove('disabled');
				btnStop.classList.add('disabled');
			}
		}

		updateButtonsState(self.isTaskRunning);

		var tabConsole = E('div', {
			'data-tab': 'console',
			'data-tab-title': _('控制台与实时日志')
		}, [
			E('div', { 'style': 'display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; margin-bottom:8px;' }, [
				E('div', {}, [btnStart, btnStop, btnClear]),
				E('div', { 'style': 'font-size:13px; color:#555;' }, [
					chkAutoScroll,
					E('label', { 'for': 'ipselect-autoscroll-chk', 'style': 'cursor:pointer;' }, [_('自动滚屏到底部')])
				])
			]),
			logWindow
		]);

		/* ==================== Tab 2: 优选结果展示 ==================== */
		var resSummaryBar = E('div', {
			'class': 'cbi-value-description',
			'style': 'display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; margin-bottom:12px; padding:8px 12px; background:#f1f3f5; border-radius:4px; font-size:13px;'
		});

		var resultsTable = E('table', { 'class': 'table cbi-section-table' }, [
			E('thead', { 'class': 'thead cbi-section-thead' }, [
				E('tr', { 'class': 'tr cbi-section-table-titles' }, [
					E('th', { 'class': 'th center', 'style': 'width:60px;' }, [_('序号')]),
					E('th', { 'class': 'th' }, [_('节点 IP')]),
					E('th', { 'class': 'th', 'style': 'width:80px;' }, [_('端口')]),
					E('th', { 'class': 'th' }, [_('节点备注 / 机房标签')]),
					E('th', { 'class': 'th center', 'style': 'width:100px;' }, [_('优选状态')])
				])
			]),
			E('tbody', { 'class': 'tbody cbi-section-tbody' })
		]);

		var currentNodesList = resultsData.nodes || [];

		function renderResultsTable(res) {
			var tbody = resultsTable.querySelector('tbody');
			dom.content(tbody, null);

			currentNodesList = res.nodes || [];
			var updateTime = res.update_time || _('从未生成');
			var fileName = res.file || 'best_us.txt';
			var count = currentNodesList.length;

			dom.content(resSummaryBar, [
				E('div', {}, [
					E('span', { 'style': 'margin-right:16px;' }, [
						_('结果文件：'),
						E('strong', {}, [fileName])
					]),
					E('span', { 'style': 'margin-right:16px;' }, [
						_('更新时间：'),
						E('strong', {}, [updateTime])
					]),
					E('span', {}, [
						_('有效节点数：'),
						E('strong', { 'style': 'color:#28a745;' }, [count + ' ' + _('个')])
					])
				]),
				E('div', {}, [
					E('button', {
						'class': 'btn cbi-button-action',
						'style': 'margin-right:8px;',
						'click': function() {
							if (currentNodesList.length === 0) {
								ui.addNotification(null, E('p', _('当前没有节点可供复制')), 'warning');
								return;
							}
							var textLines = currentNodesList.map(function(n) {
								return n.ip + ':' + n.port + '#' + n.remark;
							}).join('\n');
							copyToClipboard(textLines, _('已复制全部 ') + currentNodesList.length + _(' 个节点到剪贴板！'));
						}
					}, ['📋 ' + _('一键复制全部节点')]),
					E('button', {
						'class': 'btn cbi-button-positive',
						'style': 'margin-right:8px;',
						'click': function() {
							if (currentNodesList.length === 0) {
								ui.addNotification(null, E('p', _('当前没有节点可供下载')), 'warning');
								return;
							}
							var textLines = currentNodesList.map(function(n) {
								return n.ip + ':' + n.port + '#' + n.remark;
							}).join('\n');
							downloadTextFile(fileName, textLines);
						}
					}, ['💾 ' + _('下载 txt 文件')]),
					E('button', {
						'class': 'btn cbi-button-neutral',
						'click': function() {
							self.refreshResults();
						}
					}, ['🔄 ' + _('刷新')])
				])
			]);

			if (currentNodesList.length === 0) {
				tbody.appendChild(E('tr', { 'class': 'tr cbi-section-table-row placeholder' }, [
					E('td', { 'class': 'td center', 'colspan': 5, 'style': 'padding:24px;' }, [
						E('em', { 'style': 'color:#888;' }, [_('暂无优选节点数据，请在【控制台】中点击立即优选进行测速')])
					])
				]));
				return;
			}

			for (var i = 0; i < currentNodesList.length; i++) {
				var node = currentNodesList[i];
				var rowClass = (i % 2 === 0) ? 'cbi-rowstyle-1' : 'cbi-rowstyle-2';
				tbody.appendChild(E('tr', { 'class': 'tr cbi-section-table-row ' + rowClass }, [
					E('td', { 'class': 'td center', 'style': 'font-weight:600;' }, [String(node.index || (i + 1))]),
					E('td', { 'class': 'td' }, [
						E('code', { 'style': 'font-size:13px; color:#0056b3;' }, [node.ip])
					]),
					E('td', { 'class': 'td' }, [String(node.port)]),
					E('td', { 'class': 'td' }, [node.remark || _('未命名')]),
					E('td', { 'class': 'td center' }, [
						E('span', { 'class': 'badge', 'style': 'background:#28a745;color:#fff;padding:2px 8px;border-radius:3px;font-size:12px;' }, ['🟢 ' + _('优选')])
					])
				]));
			}
		}

		renderResultsTable(resultsData);

		var tabResults = E('div', {
			'data-tab': 'results',
			'data-tab-title': _('优选结果')
		}, [
			resSummaryBar,
			resultsTable
		]);

		/* ==================== Tab 3: 任务执行历史记录 ==================== */
		var historyTable = E('table', { 'class': 'table cbi-section-table' }, [
			E('thead', { 'class': 'thead cbi-section-thead' }, [
				E('tr', { 'class': 'tr cbi-section-table-titles' }, [
					E('th', { 'class': 'th', 'style': 'width:130px;' }, [_('任务编号')]),
					E('th', { 'class': 'th', 'style': 'width:160px;' }, [_('触发时间')]),
					E('th', { 'class': 'th center', 'style': 'width:90px;' }, [_('触发源')]),
					E('th', { 'class': 'th center', 'style': 'width:90px;' }, [_('执行耗时')]),
					E('th', { 'class': 'th center', 'style': 'width:80px;' }, [_('节点数')]),
					E('th', { 'class': 'th center', 'style': 'width:120px;' }, [_('GitHub 推送')]),
					E('th', { 'class': 'th center', 'style': 'width:120px;' }, [_('OpenClash')]),
					E('th', { 'class': 'th center', 'style': 'width:90px;' }, [_('最终状态')]),
					E('th', { 'class': 'th center', 'style': 'width:100px;' }, [_('操作')])
				])
			]),
			E('tbody', { 'class': 'tbody cbi-section-tbody' })
		]);

		function renderHistoryTable(histList) {
			var tbody = historyTable.querySelector('tbody');
			dom.content(tbody, null);

			var list = Array.isArray(histList) ? histList : [];
			if (list.length === 0) {
				tbody.appendChild(E('tr', { 'class': 'tr cbi-section-table-row placeholder' }, [
					E('td', { 'class': 'td center', 'colspan': 9, 'style': 'padding:24px;' }, [
						E('em', { 'style': 'color:#888;' }, [_('暂无历史执行记录')])
					])
				]));
				return;
			}

			for (var i = 0; i < list.length; i++) {
				var item = list[i];
				var rowClass = (i % 2 === 0) ? 'cbi-rowstyle-1' : 'cbi-rowstyle-2';
				var triggerText = (item.trigger === 'cron') ? ('⏰ ' + _('定时任务')) : ('👤 ' + _('手动触发'));

				var btnViewLog = E('button', {
					'class': 'btn cbi-button-action',
					'click': (function(rec) {
						return function() {
							self.showHistoryLogModal(rec);
						};
					})(item)
				}, [_('查看日志')]);

				tbody.appendChild(E('tr', { 'class': 'tr cbi-section-table-row ' + rowClass }, [
					E('td', { 'class': 'td' }, [
						E('code', { 'style': 'font-size:12px;' }, [item.id])
					]),
					E('td', { 'class': 'td' }, [item.timestamp || '-']),
					E('td', { 'class': 'td center' }, [triggerText]),
					E('td', { 'class': 'td center' }, [formatDuration(item.duration)]),
					E('td', { 'class': 'td center', 'style': 'font-weight:600;' }, [String(item.nodeCount || 0)]),
					E('td', { 'class': 'td center' }, [getGitBadge(item.gitStatus)]),
					E('td', { 'class': 'td center' }, [getOcBadge(item.openclashStatus)]),
					E('td', { 'class': 'td center' }, [getStatusBadge(item.status)]),
					E('td', { 'class': 'td center' }, [btnViewLog])
				]));
			}
		}

		renderHistoryTable(historyData);

		var tabHistory = E('div', {
			'data-tab': 'history',
			'data-tab-title': _('历史档案')
		}, [
			E('div', { 'style': 'display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;' }, [
				E('span', { 'class': 'cbi-value-description' }, [_('归档展示最近 10 次节点优选任务的执行快照与联动审计记录：')]),
				E('button', {
					'class': 'btn cbi-button-neutral',
					'click': function() {
						self.refreshHistory();
					}
				}, ['🔄 ' + _('刷新历史')])
			]),
			historyTable
		]);

		/* ==================== Tab 4: 基础设置 (form.Map 绑定 UCI) ==================== */
		var m = new form.Map('ipselect', _('基础与系统联动设置'), _('配置节点优选运行目录、测速环境标签、出站网卡、GitHub 自动推送以及 OpenClash 与定时任务联动规则。'));
		var s = m.section(form.NamedSection, 'config', 'ipselect');
		s.anonymous = false;
		s.addremove = false;

		var o;

		/* 1. enabled */
		o = s.option(form.Flag, 'enabled', _('启用服务'), _('总开关，开启后生效定时优选任务与系统后台守护'));
		o.default = '1';
		o.rmempty = false;

		/* 2. workdir */
		o = s.option(form.Value, 'workdir', _('工作目录路径'), _('优选测速脚本与 Git 仓库所在的本地绝对路径 (必须包含 scripts/filter_us_nodes.py)'));
		o.default = '/root/ipselect';
		o.placeholder = '/root/ipselect';
		o.rmempty = false;

		/* 3. env_tag */
		o = s.option(form.Value, 'env_tag', _('运行环境标签'), _('自定义网络或地理环境标识（如“公司”、“家庭”、“软路由”），该标签将直接注入到测速输出的节点名称中，例如：US-DaTree-95.0-01-公司'));
		o.default = '公司';
		o.placeholder = '公司';
		o.rmempty = false;

		/* 4. output_file */
		o = s.option(form.Value, 'output_file', _('优选输出文件名'), _('测速结果保存的目标文件名 (带有仓库相对路径，如 best_us.txt)'));
		o.default = 'best_us.txt';
		o.placeholder = 'best_us.txt';
		o.rmempty = false;

		/* 5. interface */
		o = s.option(form.Value, 'interface', _('出站网络接口'), _('执行节点筛选与真实测速时绑定的网络物理接口'));
		o.value('br-lan', 'br-lan (' + _('默认局域网桥') + ')');
		o.value('eth0', 'eth0');
		o.value('eth1', 'eth1');
		o.value('pppoe-wan', 'pppoe-wan');
		o.default = 'br-lan';
		o.rmempty = false;

		/* 6. auto_push */
		o = s.option(form.Flag, 'auto_push', _('自动推送到 GitHub'), _('测速完成且节点文件有变动时，自动提交并 push 到远端仓库'));
		o.default = '1';

		/* 6.1 github_repo */
		o = s.option(form.Value, 'github_repo', _('GitHub 仓库名'), _('格式为：用户名/仓库名，例如 cuiyuanqing/ipselect'));
		o.placeholder = 'cuiyuanqing/ipselect';
		o.default = 'cuiyuanqing/ipselect';
		o.depends('auto_push', '1');

		/* 6.2 github_token */
		o = s.option(form.Value, 'github_token', _('GitHub 访问令牌 (Token)'), _('GitHub Personal Access Token (PAT)，需具备 repo 读写权限'));
		o.password = true;
		o.placeholder = 'github_pat_...';
		o.depends('auto_push', '1');

		/* 6.3 github_branch */
		o = s.option(form.Value, 'github_branch', _('GitHub 目标分支'), _('推送的目标分支名，默认为 main'));
		o.default = 'main';
		o.placeholder = 'main';
		o.depends('auto_push', '1');

		/* 6.4 github_user */
		o = s.option(form.Value, 'github_user', _('Git 提交用户名'), _('Git commit 提交记录的作者姓名，例如 cuiyuanqing'));
		o.default = 'cuiyuanqing';
		o.placeholder = 'cuiyuanqing';
		o.depends('auto_push', '1');

		/* 6.5 github_email */
		o = s.option(form.Value, 'github_email', _('Git 提交邮箱'), _('Git commit 提交记录的作者邮箱，例如 759666247@qq.com'));
		o.default = '759666247@qq.com';
		o.placeholder = '759666247@qq.com';
		o.depends('auto_push', '1');

		/* 6.6 auto_clone */
		o = s.option(form.Flag, 'auto_clone', _('工作目录缺失时自动克隆'), _('若本地工作目录不存在或未初始化，测速启动时自动通过 Token 克隆远端仓库'));
		o.default = '1';
		o.depends('auto_push', '1');

		/* 6.7 _test_github */
		var btnCls = form.DummyValue || form.Button || form.Value;
		o = s.option(btnCls, '_test_github', _('测试 GitHub 连接'), _('验证当前配置的 GitHub 仓库与 Token 权限是否有效（测试前请先保存并应用配置）'));
		o.depends('auto_push', '1');
		o.renderWidget = function(section_id) {
			var testBtn = E('button', {
				'type': 'button',
				'class': 'btn cbi-button cbi-button-action',
				'style': 'padding:4px 14px; font-size:13px; cursor:pointer;'
			}, ['🔍 ' + _('测试连接')]);

			var statusInline = E('span', {
				'style': 'margin-left:12px; font-size:13px; vertical-align:middle; display:none;'
			});

			var resultBox = E('div', {
				'style': 'margin-top:10px; display:none; padding:10px 14px; border-radius:4px; font-size:13px; line-height:1.5; border:1px solid #ced4da; word-break:break-all;'
			});

			testBtn.addEventListener('click', function(ev) {
				testBtn.disabled = true;
				testBtn.textContent = '⏳ ' + _('正在测试连接...');
				statusInline.style.display = 'inline-block';
				statusInline.style.color = '#0072c6';
				statusInline.style.fontWeight = 'bold';
				statusInline.textContent = '⏳ ' + _('正在与 GitHub 远程服务器通信...');

				resultBox.style.display = 'block';
				resultBox.style.background = '#e8f4fd';
				resultBox.style.borderColor = '#b8daff';
				resultBox.style.color = '#004085';
				dom.content(resultBox, E('div', {}, [
					E('div', { 'style': 'font-weight:bold; margin-bottom:4px;' }, ['⏳ ' + _('正在测试 GitHub 连接...')]),
					E('div', {}, [_('正在发起远程分支与访问令牌 (Token) 探测，请稍候...')])
				]));

				request.post(getApiUrl('test_github')).then(function(res) {
					testBtn.disabled = false;
					testBtn.textContent = '🔍 ' + _('测试连接');
					var body = res.json();
					var now = new Date();
					var timeStr = (now.getHours() < 10 ? '0' + now.getHours() : now.getHours()) + ':' +
						(now.getMinutes() < 10 ? '0' + now.getMinutes() : now.getMinutes()) + ':' +
						(now.getSeconds() < 10 ? '0' + now.getSeconds() : now.getSeconds());

					if (body && body.success) {
						statusInline.style.color = '#28a745';
						statusInline.textContent = '✅ ' + _('连接成功');

						resultBox.style.background = '#d4edda';
						resultBox.style.borderColor = '#c3e6cb';
						resultBox.style.color = '#155724';
						dom.content(resultBox, E('div', {}, [
							E('div', { 'style': 'font-weight:bold; margin-bottom:4px;' }, [
								'✅ ' + _('GitHub 连通性与权限校验成功！') + ' (' + timeStr + ')'
							]),
							E('div', {}, [body.message || _('远程分支鉴权通过，自动推仓功能正常就绪。')])
						]));
					} else {
						statusInline.style.color = '#dc3545';
						statusInline.textContent = '❌ ' + _('连接失败');

						resultBox.style.background = '#f8d7da';
						resultBox.style.borderColor = '#f5c6cb';
						resultBox.style.color = '#721c24';
						dom.content(resultBox, E('div', {}, [
							E('div', { 'style': 'font-weight:bold; margin-bottom:4px;' }, [
								'❌ ' + _('GitHub 连接或鉴权失败') + ' (' + timeStr + ')'
							]),
							E('div', { 'style': 'margin-top:4px;' }, [
								(body && body.message) ? body.message : _('未能连接到目标 GitHub 仓库，请检查仓库名、Token 或网络连通性。')
							])
						]));
					}
				}).catch(function(err) {
					testBtn.disabled = false;
					testBtn.textContent = '🔍 ' + _('测试连接');
					statusInline.style.color = '#dc3545';
					statusInline.textContent = '❌ ' + _('请求异常');

					resultBox.style.background = '#f8d7da';
					resultBox.style.borderColor = '#f5c6cb';
					resultBox.style.color = '#721c24';
					dom.content(resultBox, E('div', {}, [
						E('div', { 'style': 'font-weight:bold; margin-bottom:4px;' }, [
							'❌ ' + _('网络或接口请求异常')
						]),
						E('div', { 'style': 'margin-top:4px;' }, [
							err.message || String(err)
						])
					]));
				});
			});

			return E('div', { 'class': 'cbi-value-field' }, [
				E('div', { 'style': 'display:flex; align-items:center; flex-wrap:wrap; gap:8px;' }, [
					testBtn,
					statusInline
				]),
				resultBox
			]);
		};

		/* 7. openclash_sync */
		o = s.option(form.Flag, 'openclash_sync', _('联动更新 OpenClash'), _('优选完成后自动触发 OpenClash 对应订阅提供商热重载'));
		o.default = '1';

		/* 8. openclash_provider */
		o = s.option(form.Value, 'openclash_provider', _('OpenClash 订阅提供商名称'), _('OpenClash 配置文件中 Proxy Provider 的标识名称'));
		o.default = 'datree';
		o.placeholder = 'datree';
		o.depends('openclash_sync', '1');

		/* 9. cron_enabled */
		o = s.option(form.Flag, 'cron_enabled', _('启用定时优选任务'), _('每天定时自动在后台执行测速筛选与推仓同步'));
		o.default = '1';

		/* 10. 可视化 Cron 操作生成器 (放在 Cron 表达式上方) */
		var dummyCls = form.DummyValue || form.Value;
		var cronGen = s.option(dummyCls, '_cron_generator', _('Cron 调度操作生成器'));
		cronGen.depends('cron_enabled', '1');
		cronGen.renderWidget = function(section_id) {
			var initExpr = uci.get('ipselect', 'config', 'cron_expression') || '30 4 * * *';

			/* 解析初始选中小时 */
			var selectedHours = new Set();
			var selectedMinute = '30';
			if (initExpr) {
				var p = initExpr.trim().split(/\s+/);
				if (p.length >= 2) {
					selectedMinute = p[0] || '30';
					var hrs = (p[1] || '').split(',');
					hrs.forEach(function(hh) {
						var n = parseInt(hh, 10);
						if (!isNaN(n) && n >= 0 && n <= 23) selectedHours.add(n);
					});
				}
			}
			if (selectedHours.size === 0) selectedHours.add(4);

			/* 解释器徽章 */
			var explainBox = E('div', {
				'class': 'alert-message info',
				'style': 'margin-bottom:12px; padding:10px 14px; background:#e8f4fd; border:1px solid #b8daff; border-radius:4px; color:#004085; font-size:13px;'
			}, ['🕒 ' + _('当前计划解读：') + describeCron(initExpr)]);

			function syncCronToForm(newExpr) {
				explainBox.textContent = '🕒 ' + _('当前计划解读：') + describeCron(newExpr);
				var inputExpr = document.getElementById('widget.cbid.ipselect.config.cron_expression') ||
					document.querySelector('input[id*="cron_expression"]') ||
					document.querySelector('input[name*="cron_expression"]');
				if (inputExpr) {
					inputExpr.value = newExpr;
					inputExpr.dispatchEvent(new Event('input', { bubbles: true }));
					inputExpr.dispatchEvent(new Event('change', { bubbles: true }));
				}
			}

			/* 1. 每日多时间点模式控件 */
			var minuteSelect = E('select', {
				'class': 'cbi-input-select',
				'style': 'width:100px; display:inline-block; margin-right:12px;',
				'change': function(ev) {
					selectedMinute = ev.target.value;
					updateDailyCron();
				}
			}, [
				E('option', { 'value': '0' }, ['00 ' + _('分')]),
				E('option', { 'value': '15' }, ['15 ' + _('分')]),
				E('option', { 'value': '30', 'selected': (selectedMinute === '30' ? 'selected' : null) }, ['30 ' + _('分')]),
				E('option', { 'value': '45' }, ['45 ' + _('分')])
			]);

			var hourButtons = [];
			var gridContainer = E('div', {
				'style': 'display:grid; grid-template-columns:repeat(auto-fill, minmax(48px, 1fr)); gap:6px; margin-top:8px; margin-bottom:10px;'
			});

			function updateHourBtnStyle(btn, isSel) {
				if (isSel) {
					btn.style.background = '#0072c6';
					btn.style.color = '#ffffff';
					btn.style.borderColor = '#005b9f';
					btn.style.fontWeight = 'bold';
				} else {
					btn.style.background = '#f8f9fa';
					btn.style.color = '#333333';
					btn.style.borderColor = '#ced4da';
					btn.style.fontWeight = 'normal';
				}
			}

			function updateDailyCron() {
				var sortedHours = Array.from(selectedHours).sort(function(a, b) { return a - b; });
				if (sortedHours.length === 0) {
					sortedHours = [4];
					selectedHours.add(4);
				}
				var hourStr = sortedHours.join(',');
				var expr = selectedMinute + ' ' + hourStr + ' * * *';
				syncCronToForm(expr);
			}

			for (var i = 0; i < 24; i++) {
				(function(hour) {
					var hStr = (hour < 10 ? '0' + hour : String(hour)) + ':00';
					var isSel = selectedHours.has(hour);
					var btn = E('button', {
						'type': 'button',
						'class': 'btn',
						'style': 'padding:6px 0; border:1px solid #ced4da; border-radius:4px; font-size:12px; cursor:pointer;',
						'click': function() {
							if (selectedHours.has(hour)) {
								if (selectedHours.size > 1) selectedHours.delete(hour);
							} else {
								selectedHours.add(hour);
							}
							updateHourBtnStyle(btn, selectedHours.has(hour));
							updateDailyCron();
						}
					}, [hStr]);
					updateHourBtnStyle(btn, isSel);
					hourButtons.push(btn);
					gridContainer.appendChild(btn);
				})(i);
			}

			var quickPresets = E('div', { 'style': 'margin-top:6px; margin-bottom:12px;' }, [
				E('span', { 'style': 'color:#666; font-size:12px; margin-right:8px;' }, [_('快捷选择：')]),
				E('button', {
					'type': 'button',
					'class': 'btn cbi-button-neutral',
					'style': 'margin-right:6px; font-size:12px; padding:2px 8px;',
					'click': function() {
						selectedHours.clear();
						[3, 8, 9, 12].forEach(function(h) { selectedHours.add(h); });
						for (var h = 0; h < 24; h++) updateHourBtnStyle(hourButtons[h], selectedHours.has(h));
						updateDailyCron();
					}
				}, ['⭐ 常用四次 (3, 8, 9, 12 点)']),
				E('button', {
					'type': 'button',
					'class': 'btn cbi-button-neutral',
					'style': 'margin-right:6px; font-size:12px; padding:2px 8px;',
					'click': function() {
						selectedHours.clear();
						[8, 20].forEach(function(h) { selectedHours.add(h); });
						for (var h = 0; h < 24; h++) updateHourBtnStyle(hourButtons[h], selectedHours.has(h));
						updateDailyCron();
					}
				}, ['☀️ 早晚两次 (8, 20 点)']),
				E('button', {
					'type': 'button',
					'class': 'btn cbi-button-neutral',
					'style': 'margin-right:6px; font-size:12px; padding:2px 8px;',
					'click': function() {
						selectedHours.clear();
						for (var h = 0; h < 24; h += 2) selectedHours.add(h);
						for (var h = 0; h < 24; h++) updateHourBtnStyle(hourButtons[h], selectedHours.has(h));
						updateDailyCron();
					}
				}, ['🔄 偶数整点 (每2小时)']),
				E('button', {
					'type': 'button',
					'class': 'btn cbi-button-neutral',
					'style': 'font-size:12px; padding:2px 8px;',
					'click': function() {
						selectedHours.clear();
						selectedHours.add(4);
						for (var h = 0; h < 24; h++) updateHourBtnStyle(hourButtons[h], selectedHours.has(h));
						updateDailyCron();
					}
				}, ['🌙 凌晨默认 (04:30)'])
			]);

			/* 2. 间隔循环选择 */
			var intervalSelect = E('select', {
				'class': 'cbi-input-select',
				'style': 'width:260px; margin-top:6px;',
				'change': function(ev) {
					syncCronToForm(ev.target.value);
				}
			}, [
				E('option', { 'value': '0 */2 * * *' }, ['每 2 小时整点 (0 */2 * * *)']),
				E('option', { 'value': '0 */3 * * *' }, ['每 3 小时整点 (0 */3 * * *)']),
				E('option', { 'value': '0 */4 * * *' }, ['每 4 小时整点 (0 */4 * * *)']),
				E('option', { 'value': '0 */6 * * *' }, ['每 6 小时整点 (0 */6 * * *)']),
				E('option', { 'value': '0 */8 * * *' }, ['每 8 小时整点 (0 */8 * * *)']),
				E('option', { 'value': '0 */12 * * *' }, ['每 12 小时整点 (0 */12 * * *)']),
				E('option', { 'value': '*/30 * * * *' }, ['每 30 分钟 (*/30 * * * *)']),
				E('option', { 'value': '*/15 * * * *' }, ['每 15 分钟 (*/15 * * * *)']),
				E('option', { 'value': '0 * * * *' }, ['每 1 小时整点 (0 * * * *)'])
			]);

			/* 监听下方 cron_expression 输入框的手动改动并实时更新说明解读文案 */
			var checkTimer = setInterval(function() {
				var inputExpr = document.getElementById('widget.cbid.ipselect.config.cron_expression') ||
					document.querySelector('input[id*="cron_expression"]');
				if (inputExpr && !inputExpr._explainHooked) {
					inputExpr._explainHooked = true;
					inputExpr.addEventListener('input', function(e) {
						explainBox.textContent = '🕒 ' + _('当前计划解读：') + describeCron(e.target.value);
					});
					clearInterval(checkTimer);
				}
			}, 300);
			setTimeout(function() { clearInterval(checkTimer); }, 5000);

			return E('div', {
				'class': 'cbi-value-field',
				'style': 'background:#fafafa; border:1px solid #e2e8f0; border-radius:6px; padding:16px;'
			}, [
				explainBox,
				E('div', { 'style': 'margin-bottom:8px;' }, [
					E('strong', {}, [_('【每日多时间点网格】')]),
					E('span', { 'style': 'color:#666; font-size:12px; margin-left:8px;' }, [_('选择执行分钟并在下方多选需要更新的小时：')]),
					minuteSelect
				]),
				gridContainer,
				quickPresets,
				E('div', { 'style': 'margin-top:12px; padding-top:12px; border-top:1px dashed #ddd;' }, [
					E('strong', {}, [_('【周期循环执行预设】')]),
					E('span', { 'style': 'color:#666; font-size:12px; margin-left:8px;' }, [_('若使用周期循环，可在此选取常见间隔表达式：')]),
					E('div', {}, [intervalSelect])
				])
			]);
		};

		/* 11. cron_expression (放置在生成器下方) */
		var cronExprOpt = s.option(form.Value, 'cron_expression', _('Cron 调度表达式'), _('生成的标准 5 段 Crontab 表达式 (分 时 日 月 周)，上方生成器将实时联动更新，亦可直接手动编辑'));
		cronExprOpt.default = '30 4 * * *';
		cronExprOpt.placeholder = '30 4 * * *';
		cronExprOpt.rmempty = false;
		cronExprOpt.depends('cron_enabled', '1');

		var tabSettings = E('div', {
			'data-tab': 'settings',
			'data-tab-title': _('基础设置')
		});

		/* 组装选项卡容器 */
		var tabsContainer = E('div', { 'class': 'cbi-map-tabbed' }, [
			tabConsole,
			tabResults,
			tabHistory,
			tabSettings
		]);

		/* 绑定视图级操作与刷新句柄 */
		self.updateButtonsState = updateButtonsState;
		self.updateDashboardStatus = updateDashboardStatus;
		self.renderResultsTable = renderResultsTable;
		self.renderHistoryTable = renderHistoryTable;
		self.logWindow = logWindow;
		self.chkAutoScroll = chkAutoScroll;

		/* 初次加载日志内容 */
		self.fetchLogInitial();

		/* 若当前正在运行，立刻启动定时轮询 */
		if (self.isTaskRunning) {
			self.startLogPolling();
		}

		/* 渲染 settings form 并挂载 */
		return m.render().then(function(mapNode) {
			tabSettings.appendChild(mapNode);

			var wrapper = E('div', { 'class': 'ipselect-view-wrapper' }, [
				headerTitle,
				headerDesc,
				dashboardCard,
				tabsContainer
			]);

			/* 初始化 Tab 选项卡组件 (必须在 tabsContainer 挂载到父容器 wrapper 之后调用，确保 group.parentNode 存在) */
			ui.tabs.initTabGroup(tabsContainer.childNodes);

			return wrapper;
		});

	},

	fetchLogInitial: function() {
		var self = this;
		request.get(getApiUrl('get_log'), { query: { offset: '0' } }).then(function(res) {
			var text = res.text();
			if (text && text.trim().length > 0) {
				self.logWindow.textContent = text;
				self.logOffset = new Blob([text]).size;
			} else {
				self.logWindow.textContent = _('暂无日志记录。点击【立即开始优选】启动任务。');
				self.logOffset = 0;
			}
			if (self.chkAutoScroll && self.chkAutoScroll.checked) {
				self.logWindow.scrollTop = self.logWindow.scrollHeight;
			}
		}).catch(function() {
			self.logWindow.textContent = _('读取日志失败，请检查后端执行状态。');
		});
	},

	startLogPolling: function() {
		var self = this;
		if (self.pollTimer)
			return;

		self.pollTimer = window.setInterval(function() {
			/* 1. 增量拉取日志 */
			request.get(getApiUrl('get_log'), { query: { offset: String(self.logOffset) } }).then(function(res) {
				var chunk = res.text();
				if (chunk && chunk.length > 0) {
					if (self.logWindow.textContent.indexOf(_('暂无日志记录')) === 0 || self.logWindow.textContent.indexOf(_('正在准备')) === 0) {
						self.logWindow.textContent = '';
					}
					self.logWindow.textContent += chunk;
					self.logOffset += new Blob([chunk]).size;
					if (self.chkAutoScroll && self.chkAutoScroll.checked) {
						self.logWindow.scrollTop = self.logWindow.scrollHeight;
					}
				}
			}).catch(function() {});

			/* 2. 检查运行状态 */
			request.get(getApiUrl('get_status')).then(function(res) {
				var st = res.json();
				var wasRunning = self.isTaskRunning;
				self.isTaskRunning = !!st.running;
				self.updateButtonsState(self.isTaskRunning);
				self.updateDashboardStatus(st);

				if (wasRunning && !st.running) {
					/* 任务执行结束 */
					self.stopLogPolling();
					ui.addNotification(null, E('p', _('节点优选任务已执行完成！')), 'info');
					self.refreshResults();
					self.refreshHistory();
				}
			}).catch(function() {});
		}, 1500);
	},

	stopLogPolling: function() {
		if (this.pollTimer) {
			window.clearInterval(this.pollTimer);
			this.pollTimer = null;
		}
	},

	handleStart: function(ev) {
		var self = this;
		self.updateButtonsState(true);
		self.isTaskRunning = true;
		self.updateDashboardStatus({ running: true, pid: '-' });

		request.post(getApiUrl('start')).then(function(res) {
			var body = res.json();
			if (body && body.success) {
				ui.addNotification(null, E('p', body.message || _('任务已成功启动！')), 'info');
				self.startLogPolling();
			} else {
				self.isTaskRunning = false;
				self.updateButtonsState(false);
				self.updateDashboardStatus({ running: false, pid: 0 });
				ui.addNotification(null, E('p', (body && body.message) ? body.message : _('启动任务失败')), 'danger');
			}
		}).catch(function(err) {
			self.isTaskRunning = false;
			self.updateButtonsState(false);
			self.updateDashboardStatus({ running: false, pid: 0 });
			ui.addNotification(null, E('p', _('网络请求异常: ') + (err.message || String(err))), 'danger');
		});
	},

	handleStop: function(ev) {
		var self = this;
		ui.showModal(_('确认终止任务'), [
			E('p', _('确定要强制中断当前正在运行的优选测速任务吗？已测速的数据将不会提交。')),
			E('div', { 'class': 'right' }, [
				E('button', {
					'class': 'btn cbi-button-neutral',
					'click': ui.hideModal
				}, [_('取消')]),
				' ',
				E('button', {
					'class': 'btn cbi-button-negative',
					'click': function() {
						ui.hideModal();
						request.post(getApiUrl('stop')).then(function(res) {
							var body = res.json();
							if (body && body.success) {
								ui.addNotification(null, E('p', body.message || _('任务已成功终止')), 'warning');
							} else {
								ui.addNotification(null, E('p', (body && body.message) ? body.message : _('终止失败')), 'warning');
							}
							self.stopLogPolling();
							self.isTaskRunning = false;
							self.updateButtonsState(false);
							self.updateDashboardStatus({ running: false, pid: 0 });
							self.refreshHistory();
						}).catch(function(err) {
							ui.addNotification(null, E('p', _('请求失败: ') + (err.message || String(err))), 'danger');
						});
					}
				}, [_('确认终止')])
			])
		]);
	},

	handleClearLog: function(ev) {
		var self = this;
		request.post(getApiUrl('clear_log')).then(function(res) {
			self.logWindow.textContent = '';
			self.logOffset = 0;
			ui.addNotification(null, E('p', _('日志已清空')), 'info');
		}).catch(function(err) {
			ui.addNotification(null, E('p', _('清空日志失败: ') + (err.message || String(err))), 'danger');
		});
	},

	refreshResults: function() {
		var self = this;
		return request.get(getApiUrl('get_results')).then(function(res) {
			var body = res.json();
			self.renderResultsTable(body);
		}).catch(function() {});
	},

	refreshHistory: function() {
		var self = this;
		return Promise.all([
			request.get(getApiUrl('get_status')).then(function(res) { return res.json(); }).catch(function() { return null; }),
			request.get(getApiUrl('get_history')).then(function(res) { return res.json(); }).catch(function() { return []; })
		]).then(function(res) {
			var st = res[0];
			var hist = res[1];
			self.renderHistoryTable(hist);
			if (st) {
				self.updateDashboardStatus(st, hist);
			}
		});
	},

	showHistoryLogModal: function(record) {
		request.get(getApiUrl('get_history_log'), { query: { id: record.id } }).then(function(res) {
			var logContent = res.text() || _('暂无快照日志内容');
			var modalPre = E('pre', {
				'style': 'background:#1e1e1e; color:#d4d4d4; font-family:"SFMono-Regular", Consolas, Menlo, monospace; font-size:12px; line-height:1.45; padding:12px; border-radius:4px; max-height:400px; overflow-y:auto; white-space:pre-wrap; word-break:break-all;'
			}, [logContent]);

			ui.showModal(_('历史执行日志快照 - ') + record.id, [
				E('div', { 'style': 'margin-bottom:12px; font-size:13px; color:#555;' }, [
					E('span', { 'style': 'margin-right:16px;' }, [_('执行时间：') + (record.timestamp || '-')]),
					E('span', { 'style': 'margin-right:16px;' }, [_('耗时：') + formatDuration(record.duration)]),
					E('span', {}, [_('最终状态：') + (record.status || '-')])
				]),
				modalPre,
				E('div', { 'class': 'right', 'style': 'margin-top:14px;' }, [
					E('button', {
						'class': 'btn cbi-button-action',
						'style': 'margin-right:8px;',
						'click': function() {
							copyToClipboard(logContent, _('已复制快照日志到剪贴板！'));
						}
					}, ['📋 ' + _('复制日志')]),
					E('button', {
						'class': 'btn cbi-button-neutral',
						'click': ui.hideModal
					}, [_('关闭')])
				])
			]);
		}).catch(function(err) {
			ui.addNotification(null, E('p', _('获取历史日志失败: ') + (err.message || String(err))), 'danger');
		});
	},

	handleSaveApply: function(ev, mode) {
		return this.handleSave(ev).then(function() {
			return ui.changes.apply(mode == '0');
		}).then(function() {
			return fs.exec('/etc/init.d/ipselect', ['restart']).catch(function() {});
		}).then(function() {
			ui.addNotification(null, E('p', _('配置已成功保存并应用，系统服务与定时任务已同步重载！')), 'info');
		});
	}
});
