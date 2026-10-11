# 全链路真实代理沙箱节点优选引擎 (End-to-End Sandbox Pipeline) 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建基于 Mihomo Meta 直连隔离沙箱的端到端真实代理节点优选评估引擎，彻底跑通“本地直连 $\to$ CF Anycast 入口 $\to$ edgetunnel (Worker) $\to$ 出口 ProxyIP $\to$ Google / YouTube / ip.sb”完整通信链路，基于真实翻墙性能与风控状态打分评出 Top 15 黄金节点。

**Architecture:** 在 `filter_us_nodes.py` 中引入 `MihomoSandbox` 管理器，通过 `GID 65534 (nogroup)` 启动纯用户态无 TUN 的隔离内核；前置保留毫秒级 TCP/TLS 粗筛（入围 Top 25），后置挂载沙箱并发执行 Google 204 与 YouTube 204 端到端 RTT/抖动探针、`api.ip.sb/geoip` 出口落地美国风控质检及真实代理微吞吐测速；通过一票否决淘汰制与四维综合打分模型评选 Top 15。

**Tech Stack:** Python 3.9+, Mihomo Meta (`clash_meta` / RESTful External Controller API), SOCKS5 / HTTP Proxy Client, Linux nftables / GID isolation, pytest / unittest.

**Spec:** `docs/superpowers/specs/2026-10-10-e2e-sandbox-node-evaluation-pipeline-design.md`

## Global Constraints

- **沙箱隔离属性**：内核必须以 `GID 65534 (nogroup)` 启动，仅监听 `127.0.0.1:19098` (API) 与 `127.0.0.1:19998` (Proxy)，严禁开启 TUN/TProxy 或修改系统网络。
- **一票否决红线**：Google 或 YouTube 失败、多轮丢包率 $\ge 30\%$、`api.ip.sb` 出口非 `US`、或遇到 403/风控，一律直接淘汰。
- **协议兼容性**：生成的节点行格式保持 `104.16.x.x:8443#US-DaTree-8.8-01-家庭 1.80MB/s`，完全兼容 OpenClash 与现有 LuCI 控制台。
- **跨平台优雅降级**：在非 Linux/OpenWrt 宿主机环境（如 Windows 开发测试机无 `clash_meta`）时，自动降级为 Mock/仿真模式，确保单元测试全绿。

## Review Focus

1. **沙箱进程残留**：若脚本被 `Ctrl+C` 中断或异常崩溃，需在异常捕获与 atexit 中确保杀掉临时 `clash_meta` 进程并清理目录。
2. **多环境 VLESS 参数动态提取**：沙箱必须能从 OpenClash 当前配置中自动提取真实的 UUID、SNI 和 Path，若无法读取则使用默认兜底参数。
3. **Mihomo 启动与 API 就绪时序**：启动进程后需轮询等待 REST 端口就绪（最长 2.5s），避免过早发起 HTTP 请求导致 ECONNREFUSED。
4. **ProxyIP 偶发丢包与抖动处理**：多轮探针之间执行微小打散休眠（40ms~80ms），防止高频连续并发打爆 Worker 触发 429。
5. **代理下载超时死锁**：阶段 4 真实微吞吐测速必须设置严格超时（2.0s 截断），拉取不超过 1.0MB，避免测试挂起。

---

### Task 1: MihomoSandbox 沙箱生命周期与配置生成器

**Files:**
- Modify: `scripts/filter_us_nodes.py` (新增 `MihomoSandbox` 类及辅助函数)
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Produces:
  ```python
  class MihomoSandbox:
      def __init__(self, candidates, vless_template=None, work_dir="/tmp/ipselect_sandbox", api_port=19098, proxy_port=19998, core_path=None)
      def start(self) -> bool
      def stop(self)
      def is_ready(self) -> bool
  ```

- [ ] **Step 1: 编写沙箱配置生成与生命周期测试用例**

在 `tests/test_sandbox.py` 中编写对 YAML 生成逻辑、端口分配、跨平台降级机制和 `start/stop` 安全清理的测试用例。

- [ ] **Step 2: 运行测试验证失败**

运行: `python -m unittest tests/test_sandbox.py -v`
预期: 失败（`NameError: name 'MihomoSandbox' is not defined`）

- [ ] **Step 3: 实现 `MihomoSandbox` 类与内核生命周期控制**

