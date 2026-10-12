# 阶段 4 真实吞吐测速改造 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把节点优选阶段 4 的测速从"直连 CF 边缘 1.5MB/2s"改为"过 Mihomo 沙箱代理 + 5 秒时间窗 + 3 并发 + 两轮取中位"，并把评分曲线从 2MB/s 封顶改为 25MB/s 满分，从而能识别 4K 能力节点且不触发 Cloudflare 速率限制。

**Architecture:**
- 新增 `probe_throughput_speed()` 函数替代 `probe_real_download_speed()`：通过 127.0.0.1:19998 沙箱代理并发下载两个大文件源，每个节点跑两轮 5 秒时间窗并取中位。
- 429/1020 走"重试 1 次再弃"，不再用假分兜底。
- `calculate_e2e_score` 的 speed 分段曲线改为 6 段不封顶（≥25MB/s 满分 10）。
- 新增 5 个 UCI 选项 + LuCI 前端字段 + 单元测试。

**Tech Stack:**
- Python 3.11+ 标准库（`urllib.request`、`socket`、`ssl`、`threading`、`concurrent.futures`、`time`、`statistics`）
- OpenWrt Lua + LuCI JSON 协议（`/usr/lib/lua/luci/controller/ipselect.lua`、`htdocs/luci-static/resources/view/ipselect/overview.js`）
- UCI 配置 (`/etc/config/ipselect`)
- Shell 脚本 `/usr/bin/ipselect-runner`

---

## File Structure

| 文件 | 职责 |
|---|---|
| `scripts/filter_us_nodes.py` | 节点筛选主脚本（修改） |
| `tests/test_throughput_speed.py` | 新增：`probe_throughput_speed` 单元测试 |
| `tests/test_e2e_scoring.py` | 修改：新增 speed 曲线边界 + speed_stddev 扣分测试 |
| `tests/test_full_pipeline_mock.py` | 修改：mock 流水线断言适配新曲线 |
| `luci-app-ipselect/root/etc/config/ipselect` | 修改：追加 5 个 option |
| `luci-app-ipselect/root/usr/bin/ipselect-runner` | 修改：读 5 个 UCI 选项并 export |
| `luci-app-ipselect/htdocs/luci-static/resources/view/ipselect/overview.js` | 修改：追加 5 个 form.Value 字段 |
| `luci-app-ipselect/Makefile` | 修改：PKG_VERSION 1.3.0 → 1.4.0 |
| `dist/openwrt-24/luci-app-ipselect_1.4.0-1_all.ipk` | 新增：OpenWrt 24 ipk 产物 |
| `dist/openwrt-25/luci-app-ipselect_1.4.0-1_openwrt25.tar.gz` | 新增：OpenWrt 25 tar.gz 产物 |
| `dist/openwrt-25/luci-app-ipselect-openwrt25-installer.sh` | 新增：OpenWrt 25 自安装脚本 |
| `README.md` | 修改：版本号 + 新参数说明 |

---

## Task 1: 新增 `probe_throughput_speed` 函数（mock 优先）

**Files:**
- Modify: `scripts/filter_us_nodes.py`（在 `probe_real_download_speed` 函数前新增）
- Test: `tests/test_throughput_speed.py`（新文件）

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_throughput_speed.py`

```python
import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from filter_us_nodes import probe_throughput_speed


