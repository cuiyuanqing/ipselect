# 阶段 4 真实吞吐测速改造 — 设计文档

- 日期：2026-10-11
- 作者：Claude (Opus 4.8)
- 状态：待用户批准
- 目标代号：`throughput-test-redesign`

---

## 一、背景与目标

### 现状
- `filter_us_nodes.py:982` `probe_real_download_speed` 直接 TCP 连到 CF 边缘测 1.5MB / 2s 截断，**绕开 VLESS/edgetunnel worker**。
- 评分曲线 `calculate_e2e_score:598` 在 `speed >= 2.0 MB/s` 即封顶 10 分；4K 能力节点无法被识别。
- `probe_real_download_speed:1030`（429 命中）与 `:1049`（异常兜底）会"奖励坏节点"。
- 已写的过隧道测速函数 `probe_e2e_download_speed:534` **没接入最终 score**。

### 目标
1. 阶段 4 改为**过隧道 + 时长驱动 + 并发**的真实吞吐测速，反映 YouTube/Google 实际路径。
2. 评分曲线改为**不封顶**，≥25 MB/s 满分，鼓励高位能力被识别。
3. 429/1020 不再用假分兜底，直接弃用该节点（或重试 1 次再弃）。
4. 保持 5 层漏斗结构和现有阶段 1-3 行为不变。
5. LuCI 暴露新参数（并发、持续时长、测速源、最大重试），默认值按推荐设置。

### 非目标
- 不重构阶段 1-3 的 TLS/colo 检测。
- 不引入机器学习模型（方向 C 留作未来）。
- 不做 YouTube 真实拉流（方向 B 的 4b 暂不做）。

---

## 二、关键决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 测速路径 | **过本地 Mihomo 沙箱代理 127.0.0.1:19998** | 匹配实际 YouTube 路径，过 VLESS + worker |
| 测速协议 | HTTP/1.1 GET Range | 简单可控，Range 不会因大文件撑爆内存 |
| 单次时长 | **5 秒时间窗**（可配 3/5/8） | 跑出 TCP 慢启动、进入稳态；总耗时可控 |
| 并发数 | **3 并发**（可配 2/3/4） | 贴 YouTube 实际并发；不触发 worker 反压 |
| 测速源 | **`speed.cloudflare.com/__down?bytes=` + `proof.ovh.net/files/100Mb.dat` 二选一** | 两源轮换避单源触发限速；前者 CF 自身、后者非 CF 兜底 |
| 429 行为 | **重试 1 次再弃**（可配 0/1/2） | 偶发抖动容错；仍失败即弃，**不写 fallback 假分** |
| 单 IP 累计下载上限 | **80 MB**（可配） | 远低于任何 edge 反爬阈值；30 节点 × 80 MB = 2.4 GB 总开销仍可控 |
| 评分 speed 曲线 | 阈值化不封顶 | ≥25 MB/s=10、10-25 线性、2-10 平缓、<2 重扣 |
| 引入 speed 抖动 | 作为 e2e_jitter 旁路扣分 | 用两次测速的标准差，>3 MB/s 抖动扣 0.5 分 |
| 数据结构 | 在节点字典新增 `speed_samples`（样本列表）、`speed_p90`、`speed_stddev` | 支持抖动分析；落库到 `_detail.json` |

---

## 三、架构与数据流

### 阶段 4 新流程（替换旧 `probe_real_download_speed` 调用链）