在 `scripts/filter_us_nodes.py` 中实现：
1. `find_clash_core()`: 搜索 `/etc/openclash/core/clash_meta`, `/usr/bin/clash_meta`, `mihomo` 等，若未找到允许进入 Mock 模式；
2. `generate_sandbox_yaml()`: 根据传入的入围候选 IP 列表，注入 VLESS 节点配置，设置 `mixed-port: 19998`, `external-controller: 127.0.0.1:19098`, `mode: direct`, `tun: {enable: false}`;
3. `start()`: 创建工作目录，以 `GID 65534` 派生子进程，轮询 `http://127.0.0.1:19098/version` 直至就绪；
4. `stop()`: `terminate()` / `kill()` 进程，清空临时目录；注册 `atexit` 保证异常退出兜底清理。

- [ ] **Step 4: 运行测试验证通过**

运行: `python -m unittest tests/test_sandbox.py -v`
预期: 全部测试用例 PASS

- [ ] **Step 5: 提交任务代码**

```bash
git add scripts/filter_us_nodes.py tests/test_sandbox.py
git commit -m "feat(sandbox): implement MihomoSandbox lifecycle manager with GID 65534 bypass"
```

---

### Task 2: 端到端全链路探针引擎 (Google / YouTube / ip.sb)

**Files:**
- Modify: `scripts/filter_us_nodes.py` (实现 `probe_e2e_node`, `probe_ip_sb_exit`, `batch_probe_e2e`)
- Test: `tests/test_e2e_probe.py`

**Interfaces:**
- Consumes: `MihomoSandbox` from Task 1
- Produces:
  ```python
  def probe_e2e_delay(proxy_name: str, target_url: str, api_port: int = 19098, timeout_ms: int = 2500) -> Optional[int]
  def probe_ip_sb_exit(proxy_port: int = 19998, proxy_name: str = None, timeout: float = 3.5) -> Tuple[bool, str, str]
  def evaluate_e2e_candidate(proxy_name: str, api_port: int, proxy_port: int) -> Optional[Dict]
  ```

- [ ] **Step 1: 编写全链路探针与一票否决淘汰机制测试用例**

在 `tests/test_e2e_probe.py` 中模拟：
1. Google 204 多轮 RTT 统计（均值、极差抖动）；
2. YouTube 204 连通性校验；
3. `api.ip.sb/geoip` 出口国家校验（US 通过，非 US 或超时一票淘汰）；
4. 403/阻断一票否决。

- [ ] **Step 2: 运行测试验证失败**

运行: `python -m unittest tests/test_e2e_probe.py -v`
预期: 失败（探针函数尚未实现）

- [ ] **Step 3: 实现全链路探针逻辑**

在 `scripts/filter_us_nodes.py` 中实现：
1. `probe_e2e_delay`: HTTP GET 请求沙箱 API `/proxies/{name}/delay?url={url}&timeout={timeout}`；
2. 3 轮并发测试 Google 204 (`http://www.gstatic.com/generate_204`)，计算平均 RTT 与抖动；
3. 单轮快速测试 YouTube 204 (`https://www.youtube.com/generate_204`)；
4. 走本地代理端口请求 `https://api.ip.sb/geoip`，解析 JSON 获取 `country_code` 及出口 IP；
5. 综合判定：若不满足门槛返回 `None`，否则返回包含 `e2e_avg_rtt`, `e2e_jitter`, `exit_ip`, `exit_country` 的指标字典。

- [ ] **Step 4: 运行测试验证通过**

运行: `python -m unittest tests/test_e2e_probe.py -v`
预期: 全部测试用例 PASS

- [ ] **Step 5: 提交任务代码**

```bash
git add scripts/filter_us_nodes.py tests/test_e2e_probe.py
git commit -m "feat(probe): add end-to-end probing for Google, YouTube and ip.sb verification"
```

---

### Task 3: 真实微吞吐测速与四维综合打分模型

**Files:**
- Modify: `scripts/filter_us_nodes.py` (重构 `probe_real_proxy_speed` 与 `calculate_e2e_score`)
- Test: `tests/test_e2e_scoring.py`

**Interfaces:**
- Consumes: `evaluate_e2e_candidate` from Task 2
- Produces:
  ```python
  def probe_e2e_download_speed(proxy_port: int, max_bytes: int = 1048576, max_duration: float = 2.0) -> float
  def calculate_e2e_score(e2e_rtt: float, jitter: float, speed_mb_s: float, colo: str) -> float
  def format_node_line(ip: str, port: int, score: float, rank: int, tag: str, speed_mb_s: float, colo: str) -> str
  ```