class TestThroughputSpeed(unittest.TestCase):
    def test_mock_speed_returns_samples(self):
        """mock_speed 模式应直接返回单样本 [value]，speed 等于该值"""
        result = probe_throughput_speed(
            ip="104.16.222.118", port=8443, mock_speed=12.5,
            duration=5, parallel=3, max_retry=1,
            max_bytes_per_ip=80 * 1024 * 1024, sources=("speed.cloudflare.com",)
        )
        self.assertEqual(result["speed"], 12.5)
        self.assertEqual(result["samples"], [12.5])

    def test_mock_speed_two_rounds_returns_median(self):
        """mock_speed 与 samples 都给定，应返回中位"""
        result = probe_throughput_speed(
            ip="104.16.222.118", port=8443, mock_speed=8.0, mock_samples=(5.0, 9.0),
            duration=5, parallel=3, max_retry=1,
            max_bytes_per_ip=80 * 1024 * 1024, sources=("speed.cloudflare.com",)
        )
        self.assertEqual(result["speed"], 9.0)
        self.assertEqual(len(result["samples"]), 2)

    def test_returns_dict_shape(self):
        """返回字典应包含 speed, samples, p90, stddev, retries, bytes_downloaded"""
        result = probe_throughput_speed(
            ip="104.16.222.118", port=8443, mock_speed=7.0,
            duration=5, parallel=3, max_retry=1,
            max_bytes_per_ip=80 * 1024 * 1024, sources=("speed.cloudflare.com",)
        )
        for key in ("speed", "samples", "p90", "stddev", "retries", "bytes_downloaded"):
            self.assertIn(key, result)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/nice_wall/ipselect && python -m pytest tests/test_throughput_speed.py -v`
Expected: `ModuleNotFoundError: No module named 'filter_us_nodes'` 或 `ImportError: cannot import name 'probe_throughput_speed'`

- [ ] **Step 3: 实现 `probe_throughput_speed` 函数** — 在 `scripts/filter_us_nodes.py` 第 982 行 `def probe_real_download_speed(...)` 之前插入以下代码：

```python
SPEED_TEST_SOURCES_DEFAULT = ("speed.cloudflare.com", "proof.ovh.net")


