# 全链路真实代理沙箱节点优选引擎 (End-to-End Sandbox Node Evaluation Pipeline) 设计规范

## 1. 概述与背景

在现有的 Cloudflare Anycast 节点优选体系中，本地路由器的测速筛选仅覆盖了：
$$\text{路由器 (本地宽带)} \xrightarrow{\text{直连测速}} \text{Cloudflare Anycast 边缘入口 (SJC/LAX)}$$

然而，用户的真实科学上网完整通信链路为：
$$\text{本地设备} \xrightarrow{\text{1. 本地入口}} \text{CF Anycast 优选 IP} \xrightarrow{\text{2. CF 骨干网}} \text{edgetunnel (Worker)} \xrightarrow{\text{3. 出口代理}} \text{ProxyIP} \xrightarrow{\text{4. 国际公网}} \text{目标网站 (Google/YouTube)}$$

单纯基于高速入口做 TLS 握手及下载测速，无法感知后半段真实代理链路的致命问题：
1. **出口 ProxyIP 状态黑天鹅**：ProxyIP 负载过高、被墙、丢包率高或频繁 Timeout；
2. **协议鉴权与 WebSocket 未被验证**：传统测试仅走 HTTP HEAD，未验证 VLESS UUID 鉴权与 WebSocket 协议升级是否被 CF WAF 阻断；
3. **真实目标风控拦截**：出口 IP 访问 Google 触发人机验证、访问 YouTube 遭遇限速，或地理位置发生非预期漂移（偏离美区）。

本规范设计了一套基于 **Mihomo (Clash Meta) 直连隔离沙箱** 的端到端全链路真实代理测试体系，彻底解决“入口测速高但实际不可用/时好时坏”的顽疾。

---

## 2. 核心架构设计

### 2.1 直连沙箱网络隔离机制 (Sandbox Isolation via GID 65534)

在 iStoreOS / OpenWrt 系统中，OpenClash 透明代理（TProxy/REDIRECT）会劫持路由器的 DNS 及 WAN 出口流量。为保证全链路测试数据真实，且不干扰家庭/公司正常代理上网，沙箱采用如下隔离机制：

```
                                  【iStoreOS 路由器】
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                                                                             │
  │  [普通业务 / 局域网设备] ──► [OpenClash 透明代理 (7892/7895)] ──► 正常外网   │
  │                                                                 (不受任何影响)│
  │                                                                             │
  │  [优选引擎 filter_us_nodes.py]                                              │
  │     │                                                                       │
  │     ├─► 生成独立临时配置 /tmp/ipselect_sandbox/sandbox.yaml                 │
  │     │                                                                       │
  │     └─► 启动微内核沙箱 (/etc/openclash/core/clash_meta)                      │
  │           │                                                                 │
  │           ├─► 运行属性: GID=65534 (nogroup), 仅监听 127.0.0.1:19098/19998   │
  │           │   (无 TUN / 无透明代理劫持 / 零系统网络修改)                     │
  │           │                                                                 │
  │           └─► 触发 nftables 规则: meta skgid 65534 return                   │
  │                 └──► 100% 绕过 OpenClash ──► [WAN 真实物理网卡直连出海]      │
  │                                                                             │
  └─────────────────────────────────────────────────────────────────────────────┘
```

1. **零新增依赖**：复用系统中已有且性能卓越的 `/etc/openclash/core/clash_meta`；
2. **底层 nftables 放行**：nftables 规则链内置 `meta skgid 65534 return`，赋予沙箱进程 `GID 65534` 即可 100% 绕过 OpenClash，直连物理 WAN 网卡；
3. **安全自愈生命周期**：
   - 沙箱仅在全链路探测阶段（阶段 3~4）临时启动；
   - 内存占用仅 ~20MB，无 TUN 模式；
   - Python 脚本采用 `try...finally` 强保证退出时通过 `SIGTERM` 终结沙箱进程并清理 `/tmp/ipselect_sandbox` 临时目录。

---

## 3. 五阶漏斗全链路严选引擎 (Five-Stage Funnel Pipeline)

为平衡筛选精度与总执行耗时，采用漏斗分级逐步深化的测试模型：