```
阶段 3 输出的 e2e_results（已通过 Google/YT/ip.sb 闸门）
  │
  ▼
按 e2e_avg_rtt + e2e_jitter 排序，取前 STAGE4_SPEED_LIMIT 个
  │
  ▼
逐节点: open Mihomo proxy → 并发 N 个 HTTP GET Range(s) → 5s 时间窗
  │
  ├─► 累积总字节 + 单源累计字节
  │   ├─ 累计 > 单 IP 上限 → 立即结束该节点，记录已下载字节后算吞吐
  │   └─ 单源累计 > 上限 → 切换备用源
  │
  ├─► 收到 HTTP 状态码
  │   ├─ 200/206 → 累计字节
  │   ├─ 429/1020 → 标记限流，retry_count++
  │   │   └─ retry_count < max_retry → 休息 1.5s 重试
  │   │   └─ 否则 → 节点判弃（speed=None）
  │   └─ 其它非 2xx → 节点判弃
  │
  ▼
5s 时间窗到（或早退条件触发）
  │
  ├─► 计算 speed_mb_s = (总字节 / 1024^2) / 实际时长
  │
  ├─► 对该节点第二轮 5s 测速（如果时间允许 + 未超 IP 累计上限）
  │   ├─ 间隔 0.8-1.5s 错峰
  │   └─ 得到 speed_samples = [s1, s2]
  │
  ├─► speed = median(speed_samples)
  │   speed_p90 = max(speed_samples)
  │   speed_stddev = stddev(speed_samples)
  │
  ▼
入 score 公式：calculate_e2e_score(rtt, jitter, speed, colo)
   ├─ 用新曲线
   └─ 若 speed_stddev > 3 → 在 score 上扣 0.5
```

### 并发与资源
- Mihomo 沙箱保留阶段 3 已起的实例（API 19098、proxy 19998）。
- 每节点一个 `ThreadPoolExecutor(max_workers=3)`（节点间并行 + 节点内 3 并发）。
- 节点间最大并发 = 4（限 worker 数量，默认 4 个节点同时测，配置可调 `SPEED_TEST_NODE_CONCURRENCY`）。
- 总耗时估算：30 节点 × (5s + 1s 错峰 + 5s 第二轮) / 4 并发 ≈ **75-90 秒**（默认参数下）。

### 错误处理
| 错误 | 处理 |
|---|---|
| Mihomo proxy 临时不可用 | 该节点失败一次，重试 1 次，仍失败判弃 |
| `1020 / 429 Too Many Requests` | 立即暂停该源 30s，累计 retry_count，超出判弃 |
| 测试主机 DNS 解析失败 | 切备用源；两源都失败判弃 |
| 单 IP 累计下载超限 | 提早结束该节点测速，保留已测数据算吞吐 |
| 阶段 3 沙箱未启动 | 阶段 4 退化为原 `probe_real_download_speed`（保留兼容），日志标 WARN |

---

## 四、配置项（默认 + 环境变量 + UCI）

### 环境变量（脚本读取）
- `SPEED_TEST_DURATION`：单节点单轮时长（秒），默认 `5`
- `SPEED_TEST_PARALLEL`：节点内并发数，默认 `3`
- `SPEED_TEST_NODE_CONCURRENCY`：节点间并发数，默认 `4`
- `SPEED_TEST_MAX_RETRY`：429/1020 单节点重试次数，默认 `1`
- `SPEED_TEST_MAX_BYTES_PER_IP`：单 IP 累计下载字节上限，默认 `80 * 1024 * 1024` (80 MB)
- `SPEED_TEST_SOURCES`：测速源列表，默认 `"speed.cloudflare.com,proof.ovh.net"`

### UCI（`/etc/config/ipselect`）
```uci
config ipselect 'config'
    option speed_test_duration '5'
    option speed_test_parallel '3'
    option speed_test_node_concurrency '4'
    option speed_test_max_retry '1'
    option speed_test_max_bytes_per_ip '80'
```

### LuCI 前端
- 在 `htdocs/luci-static/resources/view/ipselect/overview.js` 的现有 form 段后追加 5 个 `Value` 字段。
- 帮助文本说明"增大并发/时长会更准但更慢"。

---

## 五、新评分曲线（`calculate_e2e_score` 替换 speed 段）

```python
# speed 段（旧）→ （新）
if speed_mb_s >= 25.0:
    s_spd = 10.0
elif speed_mb_s >= 10.0:
    s_spd = 7.5 + (speed_mb_s - 10.0) / 15.0 * 2.5
elif speed_mb_s >= 4.0:
    s_spd = 5.0 + (speed_mb_s - 4.0) / 6.0 * 2.5
elif speed_mb_s >= 2.0:
    s_spd = 3.0 + (speed_mb_s - 2.0) / 2.0 * 2.0
elif speed_mb_s >= 0.5:
    s_spd = 1.0 + (speed_mb_s - 0.5) / 1.5 * 2.0
else:
    s_spd = max(0.5, speed_mb_s * 2.0)
```

