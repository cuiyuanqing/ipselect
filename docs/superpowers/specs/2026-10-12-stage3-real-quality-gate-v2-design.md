# 阶段 3 真实质量门 v2 — 设计文档

- 日期：2026-10-12
- 作者：与 Jeff 协作设计
- 状态：待 Jeff 审阅
- 关联：`docs/lessons_learned_speedtest_v5.md`、`docs/superpowers/specs/2026-10-10-e2e-sandbox-node-evaluation-pipeline-design.md`

## 1. 背景与问题

### 1.1 用户反馈

> "脚本验证过了能访问 youtube google。但是实测中有的节点还是不能打开 google。节点质量有问题。"
> "用最真实的访问去做，而且要有一个连续的区间。"
> "前 1 分钟能打开 google youtube 下一秒就打不开了。"

### 1.2 现状

`scripts/filter_us_nodes.py` 的 `evaluate_e2e_candidate()`（L479-538）当前判定逻辑：

| 探测项 | 实现 | 问题 |
|---|---|---|
| Google | `https://www.google.com/generate_204` ×3 次，≥2 次成功 | 204 探测 ≠ 真实页面，HTML/JS/CSS/字体等子资源全没测 |
| YouTube | `https://www.youtube.com/generate_204` ×1 次，成功 | 同上 |
| ip.sb | 出口国家 = target_country | 合理 |

### 1.3 根因分析

| # | 根因 | 影响 | 严重度 |
|---|---|---|---|
| 1 | 204 探测太轻量：Google 官方 204 探针只发 GET 立即 204 无 body，不触发任何子资源 | 脚本判"通过"但浏览器实际打不开（子资源被 GFW 干扰 / CF WAF 弹 challenge） | **致命** |
| 2 | 单次快照：1 个时间点测一次就锁死，节点质量时间漂移完全捕捉不到 | Jeff 报告"下一秒打不开"是这种抖动 | **致命** |
| 3 | 204 探测路径与真实页面路径在 CF 边缘机房可能不同 | 节点路径差异未被探测 | 中 |
| 4 | TLS 指纹差异：沙箱 Python urllib vs 真实浏览器 Chrome | 某些 CF 边缘机房对指纹判断不同，结果不一致 | 中 |
| 5 | IPv4 vs IPv6：客户端可能只走 v4，但沙箱双栈优先 v6 | 沙箱通过 ≠ 客户端能用 | 中 |

## 2. 目标

让 `evaluate_e2e_candidate` 升级为**真实质量门 v2**，确保输出到 `best_us.txt` 的 15 个节点在客户端浏览器真实场景下能用 Google + YouTube，且**抗秒级抖动**。

## 3. 设计

### 3.1 架构总览

```
阶段 2 (TLS 握手 + cf-ray 质检)
    ↓ 1240 → 美西 1108
阶段 3 v2 (沙箱全链路真实质量门)
    ├── 3a. 真实页面快速探针 (3s × 1 次 / 节点)
    │       淘汰明显死节点（HTML 拿不到 / <title> 不匹配）
    ├── 3b. 真实页面 60s 连续区间密集采样 (5s × 12 次 / 节点)
    │       通过率 < 11/12 → 淘汰
    └── 3c. ip.sb 出口国家 (沿用)
    ↓ ~12-15 节点
阶段 4 (5s×2 轮吞吐测速)
    ↓ 15 节点
Top 15
```

### 3.2 关键设计点

#### 3.2.1 真实页面判定

**新增** `probe_real_google_page()` 函数：

- URL：`https://www.google.com/`
- 拉取首 256KB HTML
- 校验：HTML 包含 `<title>Google</title>`（或当前 Google 主页实际 title 字符串）
- 超时：5s/次
- 失败重试：1 次（解决瞬时抖动）
- v4 优先：明确 `socket.AF_INET`，避免 v6 路径干扰 v4 客户端
- TLS 指纹：保留 Python urllib（不引入 curl_cffi，避免新依赖）