- [ ] **Step 1: 编写全链路真实吞吐计算与综合打分测试用例**

在 `tests/test_e2e_scoring.py` 中测试不同延迟（160ms vs 300ms）、抖动（10ms vs 80ms）、速度（0.2MB/s vs 2.5MB/s）与 POP 机房（SJC/LAX）下的评分输出与节点行格式化。

- [ ] **Step 2: 运行测试验证失败**

运行: `python -m unittest tests/test_e2e_scoring.py -v`
预期: 失败（打分算法尚未升级）

- [ ] **Step 3: 实现真实微吞吐代理测速与打分算法**

在 `scripts/filter_us_nodes.py` 中实现：
1. `probe_e2e_download_speed`: 通过本地 SOCKS5 代理拉取 1.0MB 数据，截断计时并计算真实流速 MB/s；
2. `calculate_e2e_score`:
   $$S = 1.0 + 0.35 \times S_{\text{RTT}} + 0.25 \times S_{\text{Jitter}} + 0.30 \times S_{\text{Speed}} + 0.10 \times S_{\text{Colo}}$$
   严格映射至 1.0~10.0 分区间；
3. `format_node_line`: 构造标准格式 `<ip>:<port>#US-DaTree-<score>-<rank>-<tag> <speed>MB/s`。

- [ ] **Step 4: 运行测试验证通过**

运行: `python -m unittest tests/test_e2e_scoring.py -v`
预期: 全部测试用例 PASS

- [ ] **Step 5: 提交任务代码**

```bash
git add scripts/filter_us_nodes.py tests/test_e2e_scoring.py
git commit -m "feat(scoring): implement 4-dimension e2e scoring model and throughput meter"
```

---

### Task 4: 五阶主流程串联、回归测试与路由器真机验证

**Files:**
- Modify: `scripts/filter_us_nodes.py` (主函数与流水线重构)
- Test: `tests/test_full_pipeline_mock.py`
- Verify Live: 部署至路由器 `192.168.0.3` 运行实测

**Interfaces:**
- Integrates Tasks 1, 2, 3 into `main()`

- [ ] **Step 1: 编写全流程 Mock 回归测试用例**

在 `tests/test_full_pipeline_mock.py` 中串联模拟完整五阶流程（阶段 1 TCP $\to$ 阶段 2 TLS POP $\to$ 阶段 3 沙箱 E2E $\to$ 阶段 4 吞吐 $\to$ 阶段 5 Top 15 写入），验证命令行输出与生成文件的合法性。

- [ ] **Step 2: 重构主流水线逻辑**

在 `scripts/filter_us_nodes.py` 的 `main()` 中：
1. 阶段 1 & 2 快速初筛选出 Top 25 节点；
2. 启动 `MihomoSandbox`（若支持），并发执行阶段 3 全链路探测（Google + YouTube + ip.sb）；
3. 对通过节点执行阶段 4 代理吞吐测速；
4. 综合打分排序，截取 Top 15，写入 `--output` 文件；
5. 在 `finally` 块中优雅关闭沙箱。

- [ ] **Step 3: 运行本地全流程测试验证**

运行: `python -m unittest discover tests -v`
预期: 全部测试用例 PASS

- [ ] **Step 4: 部署到路由器并执行全链路真机实测**

使用 MCP SSH 工具将更新后的 `filter_us_nodes.py` 上传至路由器 `/root/ipselect/scripts/filter_us_nodes.py`，执行真机实测命令：
`python3 scripts/filter_us_nodes.py --tag 家庭 --output home_us_best_node.txt --interface br-lan`
检查输出日志，确认：
1. GID 65534 规则生效，OpenClash 未受干扰；
2. 沙箱成功挂载节点并测出真实的 Google 204 和 YouTube 204 延迟；
3. 真实落地 `ip.sb` 出口为 US；
4. 输出 Top 15 节点并顺利写入 `home_us_best_node.txt`。

- [ ] **Step 5: 提交任务代码并推送到 GitHub**

```bash
git add scripts/filter_us_nodes.py tests/
git commit -m "feat(pipeline): complete end-to-end sandbox evaluation pipeline and router verification"
git push origin main
```