权重保持 `S = 0.35·S_rtt + 0.25·S_jitter + 0.30·S_speed + 0.10·S_colo`。
在 sort key 中追加对 `speed_stddev > 3` 的扣分（应用层扣，不改主分）。

---

## 六、文件改动清单

| 文件 | 改动 |
|---|---|
| `scripts/filter_us_nodes.py` | 新增 `probe_throughput_speed(node, ...)`；重写 `calculate_e2e_score` 的 speed 段；阶段 4 主循环改调用 `probe_throughput_speed`；加新环境变量常量；新增 speed_stddev 字段；`_detail.json` 输出多 speed 字段 |
| `luci-app-ipselect/root/etc/config/ipselect` | 追加 5 个 `option` |
| `luci-app-ipselect/root/usr/bin/ipselect-runner` | 读 5 个 UCI 选项并 export |
| `luci-app-ipselect/htdocs/luci-static/resources/view/ipselect/overview.js` | 追加 5 个 form.Value 字段 + 帮助文本 |
| `tests/test_e2e_scoring.py` | 追加测试：speed=0/1/5/15/30 的分数预期；speed_stddev 扣分测试 |
| `tests/test_throughput_speed.py`（新文件） | mock 模式下：模拟 429 触发重试→弃用；模拟 byte 截断；模拟两轮测速取中位 |
| `tests/test_full_pipeline_mock.py` | 调整 speed 期望值到新曲线下的合理范围 |
| 文档 | `README.md` 更新版本号到 1.4.0 + 新参数说明 |
| 打包 | OpenWrt 24 ipk + OpenWrt 25 tar.gz/installer，PKG_VERSION 1.4.0 |

---

## 七、测试策略

### 单元
- `test_throughput_speed.py`（mock）：覆盖：
  - 正常两轮测速取中位
  - 第一轮 OK 第二轮 429 仍能算一次（应判弃）
  - 累计字节超限提早结束
  - 重试成功后正常出分
- `test_e2e_scoring.py`：speed 阈值边界 0.5/2/4/10/25/30。

### 集成
- `test_full_pipeline_mock.py` 端到端 mock：固定 100 候选 → 校验最终 15 个节点的 speed 字段在新曲线下不为 0。

### 手工 / 路由器
- 部署到 `192.168.0.3` 后跑一次完整任务，对照：
  - 输出节点中是否有 `speed >= 10 MB/s` 的；
  - 总耗时是否在 75-120s 区间；
  - 跑完后 `/etc/ipselect/logs/` 历史记录是否含合理 `speed` 数值。
- 不在测试期触发 429 是关键成功指标。

### 回滚
- 保留旧 `probe_real_download_speed` 函数体不动，加注释 `# DEPRECATED: ...`，仅作 fallback 路径使用。
- UCI 新选项缺失时退回旧默认值（环境变量兜底）。
- 若阶段 4 异常，catch 后 fallback 到旧函数 + WARN 日志。

---

## 八、风险与缓解

| 风险 | 缓解 |
|---|---|
| 5s × 30 节点耗时长，被用户感觉"卡" | LuCI 暴露参数；提供默认 3/5 推荐；首页给出"约 90 秒"提示 |
| 仍触 429 | 单 IP 累计字节上限 + 重试 1 次 + 源轮换；触发即弃节点 |
| Mihomo 沙箱稳定性差 | 失败 1 次重试；连续 2 个节点失败立即 abort 阶段 4 并回退旧函数 |
| CPU 打满 | 节点内并发 3 + 节点间并发 4；总 12 线程可控 |
| 4K 仍卡 | 数据沉淀后再叠方向 B 4b 精测 |

---

## 九、成功标准

- 默认参数下，30 节点阶段 4 耗时 ≤ 120 秒。
- 输出节点至少有 3 个 speed ≥ 10 MB/s（4K 能力门槛）。
- 24 小时不触发 Cloudflare 1020/429 任何一次（通过日志确认）。
- LuCI 后台可见 5 个新参数并能修改生效。
- 单测 100% 通过。

---

## 十、变更后用户视角

- 节点名称格式保持：`US-家庭-01-X.XXMB/s`（数字本身变大）。
- 任务总耗时约 3-5 分钟（原 ~1-2 分钟），可在 LuCI 调短（duration=3）提速。
- 4K YouTube 不再卡顿（成功标准验证）。