**为什么 256KB 而非 1MB**：
- Google 主页 HTML 主体 ~50-100KB
- 256KB 足以覆盖 <title>、<head>、<body> 起始部分、所有 <link>/<script> 资源声明
- 1MB 会把 fonts.googleapis.com、www.gstatic.com 等子资源全拉下来，但子资源加载是浏览器的事，沙箱内测会大幅增加耗时且不能复现客户端体验
- **测的是"HTML 主体可达"**，子资源由浏览器处理，沙箱不替代浏览器

**为什么只测 Google 不测 YouTube**：
- Jeff 核心诉求是"打不开 Google"
- 双 URL 探测耗时 ×2，但收益边际递减（YouTube 实际更宽松）
- 阶段 3b 通过率 ≥ 11/12 已经覆盖"绝大多数时间能用"，单点失效由浏览器容错

#### 3.2.2 60s 连续区间密集采样

```python
PROBE_INTERVAL_SEC = 5       # 采样间隔
PROBE_TOTAL_ROUNDS = 12      # 60s / 5s
PROBE_PASS_THRESHOLD = 11    # 12 次中至少 11 次通过

def probe_60s_stability(proxy_name, ip, port, api_port, proxy_port):
    """60s 连续区间真实页面稳定性测试"""
    passed = 0
    failed_rounds = []
    for round_idx in range(PROBE_TOTAL_ROUNDS):
        if round_idx > 0:
            time.sleep(PROBE_INTERVAL_SEC)
        result = probe_real_google_page(proxy_name, ip, port, api_port, timeout_ms=5000)
        if result.ok:
            passed += 1
        else:
            failed_rounds.append((round_idx, result.error))
    return {
        'passed': passed,
        'total': PROBE_TOTAL_ROUNDS,
        'pass_rate': passed / PROBE_TOTAL_ROUNDS,
        'failed_rounds': failed_rounds,
        'is_stable': passed >= PROBE_PASS_THRESHOLD
    }
```

**为什么 5s 间隔**：
- 1s 太密，沙箱 + Mihomo 切换开销会污染样本
- 5s 平衡：每次请求冷启动足够，60s 内能采 12 个样本
- 5s 内连续 2 次失败 = 10s 不可用，对用户感知 = "打不开"

**为什么 11/12 通过阈值**：
- 12 次中允许 1 次失败 = 容许一次瞬时抖动
- 2 次失败 = 10s 不可用，剔除
- 阈值与"客户端真实使用体验"对齐：用户能容忍偶发卡顿，无法容忍持续 10s+ 失效

#### 3.2.3 并发模型

```python
with ThreadPoolExecutor(max_workers=10) as executor:
    futures = {executor.submit(probe_node_60s, item): item for item in topN_candidates}
    for future in as_completed(futures):
        res = future.result()
        if res and res['is_stable']:
            e2e_results.append(res)
```

- 10 并发保持与现有架构一致
- 18 节点 / 10 并发 = 2 批 × 60s = ~2-3 分钟（含 3a 快速探针 ~10s）
- 总耗时预算：阶段 3 整体 ~3-5 分钟

#### 3.2.4 IPv4 强制

```python
import socket
# 强制 IPv4，避免客户端 v4 only 时沙箱走 v6 通过但客户端失败
orig_family = socket.getaddrinfo
def v4_only_getaddrinfo(host, port, *args, **kwargs):
    return [ai for ai in orig_family(host, port, *args, **kwargs) if ai[0] == socket.AF_INET]
socket.getaddrinfo = v4_only_getaddrinfo
```

在 `evaluate_e2e_candidate` 入口处 monkey-patch，**仅影响阶段 3 探测**，不影响阶段 4 测速（阶段 4 测速源本身需要 v4/v6 自适应）。

#### 3.2.5 失败重试

```python
def probe_real_google_page(proxy_name, ip, port, api_port, timeout_ms=5000):
    """真实页面探测，单次失败重试 1 次"""
    for attempt in (0, 1):
        try:
            html = fetch_google_home_via_sandbox(proxy_name, api_port, max_bytes=262144, timeout=timeout_ms/1000)
            if html and b'<title>Google</title>' in html:
                return ProbeResult(ok=True, html_len=len(html), attempt=attempt)
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(1)  # 重试前等 1s
                continue
    return ProbeResult(ok=False, error=str(last_err))
```