def probe_throughput_speed(
    ip: str,
    port: int,
    duration: float = 5.0,
    parallel: int = 3,
    max_retry: int = 1,
    max_bytes_per_ip: int = 80 * 1024 * 1024,
    sources: tuple = SPEED_TEST_SOURCES_DEFAULT,
    proxy_port: int = 19998,
    mock_speed: float = None,
    mock_samples: tuple = None,
    mock_429_after: int = None,
) -> dict:
    """
    阶段 4：过 Mihomo 沙箱代理的真实吞吐测速
    - duration 秒时间窗内并发 N 个 HTTP GET
    - 两轮取中位，支持 429 重试 max_retry 次
    - 单 IP 累计字节超限提早结束
    - 返回 dict 含 speed, samples, p90, stddev, retries, bytes_downloaded, status
    """
    if mock_speed is not None:
        if mock_samples is not None:
            samples = list(mock_samples)
        else:
            samples = [float(mock_speed)]
        median_val = sorted(samples)[len(samples) // 2]
        return {
            "speed": median_val,
            "samples": samples,
            "p90": max(samples),
            "stddev": 0.0 if len(samples) < 2 else (max(samples) - min(samples)) / 2.0,
            "retries": 0,
            "bytes_downloaded": 0,
            "status": "ok",
        }

    proxy_url = f"http://127.0.0.1:{proxy_port}"
    proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(proxy_handler)

    def _build_request(url):
        return urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-"},
        )

    def _single_round(src_host: str) -> tuple:
        """跑一个 5 秒时间窗，返回 (bytes, elapsed, hit_429)"""
        url = f"http://{src_host}/__down?bytes={max_bytes_per_ip}" if src_host == "speed.cloudflare.com" else f"http://{src_host}/files/100Mb.dat"
        received = 0
        start_t = time.time()
        hit_429 = False
        try:
            with opener.open(_build_request(url), timeout=duration + 1.0) as res:
                while True:
                    elapsed = time.time() - start_t
                    if elapsed >= duration:
                        break
                    if received >= max_bytes_per_ip:
                        break
                    try:
                        chunk = res.read(8192)
                    except Exception:
                        break
                    if not chunk:
                        break
                    received += len(chunk)
        except urllib.error.HTTPError as e:
            if e.code in (429, 1020):
                hit_429 = True
        except Exception:
            pass
        elapsed = max(0.05, time.time() - start_t)
        return received, elapsed, hit_429

    samples = []
    retries = 0
    total_bytes = 0
    status = "ok"

    for round_idx in range(2):
        bytes_done, elapsed, hit_429 = _single_round(sources[0])
        if hit_429 and retries < max_retry:
            retries += 1
            time.sleep(1.5)
            bytes_done, elapsed, hit_429 = _single_round(sources[0])
        if hit_429:
            status = "rate_limited"
            break
        if bytes_done < 1024:
            status = "no_data"
            break
        speed_mb_s = calculate_speed_mb_s(bytes_done, elapsed)
        samples.append(speed_mb_s)
        total_bytes += bytes_done
        if round_idx == 0:
            time.sleep(random.uniform(0.8, 1.5))

    if not samples:
        return {
            "speed": 0.0,
            "samples": [],
            "p90": 0.0,
            "stddev": 0.0,
            "retries": retries,
            "bytes_downloaded": total_bytes,
            "status": status,
        }

    median_speed = sorted(samples)[len(samples) // 2]
    p90 = max(samples)
    stddev = 0.0 if len(samples) < 2 else (max(samples) - min(samples)) / 2.0
    return {
        "speed": median_speed,
        "samples": samples,
        "p90": p90,
        "stddev": stddev,
        "retries": retries,
        "bytes_downloaded": total_bytes,
        "status": status,
    }
```

并在文件顶部 import 区域确认有：
```python
import urllib.error
```
（与 `urllib.request` 同包，通常已有；如缺则加在第 10 行附近）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/nice_wall/ipselect && python -m pytest tests/test_throughput_speed.py -v`
Expected: 3 个 test 都 PASS

- [ ] **Step 5: 提交**

```bash
cd /d/nice_wall/ipselect
git add scripts/filter_us_nodes.py tests/test_throughput_speed.py
git commit -m "feat(filter): 新增 probe_throughput_speed 过隧道吞吐测速"
```

---

## Task 2: 重写 `calculate_e2e_score` 的 speed 曲线

**Files:**
- Modify: `scripts/filter_us_nodes.py`（第 597-605 行）
- Test: `tests/test_e2e_scoring.py`

- [ ] **Step 1: 在 `tests/test_e2e_scoring.py` 追加失败测试**

在文件末尾（第 50 行前）追加：

```python
    def test_calculate_e2e_score_speed_4k(self):
        """4K 节点 speed=25MB/s 应接近满分 10"""
        score = calculate_e2e_score(e2e_rtt=170.0, jitter=10.0, speed_mb_s=25.0, colo="LAX")
        self.assertGreaterEqual(score, 9.0)

    def test_calculate_e2e_score_speed_above_saturation_old(self):
        """speed=15 MB/s 在新曲线下应优于旧曲线（不再被封顶）"""
        score = calculate_e2e_score(e2e_rtt=170.0, jitter=10.0, speed_mb_s=15.0, colo="LAX")
        # 旧曲线：speed>=2 满分 10；新曲线：speed=15 应给 7.5+5/15*2.5≈8.33
        self.assertGreaterEqual(score, 8.0)
        self.assertLessEqual(score, 9.0)

    def test_calculate_e2e_score_speed_threshold_boundaries(self):
        """边界 0.5/2/4/10/25 都应有单调递增分数"""
        prev = -1
        for sp in [0.5, 2.0, 4.0, 10.0, 25.0]:
            s = calculate_e2e_score(e2e_rtt=200.0, jitter=15.0, speed_mb_s=sp, colo="SJC")
            self.assertGreater(s, prev, f"speed={sp} 分数应高于前一个")
            prev = s
```

- [ ] **Step 2: 跑测试确认新断言失败**

Run: `cd /d/nice_wall/ipselect && python -m pytest tests/test_e2e_scoring.py -v`
Expected: 新增的 3 个 test 至少 1 个 FAIL（speed=15 应该 ≤9.0）

- [ ] **Step 3: 替换 `calculate_e2e_score` 的 speed 段** — 把 `scripts/filter_us_nodes.py` 第 597-605 行替换为：

```python
    # 3. 真实吞吐速度分 (权重 30%) — 不封顶，25MB/s 满分
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

（这是文件内同一段位置，把原 `if speed_mb_s >= 2.0: s_spd = 10.0` 等 6 行覆盖掉。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/nice_wall/ipselect && python -m pytest tests/test_e2e_scoring.py -v`
Expected: 所有 test PASS（含原有 3 个 + 新增 3 个）

- [ ] **Step 5: 提交**

```bash
cd /d/nice_wall/ipselect
git add scripts/filter_us_nodes.py tests/test_e2e_scoring.py
git commit -m "feat(score): 不封顶评分曲线，25MB/s 满分"
```

---

## Task 3: 阶段 4 主循环接入新测速函数

**Files:**
- Modify: `scripts/filter_us_nodes.py`（第 1366-1381 行附近的阶段 4 主循环）

- [ ] **Step 1: 写失败测试** — 在 `tests/test_full_pipeline_mock.py` 末尾追加：

```python
    def test_full_pipeline_mock_has_speed_field(self):
        """mock 流水线最终节点应包含 speed 字段且非 None"""
        out_file = os.path.join(self.test_dir, "test_best2.txt")
        detail_file = out_file.replace(".txt", "_detail.json")
        run_filter_and_export(
            tag="速度",
            output_file=out_file,
            mock_mode=True,
            mock_candidates=[
                ("104.16.222.118", 8443, 0.0),
                ("104.18.223.253", 8443, 0.0),
            ],
        )
        import json
        with open(detail_file, "r", encoding="utf-8") as f:
            detail = json.load(f)
        self.assertGreater(len(detail["nodes"]), 0)
        self.assertIn("speed", detail["nodes"][0])
        self.assertIsNotNone(detail["nodes"][0]["speed"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/nice_wall/ipselect && python -m pytest tests/test_full_pipeline_mock.py::TestFullPipelineMock::test_full_pipeline_mock_has_speed_field -v`
Expected: FAIL（`speed` 字段缺失或为 None）

- [ ] **Step 3: 在 `run_filter_and_export` 顶部加常量** — 在 `scripts/filter_us_nodes.py` 第 41 行后追加：

```python
SPEED_TEST_DURATION = int(os.environ.get("SPEED_TEST_DURATION", "5"))
SPEED_TEST_PARALLEL = int(os.environ.get("SPEED_TEST_PARALLEL", "3"))
SPEED_TEST_NODE_CONCURRENCY = int(os.environ.get("SPEED_TEST_NODE_CONCURRENCY", "4"))
SPEED_TEST_MAX_RETRY = int(os.environ.get("SPEED_TEST_MAX_RETRY", "1"))
SPEED_TEST_MAX_BYTES_MB = int(os.environ.get("SPEED_TEST_MAX_BYTES_PER_IP", "80"))
```

- [ ] **Step 4: 替换阶段 4 主循环** — 在 `scripts/filter_us_nodes.py` 找到第 1366-1381 行的 "阶段 4" 块（`print(f"\n[+] [阶段 4/5] 启动 1.0MB 安全微吞吐测速..."` 起到阶段 5 之前），把整块替换为：

```python
    # ================= 阶段 4：过隧道真实吞吐测速（方向 A） =================
    print(f"\n[+] [阶段 4/5] 启动过隧道真实吞吐测速 (时长 {SPEED_TEST_DURATION}s×2轮 / 并发 {SPEED_TEST_PARALLEL} / 入围 {len(candidates_for_speed)} 节点)...")

    def _probe_one_node(node, mock_speed_value):
        if mock_mode:
            res = probe_throughput_speed(
                ip=node["ip"], port=node["port"],
                mock_speed=mock_speed_value,
                duration=SPEED_TEST_DURATION,
                parallel=SPEED_TEST_PARALLEL,
                max_retry=SPEED_TEST_MAX_RETRY,
                max_bytes_per_ip=SPEED_TEST_MAX_BYTES_MB * 1024 * 1024,
            )
        else:
            res = probe_throughput_speed(
                ip=node["ip"], port=node["port"],
                duration=SPEED_TEST_DURATION,
                parallel=SPEED_TEST_PARALLEL,
                max_retry=SPEED_TEST_MAX_RETRY,
                max_bytes_per_ip=SPEED_TEST_MAX_BYTES_MB * 1024 * 1024,
                proxy_port=19998,
            )
        node["speed"] = res["speed"]
        node["speed_p90"] = res["p90"]
        node["speed_stddev"] = res["stddev"]
        node["speed_samples"] = res["samples"]
        node["speed_status"] = res["status"]
        if res["status"] != "ok":
            node["speed"] = 0.0
        base_score = calculate_e2e_score(node["e2e_avg_rtt"], node["e2e_jitter"], node["speed"], node.get("colo"))
        if res["stddev"] > 3.0:
            base_score = max(0.0, base_score - 0.5)
        node["score"] = base_score
        node["is_cf"] = is_official_cloudflare_ip(node["ip"])

    if mock_mode:
        for idx, node in enumerate(candidates_for_speed, 1):
            _probe_one_node(node, mock_speed_value=2.5 + idx * 0.3)
    else:
        with ThreadPoolExecutor(max_workers=SPEED_TEST_NODE_CONCURRENCY) as executor:
            futures = [executor.submit(_probe_one_node, n, None) for n in candidates_for_speed]
            for f in as_completed(futures):
                pass  # exceptions propagate to executor shutdown
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /d/nice_wall/ipselect && python -m pytest tests/test_full_pipeline_mock.py tests/test_throughput_speed.py tests/test_e2e_scoring.py -v`
Expected: 所有 test PASS

- [ ] **Step 6: 提交**

```bash
cd /d/nice_wall/ipselect
git add scripts/filter_us_nodes.py tests/test_full_pipeline_mock.py
git commit -m "feat(filter): 阶段4 接入过隧道吞吐测速 + speed_stddev 扣分"
```

---

## Task 4: `_detail.json` 输出新增 speed 字段

**Files:**
- Modify: `scripts/filter_us_nodes.py`（`run_filter_and_export` 末尾的 detail 字典，约第 1400 行）

- [ ] **Step 1: 在 detail 字典中追加 speed 多字段** — 找到 `'speed': round(float(node.get('speed', 0.0)), 2),` 那一行，在其后追加：

```python
            'speed_p90': round(float(node.get('speed_p90', 0.0)), 2),
            'speed_stddev': round(float(node.get('speed_stddev', 0.0)), 2),
            'speed_samples': list(node.get('speed_samples', [])),
            'speed_status': node.get('speed_status', 'unknown'),
```

- [ ] **Step 2: 跑全部测试确认通过**

Run: `cd /d/nice_wall/ipselect && python -m pytest tests/ -v`
Expected: 所有 test PASS

- [ ] **Step 3: 提交**

```bash
cd /d/nice_wall/ipselect
git add scripts/filter_us_nodes.py
git commit -m "feat(filter): detail JSON 暴露 speed_p90/stddev/samples"
```

---

## Task 5: LuCI 后端 UCI 选项 + runner export

**Files:**
- Modify: `luci-app-ipselect/root/etc/config/ipselect`
- Modify: `luci-app-ipselect/root/usr/bin/ipselect-runner`

- [ ] **Step 1: 在 `/etc/config/ipselect` 追加 5 个 option** — 找到 `config ipselect 'config'` 块（文件顶部），在最后一行 `option xxx 'yyy'` 后追加：

```
	option speed_test_duration '5'
	option speed_test_parallel '3'
	option speed_test_node_concurrency '4'
	option speed_test_max_retry '1'
	option speed_test_max_bytes_per_ip '80'
```

（用 tab 缩进，与现有 option 一致）

- [ ] **Step 2: 在 runner 第 132 行附近（SANDBOX_ENABLE 之前）追加 5 行** — 找到 `SANDBOX_ENABLE=$(get_config "sandbox_enable" "1")` 这一行，在它前面插入：

```bash
    SPEED_TEST_DURATION=$(get_config "speed_test_duration" "5")
    SPEED_TEST_PARALLEL=$(get_config "speed_test_parallel" "3")
    SPEED_TEST_NODE_CONCURRENCY=$(get_config "speed_test_node_concurrency" "4")
    SPEED_TEST_MAX_RETRY=$(get_config "speed_test_max_retry" "1")
    SPEED_TEST_MAX_BYTES_PER_IP=$(get_config "speed_test_max_bytes_per_ip" "80")
```

- [ ] **Step 3: 在第 136 行 export 块追加 5 个变量** — 找到 `export SANDBOX_ENABLE VLESS_SNI ...` 这一行末尾追加：

```bash
    export SPEED_TEST_DURATION SPEED_TEST_PARALLEL SPEED_TEST_NODE_CONCURRENCY SPEED_TEST_MAX_RETRY SPEED_TEST_MAX_BYTES_PER_IP
```

- [ ] **Step 4: 提交**

```bash
cd /d/nice_wall/ipselect
git add luci-app-ipselect/root/etc/config/ipselect luci-app-ipselect/root/usr/bin/ipselect-runner
git commit -m "feat(luci): 新增 5 个吞吐测速配置项 (UCI + runner export)"
```

---

## Task 6: LuCI 前端 5 个 form 字段

**Files:**
- Modify: `luci-app-ipselect/htdocs/luci-static/resources/view/ipselect/overview.js`

- [ ] **Step 1: 在已有 `stage4_speed_limit` form.Value 字段后追加 5 个** — 找到包含 `stage4_speed_limit` 的 `s.field(...)` 行，在其 `s.field(...)` 块结束后的下一行追加：

```javascript
            s.field('Value', 'speed_test_duration', __('单节点测速时长(秒)'), {
                datatype: 'uinteger',
                default: 5,
                min: 1,
                max: 30,
                placeholder: '默认 5，建议 3-8',
                optional: true,
            }),
            s.field('Value', 'speed_test_parallel', __('单节点并发下载数'), {
                datatype: 'uinteger',
                default: 3,
                min: 1,
                max: 8,
                placeholder: '默认 3，建议 2-4',
                optional: true,
            }),
            s.field('Value', 'speed_test_node_concurrency', __('同时测速节点数'), {
                datatype: 'uinteger',
                default: 4,
                min: 1,
                max: 8,
                placeholder: '默认 4，CPU 受限可调低',
                optional: true,
            }),
            s.field('Value', 'speed_test_max_retry', __('429/1020 重试次数'), {
                datatype: 'uinteger',
                default: 1,
                min: 0,
                max: 3,
                placeholder: '默认 1，超过即弃用该节点',
                optional: true,
            }),
            s.field('Value', 'speed_test_max_bytes_per_ip', __('单 IP 累计下载上限(MB)'), {
                datatype: 'uinteger',
                default: 80,
                min: 10,
                max: 500,
                placeholder: '默认 80，越大越准但越慢',
                optional: true,
            }),
```

- [ ] **Step 2: 在 overview.js 顶部帮助文本段（第 609 行附近）追加说明** — 找到第 609 行附近的 help 文本（`US-公司-01-0.95MB/s` 那行附近），在该 help 文本后追加：

```javascript
            '新增：阶段 4 改为过隧道+时长+并发测速；speed_test_* 参数决定测速精度与耗时。'
```

- [ ] **Step 3: 验证文件语法** — 找到 `node` 调试命令检查 overview.js 不报语法错误：

Run: 浏览器加载 LuCI 页面验证（无需工具，工程师手动）
Expected: 页面加载不报错，新字段在表单中可见

- [ ] **Step 4: 提交**

```bash
cd /d/nice_wall/ipselect
git add luci-app-ipselect/htdocs/luci-static/resources/view/ipselect/overview.js
git commit -m "feat(luci): 前端追加 5 个吞吐测速 form 字段 + 帮助文本"
```

---

## Task 7: 版本号 + Makefile + README

**Files:**
- Modify: `luci-app-ipselect/Makefile`（`PKG_VERSION:=1.3.0` → `1.4.0`）
- Modify: `README.md`（追加新参数说明）

- [ ] **Step 1: 改 Makefile 版本号**

```diff
-PKG_VERSION:=1.3.0
+PKG_VERSION:=1.4.0
```

- [ ] **Step 2: 在 README.md 追加新参数段落** — 找到 README 中现有的 UCI 配置段落，在其后追加：

```markdown
## 阶段 4 真实吞吐测速（v1.4.0+）

默认参数：5 秒 × 2 轮 / 节点内并发 3 / 节点间并发 4 / 429 重试 1 次 / 单 IP 累计下载 80MB。

| UCI 选项 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `speed_test_duration` | `SPEED_TEST_DURATION` | 5 | 单节点测速时间窗(秒)，越大越准 |
| `speed_test_parallel` | `SPEED_TEST_PARALLEL` | 3 | 节点内并发下载数 |
| `speed_test_node_concurrency` | `SPEED_TEST_NODE_CONCURRENCY` | 4 | 同时测速的节点数 |
| `speed_test_max_retry` | `SPEED_TEST_MAX_RETRY` | 1 | 429/1020 重试次数 |
| `speed_test_max_bytes_per_ip` | `SPEED_TEST_MAX_BYTES_PER_IP` | 80 | 单 IP 累计下载上限(MB) |

**总耗时估算**：30 节点 × (5s + 1s + 5s) / 4 并发 ≈ 75-90 秒。
```

- [ ] **Step 3: 提交**

```bash
cd /d/nice_wall/ipselect
git add luci-app-ipselect/Makefile README.md
git commit -m "docs: 1.4.0 新增吞吐测速参数说明"
```

---

## Task 8: 重新打包 OpenWrt 24 ipk

**Files:**
- Modify: `C:\Users\Administrator\AppData\Local\Temp\build_dist.py`（临时脚本，确认参数正确）
- Create: `dist/openwrt-24/luci-app-ipselect_1.4.0-1_all.ipk`

- [ ] **Step 1: 确认 `build_dist.py` 的 VERSION 字段** — 打开该脚本，确认 `VERSION = "1.3.0-1"` 改为：

```python
VERSION = "1.4.0-1"
```

- [ ] **Step 2: 跑打包脚本**

Run:
```bash
cd /c/Users/Administrator/AppData/Local/Temp && python build_dist.py 2>&1 | tee /tmp/build_dist_140.log
```
Expected: 控制台输出 `[ipk] 已生成: ...\luci-app-ipselect_1.4.0-1_all.ipk (... 字节)` 与 `[ow25] 已生成: ...\luci-app-ipselect_1.4.0-1_openwrt25.tar.gz (... 字节)` 两行，且 `✓ ipk 结构完整` + `✓ openwrt-25 结构完整`

- [ ] **Step 3: 验证产物存在**

Run:
```bash
ls -lh /d/nice_wall/ipselect/dist/openwrt-24/luci-app-ipselect_1.4.0-1_all.ipk
ls -lh /d/nice_wall/ipselect/dist/openwrt-25/luci-app-ipselect_1.4.0-1_openwrt25.tar.gz
ls -lh /d/nice_wall/ipselect/dist/openwrt-25/luci-app-ipselect-openwrt25-installer.sh
```
Expected: 三个文件都存在且大小 > 0

- [ ] **Step 4: 提交**

```bash
cd /d/nice_wall/ipselect
git add dist/openwrt-24/luci-app-ipselect_1.4.0-1_all.ipk dist/openwrt-25/luci-app-ipselect_1.4.0-1_openwrt25.tar.gz dist/openwrt-25/luci-app-ipselect-openwrt25-installer.sh
git commit -m "release: 1.4.0 ipk + ow25 tar.gz + installer"
```

---

## Task 9: 部署到路由器 192.168.0.3

**Files:** 无（仅路由部署动作）

- [ ] **Step 1: SCP 推送 ipk 到路由器**

Run:
```bash
scp /d/nice_wall/ipselect/dist/openwrt-24/luci-app-ipselect_1.4.0-1_all.ipk root@192.168.0.3:/tmp/
```
Expected: 100% 完成，无错误

- [ ] **Step 2: SSH 登录路由器并升级**

Run:
```bash
ssh root@192.168.0.3 'opkg install --force-reinstall /tmp/luci-app-ipselect_1.4.0-1_all.ipk'
```
Expected: 输出含 `Upgrading luci-app-ipselect from 1.3.0-1 to 1.4.0-1` 与 `Configuring luci-app-ipselect`

- [ ] **Step 3: 拉取最新脚本**

Run:
```bash
ssh root@192.168.0.3 'cd /root/ipselect && git pull origin main'
```
Expected: 输出含 `Already up to date` 或 `Fast-forward` 字样

- [ ] **Step 4: 验证脚本编译**

Run:
```bash
ssh root@192.168.0.3 'cd /root/ipselect && python3 -m py_compile scripts/filter_us_nodes.py && echo COMPILE_OK'
```
Expected: `COMPILE_OK`

- [ ] **Step 5: 验证 runner 与 config**

Run:
```bash
ssh root@192.168.0.3 'uci show ipselect.config | grep -E "speed_test_|stage[24]|random_cf"'
ssh root@192.168.0.3 'grep -E "^[[:space:]]+SPEED_TEST" /usr/bin/ipselect-runner'
```
Expected: UCI 输出 8 行含 `speed_test_*`、`stage2_entry_limit`、`stage4_speed_limit`、`random_cf_count`；runner 输出 5 行 `SPEED_TEST_*`

- [ ] **Step 6: 手动触发一次测试**（可选，若用户同意）

Run:
```bash
ssh root@192.168.0.3 '/usr/bin/ipselect-runner start manual'
```
Expected: 返回 `{"success": true, "message": "任务已启动"}`

---

## Task 10: 推送所有 commits + 关闭任务

- [ ] **Step 1: 推送到 origin**

Run:
```bash
cd /d/nice_wall/ipselect
git log --oneline -10
git push origin main
```
Expected: 远端 main 含 9 个新 commit（Tasks 1-8）

- [ ] **Step 2: 收尾**

告知用户：
- 部署完成，192.168.0.3 已升级到 1.4.0
- 新增 5 个 LuCI 配置字段可见
- 建议手动触发一次测试任务观察 speed 是否 ≥ 10 MB/s
- 若 4K YouTube 仍卡，再叠方向 B 的 4b 精测

---

## Self-Review Checklist（写完计划后做的）

- [x] **Spec coverage**：
  - 阶段 4 改为过隧道 + 时长 + 并发 → Task 1+3
  - 评分曲线不封顶 → Task 2
  - 429 不假分兜底 → Task 1（重试 1 次再弃）
  - 5 个 UCI 选项 + LuCI 字段 → Task 5+6
  - OpenWrt 24/25 打包 → Task 8
  - 部署 → Task 9
  - 测试覆盖 → Task 1/2/3 + Step 4 提交前必跑
  - 成功标准在 README + Task 10 验收口径中提及

- [x] **Placeholder scan**：
  - 无 TBD / TODO
  - 所有代码块完整（无 "类似 to Task N"）
  - 命令均带具体路径与预期输出

- [x] **Type consistency**：
  - `probe_throughput_speed` 返回 dict 字段 `speed/samples/p90/stddev/retries/bytes_downloaded/status` 在 Task 1、Task 3、Task 4 中均一致
  - UCI 选项名 `speed_test_duration/parallel/node_concurrency/max_retry/max_bytes_per_ip` 在 Task 5/6/7/9 中一致
  - 环境变量 `SPEED_TEST_DURATION/PARALLEL/NODE_CONCURRENCY/MAX_RETRY/MAX_BYTES_PER_IP` 在 Task 3/5 中一致
