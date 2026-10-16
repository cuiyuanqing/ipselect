# 节点优选抗 Cloudflare 冲击方案

> **文档目的**：解决 ipselect 跑批触发 Cloudflare Workers CPU 10ms 限制、CF WAF 判定"网络滥用"、以及 LuCI 开关失效的问题。通过礼貌限速（politeness rate limiting）、探针路径替换、并发参数化、候选分片轮转、三档 preset 重构，把"短时间高并发冲击"改成"长时间低并发慢节奏探测"。
>
> **涉及项目**：
> - `scripts/filter_us_nodes.py`（核心优选流水线）
> - `scripts/stage3_quality_gate.py`（60s/30s 真实页面采样）
> - `luci-app-ipselect/root/usr/bin/ipselect-runner`（iStoreOS 执行引擎）
> - `luci-app-ipselect/root/etc/init.d/ipselect`（服务管理与 cron 同步）
> - `D:\nice_wall\edgetunnel\_worker.js`（VLESS 落地 Worker，33,144 行）
>
> **版本**：v2.1（第二轮三方评审后代码落地版）
> **状态**：核心代码已落地，待域名更新后执行完整测试

---

## 版本变更记录

| 版本 | 日期 | 变更摘要 |
|---|---|---|
| v1.0 | 2026-10-15 | 初版方案：并发 25→3、换探针到 cloudflare.com、测速 80MB→5MB、三档 cron + 全局随机抖动 0~10 分钟、LuCI bug 修复 |
| v2.0 | 2026-10-16 | **三方评审后全面重写**：<br>1. 纠正所有与代码事实不符的量化数字（THREAD_WORKERS 死常量、阶段2 探测量 1600~2200 非 75、阶段4 非 60s×12、活跃测速函数名错误、max_bytes 量纲错误等）<br>2. 探针改造改为**保留 SNI=自有域 + 路径换 /cdn-cgi/trace**（不换 cloudflare.com，避免反效果）<br>3. 新增 P0 级安全修复：VLESS UUID 泄露治理、fail-open 判定修复、封禁熔断机制<br>4. 并发参数化改为替换 4 处硬编码 + 候选分片轮转（每日只扫 1/3 撒点）<br>5. 测速降级改为 15-20MB 稳态口径 + 重标定阈值，而非 5MB<br>6. 三档模式改为 preset 化配置表 + 单编排入口串行，消除 PID 锁撞车<br>7. 全局随机抖动降级为"无害但不计入防护预算"，挪到编排层<br>8. 新增阶段4 分层并行（Top8 真 60s + 其余 30s）、阶段级埋点、失败告警 |
| v2.1 | 2026-10-16 | **第二轮三方评审（R2）后代码落地**：<br>1. **P0 安全项已落地**：<br>   - UUID 治理：`DEFAULT_VLESS_UUID` 去硬编码默认值、详情 JSON 改 `uuid_fingerprint`（SHA256 前 12 位）<br>   - fail-closed 改造：`probe_tls_colo` 4 元组返回、补全 BLOCK_MARKERS（含 "Just a moment..."、"error code: 10" 等）、同步改 :1643/:2471 两处调用点<br>   - 熔断机制：`record_cf_block()` 全局计数，累计 5 次拦截触发熔断<br>2. **LuCI bug 已修复**：`run_task()` 开头加 `enabled`/`cron_enabled` 双开关校验，关闭后跳过执行<br>3. **探针改造已落地**：`HEAD /` → `GET /cdn-cgi/trace`、单次 recv → 64KB 读循环、body `colo=` 解析优先 + cf-ray 回退、SNI 保持自有域<br>4. **并发参数化已落地**：4 处硬编码 `max_workers`（30/25/15/10）替换为配置常量（STAGE1_TCP_WORKERS=10、STAGE2_COLO_WORKERS=3、STAGE3_TIER1_WORKERS=5、SANDBOX_FAILBACK_WORKERS=2），删死参数 THREAD_WORKERS<br>5. **候选分片轮转已落地**：每日只扫 1/3 撒点（500 IP×2=1000 任务，非原方案高估的 2000），3 天一轮，扇出量降 2/3<br>6. **测速口径修正已落地**：轮次 2→3（n=3 取中位）、duration 5→6s、parallel 3→1 流、字节上限 80→100MB（按最高速×6s 估算）<br>7. **R2 修补要点**：<br>   - 回滚方案删除"改回旧 UUID"建议（R2-N1），只允许生成新随机值<br>   - 阶段2 早停配额应为导出配额 2~3 倍 + 补 KR 档（R2-N2，待实现）<br>   - 字节上限按最高速×6s 设（60-120MB），不是 15-20MB（R2-N3，已调整为 100MB）<br>   - 熔断器改持久化冷却文件（R2-N5，当前为内存计数，待升级）<br>   - 灰度门禁 = CF Dashboard Workers Metrics 归零（R2-N6）<br>   - 分片数学修正为 500 IP×2=1000 任务（R2-N7，已修正）<br>8. **待办**：等待用户提供新 vless 域名 → 执行完整流程测试 → 验证节点可用性 |

