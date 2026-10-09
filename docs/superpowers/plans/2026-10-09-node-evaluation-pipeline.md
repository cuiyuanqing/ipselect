# Cloudflare 节点四阶漏斗严选引擎与多维质量评估模型 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重构 `scripts/filter_us_nodes.py`，实现网络环境自动感知（家庭 vs 公司）、四阶漏斗探测（快速 TCP 探针、`cf-ray` 机房质检、多轮打散稳定性测试、安全微吞吐测速）及严苛加权评分模型，选出真正高品质、低延迟、零丢包的美区节点。

**Architecture:** 采用漏斗式分层递进架构：初筛快速淘汰不可达节点；次筛通过 TLS 握手解析 `cf-ray` 落地机房识别美西核心机房；三筛多轮打散测试验证 0 丢包与抖动中位数；四筛对入围 Top 20 节点执行轻量 1.5MB 真实微吞吐测速，彻底替换伪测速公式并杜绝官方限流。

**Tech Stack:** Python 3 标准库（`socket`, `ssl`, `urllib.request`, `concurrent.futures`, `ipaddress`, `argparse`, `time`, `os`, `sys`, `math`, `statistics`），零外部三方库依赖。

**Spec:** [docs/superpowers/specs/2026-10-09-node-evaluation-pipeline-design.md](file:///D:/nice_wall/ipselect/docs/superpowers/specs/2026-10-09-node-evaluation-pipeline-design.md)

## Global Constraints

- 运行环境兼容：必须在 Windows、Linux（OpenWrt/iStoreOS）及 GitHub Actions Ubuntu 环境无缝运行。
- 依赖限制：禁止引入 `requests`、`aiohttp`、`numpy` 等外部 pip 依赖，必须使用纯 Python 3 原生标准库。
- 代理绕过兼容：保留 Linux `os.setgid(65534)` 机制以及 `--interface` 出口网卡绑定逻辑。
- 测速安全红线：微吞吐测速单节点传输数据量限制在 1.5MB 且单次超时不超过 2.0s，防止触发 Cloudflare 官方 Rate Limit (429)。
- 命名契约：节点输出保持 `IP:PORT#US-DaTree-{评分}-{序号}-{tag} {真实速度}MB/s` 规范。

## Review Focus

1. **本地 IP 匹配无默认匹配项时的回退**：若机器处于双网卡、虚拟机或离线回环模式，`detect_network_environment` 应稳妥降级为 `公司` / `best_us.txt`，不抛异常崩溃。
2. **Cloudflare `cf-ray` 缺失或非标准响应**：若个别 Anycast IP 或第三方反代返回空 header 或没有 `-Colo` 后缀，安全处理并不产生 `IndexError`。
3. **微吞吐测速极端网络中断或 429 限流**：若下行连接在读取前 100 字节即被对端重置或报 429，捕获异常并降级为前置 TTFB 估算分，保证主流程平稳继续。
4. **并发线程数与 Socket 资源泄漏**：每个 probe 过程创建的 socket 必须有明确的 `finally: s.close()` 或上下文管理器保护，防止 OpenWrt 路由器句柄泄露。
5. **入围节点数不足时的容灾**：当网络极差导致通过机房质检的节点少于 15 个时，安全兜底输出全部合规节点，不引发切片越界。

---

### Task 1: 本地网络环境自动感知与配置推断模块

**Files:**
- Modify: `scripts/filter_us_nodes.py:80-110`
- Test: `tests/test_env_detect.py`

**Interfaces:**
- Consumes: 系统 socket 与网络接口
- Produces: `detect_network_environment(cli_tag: str | None, cli_output: str | None) -> tuple[str, str]`

- [ ] **Step 1: 编写失败测试用例**
  创建 `tests/test_env_detect.py`，测试不同 IP 情况下正确返回 `("家庭", "home_us_best_node.txt")` 或 `("公司", "best_us.txt")`。
- [ ] **Step 2: 运行测试验证失败**
  `python tests/test_env_detect.py`（预期因函数未实现报错）。
- [ ] **Step 3: 实现 `detect_network_environment`**
  在 `scripts/filter_us_nodes.py` 中编写根据出口 IP 判断网段（`192.168.0.*` -> 家庭，`10.10.18.*` -> 公司，其他默认公司）的逻辑，且优先尊重 CLI 传入的显式参数。
- [ ] **Step 4: 运行测试验证通过**
  `python tests/test_env_detect.py`
- [ ] **Step 5: Commit**
  `git add scripts/filter_us_nodes.py tests/test_env_detect.py; git commit -m "feat: 实现基于本地 IP 的网络环境自动感知与配置推断"`

---

### Task 2: 阶段 1 & 阶段 2：快速 TCP 探针与 `cf-ray` 机房质检解析

**Files:**
- Modify: `scripts/filter_us_nodes.py:200-240`
- Test: `tests/test_probe_colo.py`

**Interfaces:**
- Consumes: 候选节点 `(ip, port, original_speed)`
- Produces: 
  - `fast_tcp_ping(ip: str, port: int, timeout: float = 0.8) -> bool`
  - `probe_tls_colo(ip: str, port: int, timeout: float = 2.5) -> tuple[bool, str | None, float | None]`
  - 美西机房优先表 `US_WEST_DCS = {'SJC', 'LAX', 'SFO', 'SEA', 'PDX', 'SLC', 'PHX', 'LAS'}`
  - 全美机房白名单 `ALL_US_DCS`

- [ ] **Step 1: 编写失败测试用例**
  编写 `tests/test_probe_colo.py`，模拟带有 `cf-ray: 8d29b12e3f4a-SJC` 及 `cf-ray: 8d29b12e3f4a-FRA` 的 HTTP 响应解析，验证机房提取与合规过滤。
- [ ] **Step 2: 运行测试验证失败**
  `python tests/test_probe_colo.py`
- [ ] **Step 3: 实现机房解析与快速探针逻辑**
  在 `scripts/filter_us_nodes.py` 中实现 `fast_tcp_ping` 与 `probe_tls_colo`，严格提取 `cf-ray` 中的机房代码并比对美国机房白名单。
- [ ] **Step 4: 运行测试验证通过**
  `python tests/test_probe_colo.py`
- [ ] **Step 5: Commit**
  `git add scripts/filter_us_nodes.py tests/test_probe_colo.py; git commit -m "feat: 实现快速 TCP 探针与 cf-ray 边缘机房质检"`

---

### Task 3: 阶段 3：多轮打散稳定性测试与硬性 0 丢包过滤

**Files:**
- Modify: `scripts/filter_us_nodes.py:240-280`
- Test: `tests/test_stability_probe.py`

**Interfaces:**
- Consumes: 通过机房质检的候选节点列表
- Produces: `probe_stability(ip: str, port: int, rounds: int = 6) -> dict | None`
  返回字典结构：`{'ip', 'port', 'loss_rate', 'median_rtt', 'avg_rtt', 'jitter', 'colo', 'is_us_west'}`

- [ ] **Step 1: 编写失败测试用例**
  创建 `tests/test_stability_probe.py`，模拟多轮 RTT 统计（中位数、极差计算，丢包率拦截）。
- [ ] **Step 2: 运行测试验证失败**
  `python tests/test_stability_probe.py`
- [ ] **Step 3: 实现 `probe_stability`**
  实现 6 轮探测，每轮间隔随机休眠 50~100ms；计算 `median_rtt` 与 `jitter`；丢包 $\ge 2$ 次直接返回 `None` 淘汰。
- [ ] **Step 4: 运行测试验证通过**
  `python tests/test_stability_probe.py`
- [ ] **Step 5: Commit**
  `git add scripts/filter_us_nodes.py tests/test_stability_probe.py; git commit -m "feat: 实现 6 轮打散稳定性测试与 0 丢包准入筛选"`

---

### Task 4: 阶段 4：安全微吞吐测速与真实流速计算

**Files:**
- Modify: `scripts/filter_us_nodes.py:280-320`
- Test: `tests/test_micro_speed.py`

**Interfaces:**
- Consumes: 入围 Top 20 的候选节点 `(ip, port)`
- Produces: `probe_real_download_speed(ip: str, port: int, max_bytes: int = 1572864, max_duration: float = 2.0) -> float`（返回实测速度 MB/s）

- [ ] **Step 1: 编写失败测试用例**
  创建 `tests/test_micro_speed.py`，测试速率计算公式与超时截断逻辑。
- [ ] **Step 2: 运行测试验证失败**
  `python tests/test_micro_speed.py`
- [ ] **Step 3: 实现 `probe_real_download_speed`**
  使用 socket 与 SSL 发送 `GET /__down?bytes=1572864 HTTP/1.1`，设定 2 秒读取超时与 1.5MB 字节上限，实时统计下行字节数与实际耗时，计算真实流速 MB/s。包含异常捕获与保底机制。
- [ ] **Step 4: 运行测试验证通过**
  `python tests/test_micro_speed.py`
- [ ] **Step 5: Commit**
  `git add scripts/filter_us_nodes.py tests/test_micro_speed.py; git commit -m "feat: 实现防限流安全微吞吐测速引擎"`

---

### Task 5: 严苛数学评分模型与输出契约集成

**Files:**
- Modify: `scripts/filter_us_nodes.py:320-390`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: 节点多维测量结果字典
- Produces: 
  - `calculate_rigorous_score(node_data: dict) -> float`
  - `run_filter_and_export(tag=None, output_file=None, interface=None)`

- [ ] **Step 1: 编写失败测试用例**
  创建 `tests/test_scoring.py`，对不同速度（8MB/s vs 1MB/s）、延迟（180ms vs 320ms）、丢包、抖动及机房加成计算得分，验证分数区分度拉开。
- [ ] **Step 2: 运行测试验证失败**
  `python tests/test_scoring.py`
- [ ] **Step 3: 实现严苛评分算法与主管道装配**
  在 `scripts/filter_us_nodes.py` 中实现分段评分公式，并将阶段 1~4 串联为完整的四阶漏斗执行流水线，集成环境自动感知与格式化输出。
- [ ] **Step 4: 运行测试验证通过**
  `python tests/test_scoring.py`
- [ ] **Step 5: Commit**
  `git add scripts/filter_us_nodes.py tests/test_scoring.py; git commit -m "feat: 组装四阶漏斗优选主流程与严苛评分排序机制"`

---

### Task 6: 端到端本地实跑与输出文件兼容性校验

**Files:**
- Modify: `scripts/filter_us_nodes.py`（根据实跑反馈调优）
- Target Outputs: `best_us.txt` 或 `home_us_best_node.txt`

- [ ] **Step 1: 运行全量单元测试套件**
  运行所有 `tests/test_*.py`，确保各模块功能无回归。
- [ ] **Step 2: 在本地执行端到端真实测试**
  运行 `python scripts/filter_us_nodes.py`，观察日志中四个阶段的节点过滤数量、`cf-ray` 机房识别结果、真实微测速数据以及最终 Top 15 排名。
- [ ] **Step 3: 检查生成的输出文件**
  检查生成的文件命名、节点评分与格式，确保与 `WorkerVless2sub` 及 OpenClash 规范完全吻合。
- [ ] **Step 4: Commit & 整理交付**
  `git add -A; git commit -m "chore: 完成节点四阶漏斗严选引擎全量实现与端到端实测验证"`
