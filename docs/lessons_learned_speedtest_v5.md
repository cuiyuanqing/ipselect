# `filter_us_nodes.py` 阶段 4 测速 no_data 深度排障与 v5 架构演进实战录

> **作者**：Jeff Smith
> **维护者**：Jeff Smith
> **本次排障时间**：2026-10-12（一次会话内完成 v1 → v5 全链路修复）
> **关联文件**：`scripts/filter_us_nodes.py` / `best_us.txt` / `best_us_detail.json`

本篇文档完整记录 `scripts/filter_us_nodes.py` 阶段 4（真实微吞吐测速）长期输出 `0.00MB/s` 全军覆没 bug 的根因诊断、修复策略、以及最终 v5 测速引擎架构的演进过程。

**最终落地成果**：`15 个节点中 12 个拿到真实 per-node 速度`（2.04 ~ 5.67 MB/s），相比初始 `0/15` 全部 0.00 提升巨大。

---

## 目录

1. [故障现象与业务影响](#一故障现象与业务影响)
2. [三层根因复盘](#二三层根因复盘)
3. [Mihomo mixed-port HTTP forward proxy 行为研究](#三mihomo-mixed-port-http-forward-proxy-行为研究)
4. [v1 → v5 五个版本演进历程](#四v1--v5-五个版本演进历程)
5. [v5 最终架构详解](#五v5-最终架构详解)
6. [Cloudflare self-test 禁忌清单](#六cloudflare-self-test-禁忌清单)
7. [测速源选型白名单/黑名单](#七测速源选型白名单黑名单)
8. [诊断日志体系（`_probe_one_node` + `[DBG-NO-DATA]`）](#八诊断日志体系_probe_one_node--dbg-no-data)
9. [软路由 vs 本地 vs GitHub Actions 三种环境对照](#九软路由-vs-本地-vs-github-actions-三种环境对照)
10. [复现验证清单](#十复现验证清单)
11. [后续优化方向](#十一后续优化方向)
12. [阶段 3 真实质量门 v2](#十二阶段-3-真实质量门-v2-2026-10-12)

---

## 一、故障现象与业务影响

### 现象

`best_us.txt` 长期输出 15 行 `#US-公司-NN-0.00MB/s`：

```text
104.17.149.83:8443#US-公司-01-0.00MB/s
104.17.157.31:8443#US-公司-02-0.00MB/s
104.17.157.154:8443#US-公司-03-0.00MB/s
...
104.17.153.30:8443#US-公司-15-0.00MB/s
```

`best_us_detail.json` 内所有节点 `speed: 0.0` / `speed_samples: []` / `speed_status: "no_data"`。

阶段 3 E2E 真实代理实测却完全成功（e2e_avg_rtt 700~800ms，youtube_rtt 900~1075ms，exit_country: US，exit_ip 真实 CF IP）。

**业务影响**：客户端实际打开订阅拉取 15 节点后，由于没有真实速度评分作为参考，排序退化为仅看 RTT + jitter，节点列表实际上是随机均匀分布而非按速度排序。

### 影响范围

- 公司软路由 `10.10.18.2`（Linux iStoreOS 25.12.5）
- 家庭软路由 `192.168.0.3 / 192.168.0.2`（待同步更新）
- 任何调用 `scripts/filter_us_nodes.py` 的本地/云端执行

---

## 二、三层根因复盘

### Layer 1：edgetunnel Worker 不接受 plain HTTP 测速流量

**实证**：

| 请求 | 经 Mihomo 19998 + VLESS 后 |
|---|---|
| `http://speed.cloudflare.com/...` | **204 No Content**（不返回 body） |
| `http://example.com/` | **400 Bad Request** |
| `http://1.1.1.1/` | **400 Bad Request** |

edgetunnel Worker（部署在 CF Workers）默认不处理 plain HTTP 流量直接返回 204/400。

**结论**：测速必须用 `https://`，让请求走 CONNECT 隧道。

### Layer 2：Cloudflare speed.cloudflare.com 测速域被 CF 自身 WAF 拦截

**实证**：

经 Mihomo HTTPS CONNECT → VLESS → edgetunnel Worker → `https://speed.cloudflare.com/__down?bytes=N`：

```text
SSL: UNEXPECTED_EOF_WHILE_READING         ← 服务端提前关闭 TLS 连接
SSL: SSLV3_ALERT_HANDSHAKE_FAILURE        ← 服务端拒绝 TLS 握手协议
SSL handshake timed out                   ← TCP/TLS 握手超时
```

**根因**：edgetunnel Worker 的出站连接（Python 通过 Mihomo + VLESS + Worker 反代），对 Cloudflare 自己的测速域名 `speed.cloudflare.com` 触发 Cloudflare 自身的反爬 / WAF 检测。

**判断依据**：同一 VLESS proxy 同时测 `https://example.com/` → 200 OK + 577 bytes，说明 TLS 链路通畅；测 speed.cloudflare.com 就 SSL EOF，**说明是 CF 自家 WAF 主动拦截**。

### Layer 3：proof.ovh.net 真实服务端限速

**实证**：

经 VLESS 测 `https://proof.ovh.net/files/100Mb.dat`：

```text
HTTP/1.1 429 Too Many Requests
Retry-After: 10
Server: nginx
Content-Type: text/html
Content-Length: 1606
```

OVH 服务器对来自 Cloudflare Worker 出站的请求频率做严格限制，返回 `429 + Retry-After: 10s`。

**额外发现**：

经大陆直连测试 `https://speed.hetzner.de/100MB.bin` 和 `https://speedtest.tokyo.linode.com/100MB-tokyo.bin`：

```text
SSL: UNEXPECTED_EOF_WHILE_READING         ← 没有任何 VLESS 也失败
```

这俩域名走的也是 CF CDN 加速（Hetzner / Linode 自家测速域常用 CF Web 加速），被 GFW 链封或被 TLS 拦截，**无论走不走 VLESS 都不可用**。

### 概念层级图

```
            +-----------------------------+
            | [Python urllib]             |
            |  proxy=http://127.0.0.1:19998 |
            +-------------+---------------+
                          |
                          v
            +-----------------------------+      Layer 1: 协议层
            | [Mihomo mixed-port 19998]   | ←→ plain HTTP 拒绝 / HTTPS CONNECT 隧道
            |  mode: global               |
            |  GLOBAL = node_XX (动态切换) |
            +-------------+---------------+      Layer 2: 反代层
                          |                       ↑ CF self-test 反爬
                          v                       ↓ TLS 拦截
            +-----------------------------+
            | [VLESS+WS+TLS]             |
            |  通过 104.17.x.x CF 边缘     |
            +-------------+---------------+
                          |
                          v
            +-----------------------------+      Layer 3: 落地层
            | [edgetunnel Worker]         |
            |  反代到外网目标              |
            +-------------+---------------+
                          |
                          v
            +-----------------------------+
            | [目标测速服务器]             | ← proof.ovh.net 429
            |                             | ← speed.cloudflare.com 拒
            |                             | ← Hetzner/Linode 链封
            +-----------------------------+
```

---

## 三、Mihomo mixed-port HTTP forward proxy 行为研究

### 关键发现清单

| 场景 | 行为 |
|---|---|
| 空 Mihomo（DIRECT，无 proxy）+ `http://speed.cloudflare.com` | ✅ 200 OK + 200KB，0.3s |
| 空 Mihomo + `http://1.1.1.1/` | ✅ 400 Bad Request（CF 域名不支持） |
| Mihomo + VLESS + `http://example.com/` | ❌ 400 Bad Request（Worker 拒 plain HTTP） |
| Mihomo + VLESS + `https://example.com/` | ✅ 200 OK + 577 bytes |
| Mihomo + VLESS + `http://speed.cloudflare.com/` | ❌ **204 No Content** |
| Mihomo + VLESS + `https://speed.cloudflare.com/` | ❌ SSL UNEXPECTED_EOF（CF WAF） |
| Mihomo + VLESS + `https://proof.ovh.net/files/100Mb.dat` | ✅ 200，0.16~0.65 MB/s |

### 关键行为：GLOBAL Selector 单选

`Mihomo mode: global` 下，`mixed-port: 19998` 的 HTTP forward proxy 请求**全部走当前 GLOBAL selector 选中的那一个 proxy**。

```text
Mihomo GLOBAL = node_53      ← 阶段 3 最后一个 switch_sandbox_proxy 留下的
                        ↓
所有经 19998 的 HTTP 代理请求都走 node_53（不管 URL target 是哪）
```

**后果**：4 线程并发测速时，每个线程发的请求都被强制走同一个 proxy，30 节点测速值**都相同**，等于没测（v2 实测证明了这一 bug）。

### Mihomo REST API 可用能力

通过 `http://127.0.0.1:19098/proxies/...` 控制 sandbox：

| 方法 | 作用 |
|---|---|
| `GET /version` | 检查 Mihomo 是否启动 |
| `GET /proxies/GLOBAL` | 查询当前选中 proxy |
| `PUT /proxies/GLOBAL {name: "node_XX"}` | 切换到指定 proxy |
| `GET /proxies/<name>/delay?url=...` | 测单 proxy 延迟（与阶段 3 E2E 用） |
| ❌ 无 | **没有公开的 per-proxy speedtest API** |

---

## 四、v1 → v5 五个版本演进历程

| 版本 | 改动 | 数据节点 | 最高速度 | 评估 |
|---|---|---|---|---|
| **v1（原始）** | 无 | 0/15 | 0.00 | bug 全军覆没 |
| **v2** | 测速源换为 proof.ovh.net + https；rate_limited-but-keep-samples | 5/15 | 0.11 MB/s | 有数据但假（共享 GLOBAL） |
| **v3** | 加 per-node Mihomo GLOBAL selector 切换；顺序测速；多源 | 1/15 | 0.65 MB/s | 真 per-node，但测速源被 CF CDN 拦 |
| **v4** | 加 Hetzner/Linode | 0/15 | 0.00 | 没意识到这俩被 GFW 链封 |
| **v5** | 测速源缩到 OVH/CacheFly/Singapore-Linode + adaptive backoff | **12/15** | **5.67 MB/s** | ✅ **生产可用** |

### v5 关键决策

1. **Cloudflare self-test 禁忌**（Jeff Smith 明确强调）— 删 `speed.cloudflare.com / cloudflare.com / cdn-cgi / 1.1.1.1`
2. **避开 CF CDN 加速的"独立 IDC"测速点**（Hetzner/Linode Tokyo 用 CF Web 加速，直连就 SSL EOF）
3. **实测可达的 3 个 HTTPS 测速源**（OVH/CacheFly/Singapore-Linode）
4. **adaptive backoff**：连续 3 个测速失败自动 `30s + jitter` 暂停，让上游测速服务喘息
5. **测速上限 80MB → 10MB**：CF/OVH 服务端对小请求更友好

---

## 五、v5 最终架构详解

### 双 Mihomo 隔离架构

```
┌─────────────────────────────────────┐
│ [生产 mihomo]                       │   ← 完全不动
│   Jeff Smith 日常代理工作            │
└─────────────────────────────────────┘
   ↑ 隔离，永远不交互

┌─────────────────────────────────────┐
│ [测速 sandbox mihomo] 独立进程       │
│   external-controller: 19098        │   优选脚本独占
│   mixed-port: 19998                 │
│   Proxies=[node_01..node_75]        │   包含 E2E 入围的 30+ 个候选 VLESS 节点
└─────────────────────────────────────┘
   ↑ 脚本控制，REST API 切到任意节点测速
```

### 阶段 4 核心流程

```python
# 阶段 4 主循环（v5）
consecutive_429 = 0
for idx, node in enumerate(candidates_for_speed, 1):
    if idx > 1:
        base_jitter = random.uniform(1.0, 3.0)
        penalty_jitter = min(10.0, consecutive_429 * 1.5)
        time.sleep(base_jitter + penalty_jitter)

    # [per-node] 切换 sandbox GLOBAL 到本节点
    sandbox.switch_proxy(node["proxy_name"])

    # [per-node] 多源 URL 轮换（避开单一测速点限速）
    src_url = random.choice(SPEED_TEST_SOURCES_DEFAULT)
    res = probe_throughput_speed(
        ip=node["ip"], port=node["port"],
        duration=5.0,                    # 5s 测速窗口
        parallel=3,                      # 每节点 3 worker 并发
        max_bytes_per_ip=10 * 1024 * 1024,  # 10MB 上限（防 429）
        sources=(src_url,),
        proxy_port=19998,
    )
    node["speed"] = res["speed"]

    # [adaptive] 累计连续失败
    if node.get("speed", 0) <= 0 and res["status"] in ("rate_limited", "no_data"):
        consecutive_429 += 1
    else:
        consecutive_429 = 0

    # [adaptive] 连续 3 失败自动暂停 30s
    if consecutive_429 >= 3:
        time.sleep(30 + random.uniform(0, 10))
        consecutive_429 = 0
```

### 测速源白名单（已大陆直连实测）

```python
SPEED_TEST_SOURCES_DEFAULT = (
    "https://proof.ovh.net/files/100Mb.dat",                       # OVH France（主源）
    "https://cachefly.cachefly.net/100mb.test",                    # CacheFly CDN
    "https://speedtest.singapore.linode.com/100MB-singapore.bin",  # Linode Singapore 备源
)
```

### 测速上限

```python
SPEED_TEST_MAX_BYTES_MB = int(os.environ.get("SPEED_TEST_MAX_BYTES_PER_IP", "10"))  # 默认 10MB
```

### 配套环境变量（与 README 章节对应）

| UCI 选项 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `speed_test_duration` | `SPEED_TEST_DURATION` | 5 | 单节点测速时间窗(秒) |
| `speed_test_parallel` | `SPEED_TEST_PARALLEL` | 3 | 节点内并发下载数 |
| `speed_test_node_concurrency` | `SPEED_TEST_NODE_CONCURRENCY` | 4 | **v5 改为顺序执行（不并发不同 node）** |
| `speed_test_max_retry` | `SPEED_TEST_MAX_RETRY` | 1 | 429/1020 重试次数 |
| `speed_test_max_bytes_per_ip` | `SPEED_TEST_MAX_BYTES_PER_IP` | **10**（v5 改） | 单 IP 累计下载上限(MB) |

---

## 六、Cloudflare self-test 禁忌清单

### 绝对黑名单

| 域名 | 原因 |
|---|---|
| `speed.cloudflare.com` | CF 官方测速端点，CF WAF 自动拦截 VLESS Worker 反代流量 |
| `cloudflare.com/cdn-cgi/trace` | CF 自家 API |
| `cdn-cgi.commondatastorage.googleapis.com` | Google 走 CF CDN，不可用 |
| `1.1.1.1` 任何路径 | CF 自家 DNS / API |
| **任何 `*.cloudflare.com` 子域** | 全部无效 |
| **`speed.hetzner.de`**（陷阱） | Hetzner 用 CF Web 加速，链封 |
| **`speedtest.tokyo.linode.com`**（陷阱） | Linode 用 CF Web 加速，链封 |
| **`speedtest.digitalocean.com`**（陷阱） | 同样问题 |

### 【核心逻辑】为什么不能用 Cloudflare 自家测速

edgetunnel Worker 落地节点本身就部署在 Cloudflare Worker 上。如果用 Cloudflare 自家测速域：

```
测速请求路径：
  家庭宽带 → Mihomo → VLESS → 104.17.x.x CF 边缘 → edgetunnel Worker → Cloudflare speed.cloudflare.com
  
结果测的是：
  ┌→ CF Worker 出口 → Cloudflare 自家 CDN（内网链路！）
  
真实落地带宽应该是：
  ┌→ CF Worker 出口 → OVH/CacheFly 等真实外部服务器
```

前者是 Cloudflare 内部回环，**严重低估真实落地网速**（因为内网比出口快很多倍），且不能反映"出国带宽"指标。

---

## 七、测速源选型白名单/黑名单

### ✅ 已大陆直连实测可用的 HTTPS 测速源（2026-10-12）

```python
URL_TRUST_LIST = (
    "https://proof.ovh.net/files/100Mb.dat",                       # 0.41 MB/s 直连
    "https://cachefly.cachefly.net/100mb.test",                    # 0.96 MB/s 直连
    "https://speedtest.singapore.linode.com/100MB-singapore.bin",  # 0.20 MB/s 直连
)
```

### ✅ 备选白名单（未实测，按社区信誉列出）

| 域名 | 说明 |
|---|---|
| `https://speed.ovh.net/` | OVH 备份测速（与 proof.ovh.net 同机房） |
| `https://mirror.gcr.io/...` | Google Container Registry，自有大带宽 |
| `https://releases.ubuntu.com/...` | Canonical 官方，HTTP/2 高效 |
| `https://download.docker.com/...` | Docker 官方 |
| `https://artifacts.elastic.co/...` | Elastic 官方 |
| `https://*.cloudfront.net` | AWS CloudFront（注意：需要确认没挂 CF） |
| `https://*.amazonaws.com` | AWS S3 / CloudFront |

### ❌ 绝对黑名单

- `*.cloudflare.com` 任何子域
- `cdn-cgi.*`
- 任何"独立 IDC"测速域走 CF CDN 加速的（Hetzner / Linode / DigitalOcean 部分测速点）

---

## 八、诊断日志体系（`_probe_one_node` + `[DBG-NO-DATA]`）

### 关键日志前缀

`[DBG-NO-DATA]` —— 所有阶段 4 调试日志都用这个前缀，便于 `grep` 过滤。

### 主要诊断点

```python
# 1. 阶段 4 入口：沙箱状态 + Mihomo GLOBAL 当前指向
if sandbox is not None:
    sb_ready = sandbox.is_ready()
    print(f"[DBG-NO-DATA] sandbox.is_ready()={sb_ready} mock_mode={sandbox.mock_mode}")
    if sb_ready:
        _gp = urllib.request.Request("http://127.0.0.1:19098/proxies/GLOBAL")
        _gstate = json.loads(_gp.read())
        print(f"[DBG-NO-DATA] Mihomo GLOBAL 当前指向: {_gstate}")

# 2. _single_download HTTPError 捕获
except urllib.error.HTTPError as e:
    if e.code in (429, 1020):
        hit_429 = True
        print(f"[DBG-NO-DATA] HTTPError code={e.code} (RATE_LIMITED_TRIGGER) "
              f"retry_after={_parse_retry_after(e.headers)}s headers={...}")
    else:
        print(f"[DBG-NO-DATA] HTTPError code={e.code} ...")

except Exception as e:
    print(f"[DBG-NO-DATA] _single_download Exception type={type(e).__name__} msg={...}")

# 3. no_data 分支
if bytes_done < 1024:
    print(f"[DBG-NO-DATA] round{round_idx} NO_DATA: ip={...} src_url={sources[0]} "
          f"bytes_done={bytes_done} elapsed={elapsed:.2f}s hit_429={hit_429}")

# 4. rate_limited 但 samples 已有的修复
if not samples:
    status = "rate_limited"
else:
    print(f"[DBG-NO-DATA] rate_limited-but-keep-samples: retained_samples={samples}")

# 5. 每个节点测速完成
node["speed"] = res["speed"]
print(f"[DBG-NO-DATA] _probe_one_node NON-OK ip={...} speed={...} status={...} samples={...}")

# 6. switch_proxy 失败
if not switched:
    print(f"[DBG-NO-DATA] _probe_one_node switch_proxy FAILED ip={...} proxy_name={...}")

# 7. adaptive backoff 触发
if consecutive_429 >= 3:
    print(f"[DBG-NO-DATA] adaptive backoff: 连续 {consecutive_429} 个测速失败，暂停 {backoff:.1f}s")
    time.sleep(backoff)
```

### 推荐 grep 用法

```bash
# 整体状态分布
grep "_probe_one_node" /root/ipselect/update.log | sed -E 's/.*status=([^ ]+).*/\1/' | sort | uniq -c

# 异常类型分布
grep -oE "Exception type=[A-Za-z]+|HTTPError code=[0-9]+" update.log | sort | uniq -c

# 源使用分布
grep -oE "src_url=https://[^/]+/" update.log | sort | uniq -c

# 真实速度节点
grep "_probe_one_node" update.log | grep -E "speed=[1-9]|speed=0\.[1-9]"

# 进度
grep "进度" update.log
```

---

## 九、软路由 vs 本地 vs GitHub Actions 三种环境对照

### 软路由（生产）— iStoreOS 25.12.5

| 项目 | 状态 |
|---|---|
| Python | 3.13.15 ✅ |
| Mihomo 内核 | `/etc/openclash/core/clash_meta`（13MB）✅ |
| GID=65534 | 可设（root 用户）✅ |
| CF worker 测速 | ✅ 与生产效果一致 |
| 网络特征 | 单次多请求易触发目标 server 限速 |

### 本地 Windows（开发）— 无 Mihomo

| 项目 | 状态 |
|---|---|
| Python | 可装 |
| Mihomo 内核 | ❌ 未安装 → 阶段 4 `no_data`（19998 Connection refused） |
| 用途 | 仅用于语法/单元测试，不能验证阶段 4 |
| WinError 10061 | 本地确认端口无人监听，不是其他原因 |

### GitHub Actions 云端（停用）

```yaml
# .github/workflows/update_us_nodes.yml 已停用云端定时任务
# schedule:
#   - cron: '0 20 * * *'  # 改为本地 iStoreOS 路由器跑
```

原因：云端跑出来的 15 个美区节点是基于云端出口的，**不代表 Jeff Smith 在国内家庭/公司宽带的物理出口真实体验**。

---

## 十、复现验证清单

下次怀疑阶段 4 异常时，按以下清单排查：

| # | 检查 | 命令/位置 |
|---|---|---|
| 1 | Mihomo 内核是否存在 | `ls -la /etc/openclash/core/clash_meta` |
| 2 | 阶段 4 沙箱启动成功 | 日志：`隔离沙箱启动成功` |
| 3 | Mihomo REST API 可达 | `curl http://127.0.0.1:19098/version` → 200 |
| 4 | Mihomo GLOBAL 已切到目标节点 | 日志：`Mihomo GLOBAL 当前指向: {... 'now': 'node_NN' ...}` |
| 5 | _probe_one_node 输出 `status != no_data` | `grep "_probe_one_node" update.log` |
| 6 | 测速源无 502/SSL EOF/UNEXPECTED_EOF | `grep "Exception type" update.log` |
| 7 | 节点间是否生效 adaptive backoff | `grep "adaptive backoff" update.log` |
| 8 | 测速源是否被国家防火墙链封 | 大陆出口直连测试 `python -c '...'` |

### 关键 check

```bash
# 一次性诊断所有阶段 4 指标
python3 -c "
import re
log = open('/root/ipselect/update.log').read()
print('--- 异常类型分布 ---')
for m in re.findall(r'Exception type=(\\w+)|HTTPError code=(\\d+)', log):
    print(m)
print('--- _probe_one_node 状态分布 ---')
for m in re.findall(r'_probe_one_node .* status=(\\S+)', log):
    print(m)
print('--- 测速源分布 ---')
for m in re.findall(r'src_url=(https://[^/]+/)', log):
    print(m)
"
```

---

## 十一、后续优化方向

### 1. adaptive backoff 阈值再降（期望覆盖率 12/15 → 14/15）

```python
if consecutive_429 >= 2:  # 当前是 3
    time.sleep(20 + random.uniform(0, 5))  # 当前 30s
```

### 2. 加入"测速源健康度"动态剔除

```python
# 阶段 4 启动前，先 HEAD 探测 3 个源
# 失败的源标记 dead，本轮不再用
# 30 分钟后重新探活
```

### 3. 多线程池化测速（**需要 lock_selector**）

```python
# 4 线程并发测速，但每个线程独立维护 selector 状态
# 或改用 Mihomo URLTest selector 自动 round-robin
```

### 4. 测速字节分档（根据节点 RTT 自适应）

```python
# RTT > 500ms 的节点只下 2MB（RTT 大测 10MB 反而浪费）
# RTT < 200ms 的节点可下 10MB
```

### 5. 添加 IPv6 测速源

```python
# OVH / Hetzner 部分机房有 IPv6，可作备源
```

### 6. 与 GitHub Actions 集成兜底

```yaml
# 如果本地脚本异常未跑，GitHub Actions 兜底跑一版（仅参考价值低）
```

---

## 附录 A：关键 Git Diff 摘要（v1 → v5）

### A.1 `SPEED_TEST_MAX_BYTES_MB` 默认值

```python
# v1
SPEED_TEST_MAX_BYTES_MB = int(os.environ.get("SPEED_TEST_MAX_BYTES_PER_IP", "80"))
# v5
SPEED_TEST_MAX_BYTES_MB = int(os.environ.get("SPEED_TEST_MAX_BYTES_PER_IP", "10"))
```

### A.2 `SPEED_TEST_SOURCES_DEFAULT` 重写

```python
# v1 (单 host tuple)
SPEED_TEST_SOURCES_DEFAULT = ("speed.cloudflare.com", "proof.ovh.net")
# v2 (单 host)
SPEED_TEST_SOURCES_DEFAULT = ("proof.ovh.net",)
# v3+v4 (3 hosts but partially broken)
SPEED_TEST_SOURCES_DEFAULT = (
    "https://proof.ovh.net/files/100Mb.dat",
    "https://speed.cloudflare.com/__down?bytes=10485760",   # 删
    "https://1.1.1.1/cdn-cgi/trace",                          # 删
)
# v5 (3 可靠源)
SPEED_TEST_SOURCES_DEFAULT = (
    "https://proof.ovh.net/files/100Mb.dat",
    "https://cachefly.cachefly.net/100mb.test",
    "https://speedtest.singapore.linode.com/100MB-singapore.bin",
)
```

### A.3 `probe_throughput_speed` URL 协议修复

```python
# v1 (HTTP only)
url = f"http://{src_host}/__down?bytes={max_bytes_per_ip}"
# v2+ (HTTPS)
url = f"https://{src_host}/__down?bytes={max_bytes_per_ip}"
# Range 头去掉（CF 拒绝 Range + __down 组合）
# v5 不带 Range
```

### A.4 `_probe_one_node` 加 switch_proxy

```python
def _probe_one_node(node, mock_speed_value):
    # [per-node 核心] 切 sandbox GLOBAL 到本节点
    if not mock_mode and sandbox.is_ready():
        sandbox.switch_proxy(node["proxy_name"])
    ...
```

### A.5 阶段 4 主循环改顺序 + 自适应退避

```python
# v1
with ThreadPoolExecutor(max_workers=SPEED_TEST_NODE_CONCURRENCY) as executor:
    futures = [executor.submit(_probe_one_node, n, None) for n in candidates_for_speed]

# v5 (顺序 + jitter + adaptive backoff)
consecutive_429 = 0
for idx, node in enumerate(candidates_for_speed, 1):
    if idx > 1:
        time.sleep(random.uniform(1.0, 3.0) + min(10.0, consecutive_429 * 1.5))
    _probe_one_node(node, None)
    if node.get("speed", 0) <= 0:
        consecutive_429 += 1
    else:
        consecutive_429 = 0
    if consecutive_429 >= 3:
        time.sleep(30 + random.uniform(0, 10))
        consecutive_429 = 0
```

### A.6 异常处理完善（不再静默吞掉 HTTPError）

```python
# v1 (静默)
except urllib.error.HTTPError as e:
    if e.code in (429, 1020):
        hit_429 = True
# 502/504/400/403/SSL EOF 全部静默！
except Exception:
    pass

# v5 (全部打印)
except urllib.error.HTTPError as e:
    if e.code in (429, 1020):
        hit_429 = True
        print(f"[DBG-NO-DATA] HTTPError code={e.code} (RATE_LIMITED_TRIGGER) ...")
    else:
        print(f"[DBG-NO-DATA] HTTPError code={e.code} ...")
except Exception as e:
    print(f"[DBG-NO-DATA] _single_download Exception type={...} msg={...}")
```

### A.7 rate_limited 时保留已有 samples

```python
# v1
if hit_429:
    status = "rate_limited"
    break  # ← bug：丢弃 samples

# v5
if hit_429:
    if not samples:
        status = "rate_limited"
    else:
        status = "ok_rate_limited_in_retry"  # 保留合法数据
    break
```

### A.8 `_probe_one_node` 不再无脑归零

```python
# v1 (无脑归零)
if res["status"] != "ok":
    node["speed"] = 0.0

# v5 (有合法 samples 时保留)
if res["status"] not in ("ok", "ok_rate_limited_in_retry") and res["speed"] <= 0:
    node["speed"] = 0.0
elif res["status"] not in ("ok", "ok_rate_limited_in_retry") and res["speed"] > 0:
    pass  # 保留 res["speed"]
```

---

## 附录 B：参考资源

- Mihomo REST API 文档：https://github.com/MetaCubeX/mihomo
- Cloudflare Workers 反爬机制：https://developers.cloudflare.com/workers/
- edgetunnel 项目：https://github.com/cmliu/edgetunnel
- WorkerVless2sub 项目：https://github.com/cmliu/WorkerVless2sub

---

> **本次排障 ID**：IPSELECT-NO-DATA-2026-10-12
> **最终版本**：v5（2026-10-12 09:38:48 部署）
> **下次审查触发条件**：连续 3 天 best_us.txt 速度覆盖率 < 60%，或收到 CF/OVH 域名变化告警。


## 十二、阶段 3 真实质量门 v2 (2026-10-12)

### 5.1 旧版 (v1) 痛点
- 阶段 3 只测 `generate_204` 探针：单次快照 + 极轻量
- 客户端浏览器打开 google.com 远比 204 探测复杂
- 节点质量时间漂移（"前 1 分钟能 下一秒不能"）完全捕捉不到

### 5.2 v2 改进
- 真实页面探测：google.com + youtube.com 主页首 256KB + 多模式 title 匹配
- 60s 连续区间密集采样：5s × 12 探，同步双 URL，通过率 ≥ 11/12
- 三级降级链路：8-11 节点 → 30s 重测；4-7 节点 → 204 兜底；<4 节点 → 不输出
- 10min 超时保护 + v4 强制上下文

### 5.3 设计权衡
- 256KB 而非 1MB：HTML 主体可达已足够，子资源由浏览器处理
- 同步双 URL 而非 12+12：单轮判定双边都过，避免单边阻塞
- 5s 间隔：平衡冷启动开销与抖动捕捉粒度
- 11/12 阈值：容许 1 次瞬时抖动，2 次失败 = 10s 不可用 = 客户端无法容忍

### 5.4 性能预算
- 阶段 3 整体: 5-7 min (vs 旧 30-60s)
- 总脚本: 8-12 min (vs 旧 5-8 min)
- cron 凌晨 04:30 跑时间充裕

### 5.5 推送 gate 兼容
- `STATUS=success` + `GIT_STATUS=pushed` → 联动 OpenClash (v1 行为)
- `STATUS=quality_downgraded` + `GIT_STATUS=pushed` → 联动 OpenClash (v2 新增, 走 204 兜底仍可信)
- `STATUS=no_quality` → 拒绝推送, 拒绝 OpenClash 联动