---

## 目录
1. [问题根因分析（修正版）](#一问题根因分析修正版)
2. [前后对比评估（修正版）](#二前后对比评估修正版)
3. [实现策略](#三实现策略)
4. [实施计划](#四实施计划)
5. [风险与回滚](#五风险与回滚)
6. [附录：关键参数速查表](#附录关键参数速查表)

---

## 一、问题根因分析（修正版）

### 1.1 CPU 10ms 限制触发原因（三本账）

原方案估算"525 次/天 Worker 调用"严重失真。按代码实际行为，分三本账：

| 账 | 来源 | 每日请求量 | 是否触发用户 Worker |
|---|---|---|---|
| **直连探针账** | 阶段2 对全部 TCP 存活 IP 做 TLS+HEAD 探测 | **1600~2200 次** | ✅ 每次都命中 Worker（HEAD / 进入路由） |
| **隧道流量账** | 阶段3 Mihomo 沙箱 + 阶段4 测速，走 VLESS 代理出去 | 数百次连接 | ✅ 每连接都是 Worker 调用（VLESS 落地必经） |
| **字节流量账** | 阶段4 下载测速，实测 2.04~5.67 MB/s | 0.4~2 GB/次（非原方案的 3.6GB） | 走隧道，经 Worker 转发 |

**核心结论**：
- 直连探针冲击主峰在阶段2，**1600~2200 次/天**，原方案只算了 75 次，低估 20~30 倍
- 阶段3/4 走 VLESS 隧道，每连接都经过 Worker，"换探针后 Worker 调用归零"是错误的——直连探针归零，但隧道流量仍在
- CPU 10ms 告警的主因是阶段2 的 1600~2200 次直连 Worker 调用

### 1.2 网络滥用判定触发原因

CF 边缘 WAF 从以下特征识别为扫描行为：

| 信号维度 | CF 能看到什么 | 现状指纹 | 原方案覆盖？ |
|---|---|---|---|
| 请求速率/突发 | 每秒连接数 | 30 并发 × 3000 候选任务（1500 IP × 2 端口） | ⚠️ 方向对，但改错了常量 |
| TLS 指纹 JA3/JA4 | ClientHello 顺序 | Python OpenSSL 指纹，全网同款 | ❌ 未动 |
| HTTP 指纹（ALPN/H2） | ALPN、SETTINGS 帧序 | 无 ALPN → 强制 HTTP/1.1；真浏览器几乎全是 h2 | ❌ 最强 bot 信号之一 |
| 跨 IP 聚合 | 一个源 IP 扫 1500 个 Anycast | 固定家庭/公司宽带出口扇出 | ❌ 核心问题 |
| 流量体量 | 字节量、持续时长 | 0.4~2 GB/次 | ✅ 测速降载有效 |

**关键认知**：降并发改变的是"像不像 DDoS"，TLS/HTTP 指纹决定的是"是不是人"。但本方案聚焦于**礼貌限速**（降低扇出量和速率），不做指纹伪装（那是另一个大工程）。

### 1.3 LuCI 配置不生效 Bug（已确认）

`ipselect-runner` 的 `run_task()` 读取了 20+ 个 UCI 配置项，但**唯独没有读取 `enabled` 和 `cron_enabled`**。

**后果**：用户在 LuCI 关掉"启动服务"开关、关掉"定时优选任务"开关，只要 runner 被任何方式触发（crontab 残留、手动按钮、其他脚本调用），就会直接跑批，开关形同虚设。

### 1.4 原方案漏掉的 P0 级风险

| 风险 | 严重性 | 说明 |
|---|---|---|
| 🔴 VLESS UUID 明文泄露到公开 GitHub 仓库 | Critical | `best_us_detail.json` 公开可拉，含完整 UUID → Worker 沦为免费开放代理，爆炸半径是整个 CF 账号 |
| 🔴 `probe_tls_colo` fail-open | High | 判定逻辑 `is_valid = bool(colo) or "HTTP/1." in res or "cloudflare" in res`，CF 挑战页/封禁页全部含 "cloudflare" 字样 → 假活节点污染候选池 |
| 🔴 沙箱失败降级反向轰击 | High | Mihomo 沙箱起不来 → `max_workers=10` 批量 `probe_stability` 直连打 Worker → 越异常越轰击，与目标反向 |

---

## 二、前后对比评估（修正版）

### 2.1 整体冲击指标对比

| 指标 | 优化前（实际） | 优化后（目标） | 降幅 |
|---|---|---|---|
| 阶段1 并发 | 30 硬编码（THREAD_WORKERS=25 是死常量） | **8~12**（纯 TCP 不进 Worker，保并发） | -60% |
| 阶段2 并发 | 25 硬编码 | **3**（真正要降的只有直连 Worker 的阶段2） | -88% |
| 阶段2 候选量 | 全部 TCP 存活者（1600~2200） | **候选分片轮转：每日只扫 1/3 撒点**（~1000 IP × 2 端口） | -67% |
| 阶段2 探针目标 | `HEAD /`（SNI=自有域，命中 Worker） | `GET /cdn-cgi/trace`（SNI=自有域，边缘直接应答不进 Worker） | **Worker 调用归零** |
| 直连 Worker 调用（阶段2） | 1600~2200 次/天 | **0 次/天**（trace 由边缘直接应答） | -100% |
| 单流下载量上限 | UCI=80MB（实际 5s 墙钟截断，每流仅 3~9.5MB） | **15~20MB + 6s 稳态口径（弃前 2s 慢启动）** | 字节量降 50~60%，测速精度提升 |
| 测速节点数 | 45 | 15 + 边界复测层 | -67% |
| 节点间并发 | 代码早已串行（SPEED_TEST_NODE_CONCURRENCY 是死配置） | 保持串行（本来就是正确性要求） | 无变化 |
| 节点内并发（流数） | 3 | 1 | -67%，稳定性提升 |
| 端到端总时长 | ~25~45 分钟（密集冲击） | **单次全量 70~120 分钟**（分散慢节奏） | 拉长 2~3 倍，冲击更分散 |
| 全局随机抖动 | 无 | 0~10 分钟（**无害但不计入防护预算**，挪到编排层） | 心理安慰 |

### 2.2 分阶段详细对比（修正版）

#### 阶段 1：TCP 快速存活扫描

| 项目 | 优化前（实际） | 优化后（目标） | 变化 |
|---|---|---|---|
| 并发 | 30 硬编码（filter_us_nodes.py:2435） | **8~12** | 降 60%，但仍是所有阶段中最高（纯 TCP 不进 Worker） |
| 候选量 | 1500 随机 IP × 2 端口 ≈ 3000 任务 | **候选分片轮转：每日只扫 1/3 撒点**（~1000 IP × 2 端口） | 3 天一轮扇出，WAF 看到的扇出量降 2/3 |
| IP 间隔 | 连续无间隔 | 每个 IP 之间随机 sleep 0.2~0.8 秒 | 从突发变缓慢 |
| 预计耗时 | ~1.5 分钟 | ~20~30 分钟 | 拉长 15 倍 |

**为什么阶段1 不降那么多**：纯 TCP 握手不进 Worker，不消耗 CPU，CF 边缘看到的是 SYN 包但没有 HTTP 层特征。保 8~12 并发可以保证扫描效率。

#### 阶段 2：TLS 探针 + 机房识别（直连 Worker 的冲击主峰）

| 项目 | 优化前（实际） | 优化后（目标） | 变化 |
|---|---|---|---|
| 探针目标 | `HEAD /`（:1597），SNI=proxy.19940407.xyz | `GET /cdn-cgi/trace`，SNI=**保持** proxy.19940407.xyz | 路径换，SNI 不换 |
| 为什么不换 SNI=cloudflare.com | — | 本项目已有反向实证：CF 自家域对非浏览器 TLS 直接握手拒绝（lessons_learned_speedtest_v5.md Layer 2）；cloudflare.com bot 管理最严，触发挑战概率高 | 避免反效果 |
| 为什么换路径不换 SNI 就有效 | — | `/cdn-cgi/*` 是 CF 边缘保留路径，由边缘直接应答、**不进 Worker 路由** → Worker CPU 调用照样归零 | 核心收益点 |
| 并发 | 25 硬编码（:2467） | **3** | 降 88% |
| 候选量 | 全部 TCP 存活者（1600~2200） | **分片轮转 + 早停：区域配额已满即截断** | 冲击降 67% + 早停优化 |
| 节点间隔 | 连续 | 每个 IP 之间随机 sleep 1~3 秒 | 分散 |
| 预计耗时 | ~2~2.5 分钟 | ~28~39 分钟 | 拉长 15 倍 |
| Worker CPU 调用 | 1600~2200 次/天 | **0 次/天** | -100% |

**实现要点**（架构师评审）：
- trace 返回 `key=value` 文本（非 JSON），body 有 `colo=XXX` 字段
- 单次 `recv(2048)` 必须改成**读循环**（参照 edgetunnel `_worker.js:165` 的 64KB 上限实现）
- body `colo=` 解析优先，cf-ray 头解析保留作回退
- 先 5 IP 小样本 A/B 对比拦截率，再全量
- 禁忌边界：v5 文档把 `cloudflare.com/cdn-cgi/trace` 列入测速源绝对黑名单，本方案显式声明"**仅限直连 colo 探测、不经隧道、不作测速源**"

#### 阶段 3：稳定性测试 / 沙箱质量门

| 项目 | 优化前（实际） | 优化后（目标） | 变化 |
|---|---|---|---|
| 主管线实际行为 | Tier1: 15 线程 Google/YouTube 204（走隧道）+ Tier2: 顺序 ip.sb 落地校验 + Google 首页 `<title>`（走隧道） | 保持分层，**Tier1 并发降到 4 路独立端口并行** | 速率友好 |
| 假元数据修复 | `'stability_window_sec': 60` 是硬编码假元数据（:867） | 真实测量时长写回，不再造假 | 数据真实性 |
| 沙箱失败降级 | 沙箱起不来 → `max_workers=10` 批量 `probe_stability` 直连打 Worker | **降级路径同用 trace 探针 + 间隔参数**；沙箱启动失败视为告警事件，通知推送 | 消除反向轰击 |
| 预计耗时 | ~5~6 分钟 | ~8~12 分钟（分层并行后） | 拉长但更可靠 |

#### 阶段 4：真实吞吐测速

| 项目 | 优化前（实际） | 优化后（目标） | 变化 |
|---|---|---|---|
| 活跃函数 | `probe_throughput_speed`（:1747），非原方案说的 `probe_real_download_speed`（死路径） | 改活跃函数 | 落点修正 |
| 单流字节上限 | UCI=80MB，但实际 5s 墙钟截断 → 每流仅 3~9.5MB | **15~20MB + 6s 稳态口径（弃前 2s 慢启动段）** | 测速精度提升，避免快节点被压低 |
| 测速节点数 | 45 | 15 + **边界复测层（L4'）**：阈值/配额 ±15% 带内重测 2 次 | 边界误判率从 15~25% 压到 <5% |
| 节点间并发 | 代码早已串行（死配置 SPEED_TEST_NODE_CONCURRENCY） | 保持串行（正确性要求） | 无变化 |
| 节点内并发 | 3 流 | 1 流 | 稳定性提升，绝对值下移须重标定 |
| 阈值重标定 | min_speed=3.0 / vip_speed=8.0（基于 3 流口径） | **重标定为 1 流口径：min_speed=2.0 / vip_speed=5.0**（估算值，待一致性实验验证） | 适配新口径 |
| 预计耗时 | ~9~10 分钟（45 节点串行） | ~5~7 分钟（15 节点串行 + 边界复测） | 净缩短 |
| 总下载流量 | ~0.4~2 GB | ~150~250 MB | -85% |

**为什么不是 5MB**：性能基准师评审指出，5MB 在 10MB/s 节点上纯传输仅 0.5s，握手时间计入分母 → 系统性低估 2~4 倍，vip_speed=8.0 对快节点 100% 失效。15~20MB 或 6s 稳态口径才能保证排序精度。

### 2.3 三档 preset 节奏对比

| 档位 | 时间 | 内容 | 产物契约 | 是否 push |
|---|---|---|---|---|
| daily-light | 01:00 | 阶段1 TCP 扫描 + 阶段2 trace 探针机房识别 | 仅写 `/tmp` 缓存，不产出正式文件 | ❌ |
| full-pipeline | 02:00 | 阶段3 沙箱质量门 + 阶段4 测速 + 阶段5 评分导出 | `best_us.txt` + `_detail.json` + `vip_nodes.json` | ✅ |
| vip-inspect | 03:00 | 只对 vip_nodes.json 里的历史好节点做阶段3+4 验证 | 更新 `vip_nodes.json`（不覆盖 best_us.txt） | ✅ |

**关键改进**：
- **单编排入口串行**：一个 cron 触发 runner，runner 内部按序跑三档，彻底消除 PID 锁撞车
- **产物契约明确**：每档写什么文件、是否 push、是否触发 OpenClash，全部定义清楚
- **跨档交接**：daily-light 的缓存持久化到 `/etc/ipselect/stage2_cache.json`，full-pipeline 读取复用，不重跑阶段1+2

### 2.4 LuCI Bug 修复对比

| 场景 | 修复前 | 修复后 |
|---|---|---|
| LuCI 关掉 enabled=0 | crontab 清理了就不跑，没清理就还在跑（不设防） | runner 开头检查 enabled=0，直接退出，拒绝执行 |
| LuCI 关掉 cron_enabled=0 | crontab 清理了就不跑，没清理就还在跑 | runner 检测到 TRIGGER=cron 且 cron_enabled=0，直接退出 |
| 手动点"启动"按钮（TRIGGER=manual） | 不检查任何开关，直接跑 | 只检查 enabled，不检查 cron_enabled（符合预期） |

---

## 三、实现策略

### 3.1 P0 安全修复（立即执行）

#### 3.1.1 VLESS UUID 泄露治理
- **轮换 UUID**：edgetunnel 后台 `env.UUID` 立即换新值
- **详情 JSON 只留指纹**：`filter_us_nodes.py:2716` 把完整 UUID 改成 `uuid_fingerprint = sha256(uuid)[:12]`，不落明文
- **清洗仓库历史**：`git filter-repo` 清除历史 commit 中的 UUID
- **去默认值**：`DEFAULT_VLESS_UUID` 改为无默认值，缺省即报错

#### 3.1.2 探针 fail-open 修复
```python
# filter_us_nodes.py:1603 附近
BLOCK_MARKERS = ("cf-chl-", "challenge-platform", "Attention Required", "Sorry, you have been blocked")
status_match = re.match(r"HTTP/1\.\d (\d{3})", res_text)
status = int(status_match.group(1)) if status_match else 0
if status in (403, 429, 503) or any(m in res_text for m in BLOCK_MARKERS):
    return False, None, None, "blocked"   # 第 4 元组，供上层熔断
is_valid = bool(colo) and status == 200
```

#### 3.1.3 全局封禁熔断
```python
# 全局熔断器：拦截信号累计 5 次即停整轮，回退 reuse_history 旧结果
circuit = {"blocked": 0}
def record_probe(status):
    if status == "blocked":
        circuit["blocked"] += 1
    if circuit["blocked"] >= 5:
        raise SystemExit("[CIRCUIT] CF 拦截信号累计 5 次，主动终止本轮，等待 24h")
```

#### 3.1.4 LuCI enabled/cron_enabled 校验
在 `ipselect-runner` 的 `run_task()` 开头加：
```sh
ENABLED=$(get_config "enabled" "1")
CRON_ENABLED=$(get_config "cron_enabled" "1")
if [ "$ENABLED" != "1" ]; then
    echo "服务已禁用 (enabled=0)，跳过本次执行。" >> "$LOG_FILE"
    exit 0
fi
if [ "$TRIGGER" = "cron" ] && [ "$CRON_ENABLED" != "1" ]; then
    echo "定时任务已禁用 (cron_enabled=0)，跳过本次执行。" >> "$LOG_FILE"
    exit 0
fi
```

### 3.2 P1 探针改造 + 并发参数化

#### 3.2.1 探针路径替换（保留 SNI）
- 路径从 `HEAD /` 改成 `GET /cdn-cgi/trace`
- SNI 保持 `proxy.19940407.xyz`（不换 cloudflare.com）
- 读取方式从单次 `recv(2048)` 改成读循环（64KB 上限，参照 edgetunnel `_worker.js:165`）
- 机房识别：body `colo=` 解析优先，cf-ray 头解析保留作回退
- 5 IP 灰度 A/B 验证拦截率后全量

#### 3.2.2 并发真实生效点参数化
替换 4 处硬编码并发，集中到配置对象：
| 位置 | 现在 | 改成 |
|---|---|---|
| :2435（阶段1 TCP） | 30 | `stage1_workers` 默认 10 |
| :2467（阶段2 TLS） | 25 | `stage2_workers` 默认 3 |
| :777（Tier1 沙箱） | 15 | `stage3_tier1_workers` 默认 4 |
| :2216（沙箱失败降级） | 10 | `stage3_fallback_workers` 默认 3 |

删除死参数：`THREAD_WORKERS`（无引用）、`SPEED_TEST_NODE_CONCURRENCY`（代码已串行）。

#### 3.2.3 候选分片轮转
每日只扫 1/3 撒点（~1000 IP × 2 端口），3 天一轮。
- 实现：根据日期 `day_of_year % 3` 决定扫哪一段 CIDR 随机范围
- 效果：WAF 看到的扇出量降 2/3，每日冲击更分散

#### 3.2.4 阶段2 早停
区域配额（HK:5 / TW:5 / JP:5 / SG:5 / US:10）已满即截断，不继续探测剩余 IP。

### 3.3 P1 测速降级修正

#### 3.3.1 落点修正
从死路径 `probe_real_download_speed` 改到活跃路径 `probe_throughput_speed`（:1747）。

#### 3.3.2 量纲修正
`max_bytes_per_ip` 现在是每下载流上限，3 流×2 轮 = 每节点最多 6 流各吃满。改成**每节点累计字节熔断**：在 `_run_parallel_round` 返回层累计，达到上限即停止后续轮次。

#### 3.3.3 稳态口径
- 测速时长 6s，**丢弃前 2s 慢启动段**，只统计后 4s 的稳态速度
- 单流字节上限 15~20MB（不是 5MB）
- 样本数从 2 轮增加到 3 轮，取中位

#### 3.3.4 阈值重标定
基于 1 流口径，重标定：
- `min_speed_threshold`：3.0 → **2.0 MB/s**（估算值）
- `vip_speed_threshold`：8.0 → **5.0 MB/s**（估算值）
- **必须通过一致性实验验证**（45 节点 ABBA 交替，Spearman ρ ≥ 0.85 才上线）

#### 3.3.5 边界复测层（L4'）
阈值/配额 ±15% 带内的节点，重测 2 次取中位，把边界误判率从 15~25% 压到 <5%。

### 3.4 P2 三档 preset 化重构

#### 3.4.1 Preset 配置表
```python
POLICY = {
  "daily-light": {
    "stages": ["scan", "colo"],
    "outputs": [],
    "push": False,
    "shard_ratio": 1/3,   # 分片轮转
  },
  "full-pipeline": {
    "stages": ["gate", "speed", "export"],
    "outputs": ["best_us.txt", "_detail.json", "vip_nodes.json"],
    "push": True,
    "shard_ratio": 1/3,
  },
  "vip-inspect": {
    "stages": ["gate", "speed", "vip_update"],
    "outputs": ["vip_nodes.json"],
    "push": True,
  },
}
```

#### 3.4.2 单编排入口串行
一条 cron 触发 runner，runner 内部按序跑三档，不再是三条独立 cron。彻底消除 PID 锁撞车。

#### 3.4.3 阶段4 分层并行
- Top 8 节点：真 60s×12 采样
- 其余节点：30s×10 降级采样
- 4 路独立端口并行（每路独立 Mihomo 实例，避免串台）
- 总耗时从 15 分钟压到 3~4 分钟

### 3.5 P2 可观测性

- 每阶段结构化 JSON 指标（开始/结束时间、通过数、失败数、拦截数）
- 失败/跳过一律通知（Telegram / 飞书）
- history.json 加 `mode` / `stage_durations` 字段
- 全局随机抖动保留但**挪到编排层**，不计入防护预算

---

## 四、实施计划

### 阶段 1：P0 安全修复（立即，本周内）

| 序号 | 任务 | 文件 | 优先级 |
|---|---|---|---|
| 1.1 | 轮换 VLESS UUID + 详情 JSON 只留指纹 + 清洗仓库历史 | filter_us_nodes.py + git filter-repo | P0 |
| 1.2 | 修 probe_tls_colo fail-open，加 403/429/503 识别 | filter_us_nodes.py:1603 | P0 |
| 1.3 | 全局封禁熔断（5 次拦截即停） | filter_us_nodes.py | P0 |
| 1.4 | LuCI enabled/cron_enabled 校验 | ipselect-runner | P0 |
| 1.5 | 沙箱失败降级路径改用 trace 探针 + 间隔 | filter_us_nodes.py:2205 | P0 |

### 阶段 2：P1 探针改造 + 并发参数化

| 序号 | 任务 | 文件 |
|---|---|---|
| 2.1 | 探针路径换 /cdn-cgi/trace，保留 SNI，读循环 + 双解析 | filter_us_nodes.py:1597 |
| 2.2 | 5 IP 灰度 A/B 验证拦截率 | 手动测试 |
| 2.3 | 替换 4 处硬编码并发到配置对象 | filter_us_nodes.py:2435/2467/777/2216 |
| 2.4 | 删死参数 THREAD_WORKERS / SPEED_TEST_NODE_CONCURRENCY | filter_us_nodes.py |
| 2.5 | 候选分片轮转（每日 1/3 撒点） | filter_us_nodes.py |
| 2.6 | 阶段2 早停（区域配额满即截断） | filter_us_nodes.py |

### 阶段 3：P1 测速降级修正

| 序号 | 任务 | 文件 |
|---|---|---|
| 3.1 | 落点改到 probe_throughput_speed | filter_us_nodes.py:1747 |
| 3.2 | 量纲修正：每节点累计字节熔断 | filter_us_nodes.py:1819 |
| 3.3 | 稳态口径：6s 计时 + 弃前 2s | filter_us_nodes.py |
| 3.4 | 单流字节上限 15~20MB | filter_us_nodes.py + UCI |
| 3.5 | 阈值重标定 min_speed=2.0 / vip_speed=5.0 | filter_us_nodes.py + UCI |
| 3.6 | 边界复测层（L4'） | filter_us_nodes.py |
| 3.7 | **一致性实验**：45 节点 ABBA 交替，验证 ρ≥0.85 | 手动测试 |

### 阶段 4：P2 三档 preset 重构 + 可观测性

| 序号 | 任务 | 文件 |
|---|---|---|
| 4.1 | Preset 配置表 + 阶段函数化 | filter_us_nodes.py |
| 4.2 | 单编排入口串行（一条 cron 跑三档） | ipselect-runner + init.d/ipselect |
| 4.3 | 阶段4 分层并行（Top8 真 60s + 其余 30s + 4 路端口并行） | stage3_quality_gate.py |
| 4.4 | 阶段级结构化埋点 + 失败告警 | filter_us_nodes.py + notify.py |
| 4.5 | history.json 加 mode / stage_durations | ipselect-runner |

### 阶段 5：测试与发布

| 序号 | 任务 | 说明 |
|---|---|---|
| 5.1 | 本地 Windows 跑一次完整流程，验证三档模式 | `python scripts/filter_us_nodes.py --mode full-pipeline` |
| 5.2 | 软路由部署新 ipk，观察 CF 后台 Workers 调用数与 CPU 时间 | CF Dashboard → Workers → Metrics |
| 5.3 | 观察 3 天，确认没有触发 WAF 滥用告警 | CF Dashboard → Security → Events |
| 5.4 | 确认 LuCI 开关关闭后 runner 确实拒绝执行 | 手动设 enabled=0，看日志 |
| 5.5 | 一致性实验通过后，正式切到新阈值 | 45 节点 ABBA 验证 |
| 5.6 | 发布 v2.0.0 ipk 到 dist 目录 | `dist/openwrt-24/` 和 `dist/openwrt-25/` |

---

## 五、风险与回滚

### 5.1 潜在风险

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| `/cdn-cgi/trace` 探针识别不出机房 | 阶段2 机房识别准确率下降 | trace 返回的 `colo` 字段就是标准机房码，和 cf-ray 一致；5 IP 灰度验证后再全量 |
| 降并发后跑批总时长变长 | 从 25 分钟变 70~120 分钟 | 本来就是要拉长时间降冲击，这是预期效果 |
| 15~20MB 测速仍不准 | 节点速度排名可能偏差 | 一致性实验验证 ρ≥0.85 才上线；不达标就回滚字节量到 10MB |
| 分片轮转导致每日候选池变化 | 节点池波动大 | VIP 池保留历史好节点，补位机制兜底 |
| 沙箱失败降级路径改动引入新 bug | 沙箱起不来时没有兜底 | 降级路径保留 probe_stability，但改用 trace 探针 + 间隔 |

### 5.2 回滚方案

| 场景 | 回滚方式 |
|---|---|
| 探针改造后拦截率升高 | 把探针路径改回 `HEAD /`，SNI 不变 |
| 测速阈值重标定后节点质量下降 | 回滚到 min_speed=3.0 / vip_speed=8.0 |
| 三档 preset 重构后跑批失败 | 回滚到单条 cron 模式（v1.7.1 ipk） |
| UUID 轮换后客户端连不上 | 把 edgetunnel UUID 改回旧值（应急） |

---

## 附录：关键参数速查表

| 参数 | v1.0 方案值 | v2.0 修正值 | UCI key | 说明 |
|---|---|---|---|---|
| 阶段1 并发 | 3（改死常量 THREAD_WORKERS） | **10**（:2435 硬编码替换） | `stage1_workers`（新增） | 纯 TCP 不进 Worker |
| 阶段2 并发 | 3 | **3** | `stage2_workers`（新增） | 直连 Worker 的阶段2 才需要降 |
| 阶段2 探针目标 | `GET /` SNI=cloudflare.com | `GET /cdn-cgi/trace` **SNI 保持自有域** | — | 路径换，SNI 不换 |
| 候选分片 | 无 | **每日 1/3 撒点，3 天一轮** | `shard_ratio`（新增） | WAF 扇出量降 2/3 |
| 单流下载量 | 5MB | **15~20MB + 6s 稳态口径** | `speed_test_max_bytes_per_ip` | 避免快节点被低估 |
| 测速节点数 | 15 | 15 + 边界复测 | `stage4_speed_limit` | 边界误判压到 <5% |
| min_speed_threshold | 3.0（未提重标定） | **2.0**（待实验验证） | `min_speed_threshold` | 适配 1 流口径 |
| vip_speed_threshold | 8.0（未提重标定） | **5.0**（待实验验证） | `vip_speed_threshold` | 适配 1 流口径 |
| 全局随机抖动 | 0~10 分钟（计入防护） | 0~10 分钟（**无害但不计入防护**，挪到编排层） | `jitter_enabled`（新增） | 心理安慰 |
| 跑批节奏 | 3 条独立 cron（01/02/03点） | **单编排入口串行跑三档** | — | 消除 PID 锁撞车 |
| 阶段4 采样 | 15×60s 串行（假元数据） | **Top8 真 60s + 其余 30s + 4 路端口并行** | — | 真实测量，不造假 |
| 封禁熔断 | 无 | **5 次拦截即停整轮** | — | 防止持续轰击 |
| UUID 泄露治理 | 无 | **轮换 + 指纹化 + 清洗历史** | — | P0 安全修复 |