- 单次失败重试 1 次：解决瞬时抖动，不污染 12 次采样
- 60s 区间内**不进行 60s 内的失败重试**（避免把 12 次降级为 6 次）—— 每次采样独立，1 次失败记 1 次失败

### 3.3 降级策略

```
60s 区间跑完 → 通过节点数:
├── ≥ 15 → 取前 15 进入阶段 4
├── 12-14 → 全部进入阶段 4（接受略少于 15）
├── 8-11 → 触发降级 1：30s 区间 / 3s 间隔 / 10 探重测
│   ├── 重测后 ≥ 12 → 取满进入阶段 4
│   └── 重测后仍 < 12 → 走 2 次取并集
├── 4-7 → 触发降级 2：降级回原 204 探测快速筛
│   ├── 204 探针通过 ≥ 15 → 用 204 结果（标注 quality_downgraded=1）
│   └── 204 通过 < 15 → 报错退出
└── < 4 → 触发降级 3：报错"上游严重劣化",不输出 best_us.txt,推送 STATUS=no_quality
```

降级 1 设计目的：避免"18 节点过 60s 探测剩 10 个"的常见情况被一刀切。
降级 2 设计目的：避免节点池暂时枯竭导致脚本整个失败，至少还能输出 204 通过的兜底。
降级 3 设计目的：避免上游整体失效时仍输出垃圾数据污染订阅。

### 3.4 与现有架构的集成

#### 3.4.1 沿用部分
- Mihomo 沙箱（端口 19098/19998）— 不变
- VLESS 模板探测 — 不变
- ip.sb 出口国家判定 — 不变
- 阶段 4 真实吞吐测速 — 不变（已经是 5s×2 轮，足够稳定）

#### 3.4.2 改造部分
- `evaluate_e2e_candidate` 拆为子函数：
  - `probe_real_google_page()`（新增）
  - `probe_60s_stability()`（新增）
  - `probe_ip_sb_exit()`（沿用）
- 整体逻辑编排移到新的 `evaluate_node_quality()`（重命名）
- `e2e_results` dict 增加字段：`pass_rate`、`failed_rounds`、`quality_gate_version=2`

#### 3.4.3 不动的部分
- `probe_stability()`（直连回退模式）— 不变
- 阶段 1/2/4/5 — 不变
- `best_us.txt` 节点行格式 — 不变（仍保留 `IP:PORT#US-公司-RANK-SPEEDMB/s`）

### 3.5 输出与可观测性

**每节点质量门 v2 日志**（写入 stage 3 完成时）：

```
[阶段 3 v2] node_05 (104.17.147.240:443) 真实质量门:
  ├─ 3a 真实页面快速探针: OK (HTML=87KB, <title>=Google ✓)
  ├─ 3b 60s 区间密集采样: 12/12 通过 (pass_rate=1.00)
  └─ 3c ip.sb 出口: US (104.28.19.1) ✓
```

**best_us_detail.json 字段扩展**：

```json
{
  "ip": "104.17.147.240",
  "port": 443,
  "colo": "SJC",
  "e2e_avg_rtt": 698.0,
  "e2e_jitter": 29.0,
  "youtube_rtt": 205.0,
  "exit_country": "US",
  "exit_ip": "104.28.19.1",
  "quality_gate_version": 2,
  "real_page_pass_rate": 1.0,
  "real_page_failed_rounds": [],
  "stability_window_sec": 60
}
```

### 3.6 错误处理

| 错误 | 处理 |
|---|---|
| 沙箱启动失败 | 沿用现状：降级到直连稳定性模式 |
| Mihomo 切换失败 | 计入失败轮次，连续 3 次切换失败 → 节点淘汰 |
| 单次 HTTP 拉取超时（5s） | 失败重试 1 次，仍失败记 1 次失败轮次 |
| HTML 拿到但 <title> 不匹配 | 失败轮次（如 Google 返回搜索结果页而非首页） |
| 12 次全失败 | 节点淘汰（极少见，IP 大概率被墙或临时下线） |
| 沙箱进程崩溃 | 沿用现状：阶段 3 整体回退到直连模式 |
| Python urllib 抛 ConnectionError | 失败轮次 |