| 阶段 | 阶段定位 | 探测对象与方法 | 候选规模 | 典型耗时 |
| :--- | :--- | :--- | :--- | :--- |
| **阶段 1** | **快速 TCP 初筛** | 快速并发 TCP 握手 (直连候选 IP:端口) | 70+ $\to$ ~50 | ~1s |
| **阶段 2** | **Anycast 入口质检** | TLS 握手与 `cf-ray` 边缘 POP 校验 (淘汰高延迟与绕路 POP) | 50 $\to$ 25 | ~5s |
| **阶段 3** | **沙箱全链路真机实测** | **挂载入围节点至直连沙箱，跑完整 VLESS 隧道**：<br>① **Google 204** 连通性与多轮真实端到端 RTT<br>② **YouTube 204** 流媒体服务可达性与响应时延<br>③ **ip.sb / ipleak** 真实出口美国落地归属校验与风控排查 | 25 $\to$ ~18 | ~15s |
| **阶段 4** | **全链路真实微吞吐** | 通过 VLESS 代理隧道拉取 1.0MB 数据块，测量真实下载速度 (MB/s) | 18 $\to$ 15 | ~15s |
| **阶段 5** | **多维综合评分决策** | 基于四维加权打分模型输出最终 Top 15 黄金节点 | 最终 Top 15 | <1s |

**总执行耗时**：严格控制在 **35 ~ 45 秒** 以内。

---

## 4. 全链路指标矩阵与评分裁决模型

### 4.1 一票否决机制 (Hard Failures)
任何候选节点触发以下任一红线直接淘汰：
1. **翻墙断流**：Google 204 或 YouTube 204 探针超时或 HTTP 状态非 204/200；
2. **多轮严重丢包**：3 轮探针中丢包率 $\ge 30\%$；
3. **出口漂移**：`api.ip.sb/geoip` 返回的 `country_code` 非 `US`（严防被出口 ProxyIP 漂移至香港/日本/新加坡等非美区节点）；
4. **出口风控/拦截**：目标网站返回 403 Forbidden、Cloudflare 盾或 Captcha 拦截。

### 4.2 四维综合评分体系 (1.0 ~ 10.0 分制)

$$S = 1.0 + 35\% \times S_{\text{端到端RTT}} + 25\% \times S_{\text{链路抖动}} + 30\% \times S_{\text{真实下载吞吐}} + 10\% \times S_{\text{核心机房加权}}$$

* **真实端到端 RTT 时延分（权重 35%）**：
  * $< 180\text{ ms}$: 满分 10.0 分
  * $180\text{ ms} \sim 220\text{ ms}$: $8.5 \sim 9.5$ 分
  * $220\text{ ms} \sim 280\text{ ms}$: $6.5 \sim 8.0$ 分
  * $> 350\text{ ms}$: 3.0 分以下
* **链路平稳度抖动分（权重 25%）**：
  * 抖动 $\Delta < 15\text{ ms}$: 满分 10.0 分
  * 抖动 $15 \sim 35\text{ ms}$: $7.5 \sim 9.0$ 分
  * 抖动 $> 60\text{ ms}$: 4.0 分以下
* **全链路真实下载吞吐分（权重 30%）**：
  * $> 2.0\text{ MB/s}$: 满分 10.0 分
  * $1.2 \sim 2.0\text{ MB/s}$: $8.0 \sim 9.0$ 分
  * $0.6 \sim 1.2\text{ MB/s}$: $6.0 \sim 7.8$ 分
* **核心机房加权分（权重 10%）**：
  * SJC（圣何塞）、LAX（洛杉矶）核心美西 POP 享受额外加分。

---

## 5. 输出契约与生态集成

1. **节点导出格式**：100% 保持与现有 OpenClash 订阅解析规范兼容：
   ```text
   <ip>:<port>#US-DaTree-<score>-<rank>-<tag> <speed>MB/s
   ```
2. **CI / CD 与多平台适配**：
   - 具备环境感知：非 Linux/OpenWrt 环境（如 Windows 单元测试）自动优雅降级为 Mock 模式；
   - 自动推仓与 OpenClash 热重载完全透明兼容，无需改动 LuCI 界面或 runner 调度逻辑。