### 3.7 性能预算

| 阶段 | 现状耗时 | v2 耗时 | 增量 |
|---|---|---|---|
| 阶段 3 整体 | ~30-60s | ~150-300s (3-5 min) | +120-240s |
| 阶段 4 吞吐测速 | ~150-300s | ~150-300s | 0 |
| 阶段 1/2/5 | ~30s | ~30s | 0 |
| **总耗时** | **~5-8 min** | **~7-12 min** | **+2-4 min** |

预算可接受（cron 凌晨 04:30 跑，有充足时间）。

### 3.8 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Google 改变 <title> 字符串（如 A/B 测试） | 误判全部节点失败 | 接受多种 title 模式（Google/Google Search/搜索等） |
| Google 主页对中国 IP 弹验证码 | 60s 内多次触发验证码 | 探测时携带真实 UA + 接受语言，避免触发 |
| 沙箱代理池耗尽（Mihomo 切换延迟） | 12 次采样间隔被破坏 | 切换失败时计入失败轮次 |
| 60s × 18 节点 = 2 批 × 60s 慢于预期 | 整体脚本超时 | 监控总耗时，超过 10 分钟触发降级 1（30s 区间） |
| 真实页面被 GFW 间歇性 RST | 误判节点不稳 | 重试 1 次 + 11/12 阈值已容错 |

## 4. 不在范围内

- 任务 1（增加测速源）— Jeff 明确说"测速源优先级低"，本设计不涉及
- 阶段 4 真实吞吐测速改造 — 现状已足够
- `home_us_best_node.txt` 家庭路由器 — 沿用现有逻辑（不同物理出口）
- OpenClash 订阅生成 — 不变
- 推送 / runner 强化 — 已 commit f22ce74/d0a89a7

## 5. 实施步骤（高层）

1. 新增 `probe_real_google_page()` + `probe_60s_stability()` 子函数
2. 重构 `evaluate_e2e_candidate` → `evaluate_node_quality_v2`
3. 实现降级 1/2/3 逻辑
4. 扩展 `best_us_detail.json` 字段
5. 阶段 3 完成日志输出增加质量门明细
6. 部署到软路由（commit + scp `/usr/bin/ipselect-runner`）
7. 触发一次完整跑，验证：
   - 节点行仍带真实速度
   - 12/12 通过率节点 → 输出 top 15
   - 推送仍成功

## 6. 验证标准

| 验收项 | 标准 |
|---|---|
| 真实页面探测 | `best_us.txt` 节点 100% 在 client 端可打开 google.com 主页 |
| 60s 稳定性 | 12 次连续探测中 11/12 通过，无连续 2 次失败 |
| 降级生效 | 节点池枯竭时输出 STATUS=no_quality，不输出垃圾 best_us.txt |
| 性能 | 阶段 3 整体 ≤ 5 分钟 |
| 不破坏现有 | 阶段 1/2/4/5 行为不变，best_us.txt 节点行格式不变 |
| 推送 | 强化 runner (f22ce74/d0a89a7) 继续生效，git push 必成功 |

## 7. 待确认

- [ ] Google `<title>` 字符串当前是 "Google" 还是要兼容其他变体（"Google Search" 等）？
- [ ] 真实页面探测是否要测 YouTube（增加耗时）？
- [ ] 降级 1 触发条件用 8-11 还是 6-11？
- [ ] 阶段 3 整体超过 10 分钟是否要主动中断？

## 8. 关联

- `docs/lessons_learned_speedtest_v5.md` §三 — 测速源白名单
- `docs/superpowers/specs/2026-10-10-e2e-sandbox-node-evaluation-pipeline-design.md` — 现有阶段 3 架构
- `scripts/filter_us_nodes.py` L479-538 — 现有 `evaluate_e2e_candidate`
- 软路由已部署强化版 runner commit d0a89a7（推送必成功 + 激进同步）
