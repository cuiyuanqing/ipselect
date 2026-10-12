# 阶段 3 真实质量门 v2 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `evaluate_e2e_candidate` 从"单次 204 快照"升级为"60s 真实页面密集采样 + Google+YouTube 同步探测"，彻底消除"脚本通过但客户端打不开"的判定失真。

**Architecture:** 抽出独立的 `scripts/stage3_quality_gate.py` 模块承载 60s/30s 区间质量门逻辑，保留 Mihomo 沙箱与 VLESS 模板不变，重构 `evaluate_e2e_candidate` 为 `evaluate_node_quality_v2` 调用新模块；新增 10 分钟超时保护 + 8-11 节点降级 1 + 4-7 节点降级 2 + <4 节点降级 3；best_us_detail.json 扩展质量门版本字段。

**Tech Stack:** Python 3.9+, Mihomo Meta sandbox (19098/19998), urllib.request, socket.AF_INET, time.sleep, threading.

**Spec:** `docs/superpowers/specs/2026-10-12-stage3-real-quality-gate-v2-design.md`

## Global Constraints

- **真实页面优先**：必须使用 `https://www.google.com/` 和 `https://www.youtube.com/` 真实主页探测，**不再**依赖 `generate_204` 探针（仅降级 2 兜底时使用）。
- **多模式 title 匹配**：Google `<title>` 接受 `Google` / `Google Search` / `谷歌搜索` / 包含 `Google` 或 `谷歌` 字样；YouTube `<title>` 接受 `YouTube` / `YouTube - Home` / 包含 `YouTube` 字样。
- **60s 区间参数**：`PROBE_INTERVAL_SEC=5` / `PROBE_TOTAL_ROUNDS=12` / `PROBE_PASS_THRESHOLD=11`。
- **30s 降级区间**：`PROBE_INTERVAL_SEC=3` / `PROBE_TOTAL_ROUNDS=10` / `PROBE_PASS_THRESHOLD=9`。
- **v4 强制**：阶段 3 探测入口 monkey-patch `socket.getaddrinfo` 过滤到 `AF_INET`，避免 v6 路径干扰 v4 客户端。
- **超时保护**：阶段 3 整体超过 600 秒主动中断，按"已通过节点数"路由到对应降级。
- **降级触发**：60s 跑完剩 ≥15 → 直入阶段 4；12-14 → 全入；8-11 → 降级 1；4-7 → 降级 2；<4 → 降级 3（STATUS=no_quality 不输出）。
- **节点行格式不变**：`IP:PORT#US-公司-RANK-SPEEDMB/s` 仍为最终输出格式。
- **推送链路不变**：复用 commit f22ce74 + d0a89a7 强化版 runner，git push 必成功。
- **单次失败重试 1 次**：解决瞬时抖动，1s 后重试，2 次仍失败记 1 次失败轮次。
- **全局代码质量规范**：本项目全局规则要求"不要做任何测试"。所有任务步骤只写实现 + 提交，不写 "Run test to verify" 步骤。实施者在 IDE 集成调试中按需手动验证。

## Review Focus

1. **真实页面判定有效性**：必须确保拉到的 HTML 包含 `<title>` 才算过；302 redirect、登录页、challenge 页都应失败。
2. **60s 区间准确性**：5s 间隔必须精准（不要 sleep 过短被算入失败轮次，也不要过长把 12 探拖成 6 探）。
3. **v4 强制范围**：只影响阶段 3 探测；阶段 4 测速源访问需要 v4/v6 自适应，不能影响。
4. **降级链路完整**：8-11 触发降级 1 时，30s 重测必须独立线程池，不能复用 60s 区间结果。
5. **超时保护精度**：10 分钟计时从阶段 3 启动开始算，不是从脚本启动算。
6. **best_us_detail.json 兼容性**：旧字段保留，新增 `quality_gate_version` / `real_page_pass_rate` / `real_page_failed_rounds` / `stability_window_sec`。
7. **推送链路兼容**：commit 必须能让远端 + 软路由工作区干净 pull，软路由 /usr/bin/ipselect-runner 已是 d0a89a7 强化版会自动拉新。

---

### Task 1: 抽出 stage3_quality_gate 模块骨架

**Files:**
- Create: `scripts/stage3_quality_gate.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class ProbeResult:
      ok: bool
      html_len: int = 0
      title_matched: str = ""
      error: str = ""
      attempt: int = 0

  @dataclass
  class StabilityResult:
      proxy_name: str
      ip: str
      port: int
      passed: int
      total: int
      pass_rate: float
      failed_rounds: list
      is_stable: bool
      window_sec: int
      duration_ms: int

  PROBE_INTERVAL_SEC = 5
  PROBE_TOTAL_ROUNDS = 12
  PROBE_PASS_THRESHOLD = 11
  MAX_BYTES_PER_PROBE = 262144  # 256KB
  PROBE_TIMEOUT_SEC = 5.0
  GOOGLE_TITLE_PATTERNS = [...]
  YOUTUBE_TITLE_PATTERNS = [...]
  ```

- [ ] **Step 1: 创建 `scripts/stage3_quality_gate.py` 骨架**

完整内容：

```python
"""
阶段 3 真实质量门 v2 — 60s 连续区间真实页面密集采样引擎

替代原 evaluate_e2e_candidate 的 204 探测，从"单次快照"升级为"60s 区间 + 真实页面",
彻底解决"脚本判通过但客户端实际打不开"的判定失真问题。

设计文档: docs/superpowers/specs/2026-10-12-stage3-real-quality-gate-v2-design.md
"""
from __future__ import annotations

import re
import socket
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ==================== 探测参数 ====================
PROBE_INTERVAL_SEC = 5            # 60s 区间采样间隔
PROBE_TOTAL_ROUNDS = 12           # 60s / 5s = 12 探
PROBE_PASS_THRESHOLD = 11         # 12 次中至少 11 次通过
MAX_BYTES_PER_PROBE = 262144      # 256KB
PROBE_TIMEOUT_SEC = 5.0           # 单次 HTTP 超时
RETRY_BACKOFF_SEC = 1.0           # 失败重试前等待

# 30s 降级区间参数
DEGRADED_INTERVAL_SEC = 3
DEGRADED_TOTAL_ROUNDS = 10
DEGRADED_PASS_THRESHOLD = 9

# 真实页面 URL
GOOGLE_HOMEPAGE_URL = "https://www.google.com/"
YOUTUBE_HOMEPAGE_URL = "https://www.youtube.com/"

# 多模式 title 匹配 (任一命中即过)
GOOGLE_TITLE_PATTERNS = [
    r"<title>\s*Google\s*</title>",
    r"<title>\s*Google\s+Search\s*</title>",
    r"<title>\s*谷歌搜索\s*</title>",
    r"<title>[^<]*Google[^<]*</title>",
    r"<title>[^<]*谷歌[^<]*</title>",
]
YOUTUBE_TITLE_PATTERNS = [
    r"<title>\s*YouTube\s*</title>",
    r"<title>\s*YouTube\s*-\s*Home\s*</title>",
    r"<title>[^<]*YouTube[^<]*</title>",
]


@dataclass
class ProbeResult:
    """单次真实页面探测结果"""
    ok: bool
    html_len: int = 0
    title_matched: str = ""
    error: str = ""
    attempt: int = 0
    duration_ms: int = 0


@dataclass
class StabilityResult:
    """60s/30s 区间稳定性结果"""
    proxy_name: str
    ip: str
    port: int
    passed: int
    total: int
    pass_rate: float
    failed_rounds: list = field(default_factory=list)
    is_stable: bool = False
    window_sec: int = 60
    duration_ms: int = 0
    google_pass_rate: float = 0.0
    youtube_pass_rate: float = 0.0


# ==================== v4 强制上下文 ====================
class IPv4OnlyContext:
    """临时把 socket.getaddrinfo 锁到 AF_INET,避免 v6 路径干扰 v4 客户端判定"""
    _original = None

    def __enter__(self):
        if IPv4OnlyContext._original is None:
            IPv4OnlyContext._original = socket.getaddrinfo
        def v4_only(host, port, *args, **kwargs):
            results = IPv4OnlyContext._original(host, port, *args, **kwargs)
            return [ai for ai in results if ai[0] == socket.AF_INET]
        socket.getaddrinfo = v4_only
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        socket.getaddrinfo = IPv4OnlyContext._original
        IPv4OnlyContext._original = None
        return False
```

- [ ] **Step 2: 提交骨架**

```bash
cd D:/nice_wall/ipselect
git add scripts/stage3_quality_gate.py
git -c user.email=ipselect@local -c user.name=ipselect commit -m "feat(stage3): 抽出真实质量门 v2 模块骨架 + 数据类 + v4 强制上下文"
```

---

### Task 2: 实现真实页面探测 (Google + YouTube)

**Files:**
- Modify: `scripts/stage3_quality_gate.py` (新增 `probe_real_google_page` + `probe_real_youtube_page` + `fetch_homepage_html`)

**Interfaces:**
- Produces:
  ```python
  def fetch_homepage_html(url: str, api_port: int = 19098, max_bytes: int = MAX_BYTES_PER_PROBE,
                          timeout_sec: float = PROBE_TIMEOUT_SEC, proxy_name: str = None) -> Tuple[Optional[bytes], str]:
      """通过沙箱代理拉取首 N 字节 HTML, 失败返回 (None, error_msg)"""

  def probe_real_google_page(proxy_name: str, ip: str, port: int,
                              api_port: int = 19098) -> ProbeResult: ...
  def probe_real_youtube_page(proxy_name: str, ip: str, port: int,
                               api_port: int = 19098) -> ProbeResult: ...
  ```

- [ ] **Step 1: 在 `scripts/stage3_quality_gate.py` 末尾追加**

```python
# ==================== 真实页面探测 ====================
def _build_proxy_opener(api_port: int):
    """通过 Mihomo 沙箱 (SOCKS5 不可用,这里走 HTTP 代理端口 19998)"""
    proxy_url = f"http://127.0.0.1:{api_port + 90}"  # 19998 = 19098 + 90
    proxy_handler = urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})
    return urllib.request.build_opener(proxy_handler)


def fetch_homepage_html(url: str, api_port: int = 19098, max_bytes: int = MAX_BYTES_PER_PROBE,
                         timeout_sec: float = PROBE_TIMEOUT_SEC) -> Tuple[Optional[bytes], str]:
    """
    通过沙箱代理拉取首 N 字节 HTML
    返回 (html_bytes_or_None, error_message)
    """
    proxy_url = f"http://127.0.0.1:{api_port + 90}"
    proxy_handler = urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8',
    })
    try:
        with opener.open(req, timeout=timeout_sec + 0.5) as res:
            data = res.read(max_bytes)
            return data, ""
    except urllib.error.HTTPError as e:
        return None, f"HTTPError {e.code}"
    except urllib.error.URLError as e:
        return None, f"URLError {e.reason}"
    except (socket.timeout, TimeoutError):
        return None, "timeout"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _match_title(html: bytes, patterns: List[str]) -> str:
    """在 HTML 中找首个匹配的 title 字符串, 找到返回该 pattern, 没找到返回空串"""
    if not html:
        return ""
    try:
        text = html.decode('utf-8', errors='ignore')
    except Exception:
        return ""
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return ""


def probe_real_google_page(proxy_name: str, ip: str, port: int,
                            api_port: int = 19098) -> ProbeResult:
    """单次 google.com 主页真实页面探测, 失败重试 1 次"""
    start = time.time()
    last_err = ""
    for attempt in (0, 1):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF_SEC)
        html, err = fetch_homepage_html(GOOGLE_HOMEPAGE_URL, api_port=api_port)
        duration_ms = int((time.time() - start) * 1000)
        if html is not None:
            title = _match_title(html, GOOGLE_TITLE_PATTERNS)
            if title:
                return ProbeResult(ok=True, html_len=len(html), title_matched=title,
                                   attempt=attempt, duration_ms=duration_ms)
            last_err = "title_not_matched"
        else:
            last_err = err
    return ProbeResult(ok=False, error=last_err, attempt=1, duration_ms=duration_ms)


def probe_real_youtube_page(proxy_name: str, ip: str, port: int,
                             api_port: int = 19098) -> ProbeResult:
    """单次 youtube.com 主页真实页面探测, 失败重试 1 次"""
    start = time.time()
    last_err = ""
    duration_ms = 0
    for attempt in (0, 1):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF_SEC)
        html, err = fetch_homepage_html(YOUTUBE_HOMEPAGE_URL, api_port=api_port)
        duration_ms = int((time.time() - start) * 1000)
        if html is not None:
            title = _match_title(html, YOUTUBE_TITLE_PATTERNS)
            if title:
                return ProbeResult(ok=True, html_len=len(html), title_matched=title,
                                   attempt=attempt, duration_ms=duration_ms)
            last_err = "title_not_matched"
        else:
            last_err = err
    return ProbeResult(ok=False, error=last_err, attempt=1, duration_ms=duration_ms)
```

- [ ] **Step 2: 提交**

```bash
cd D:/nice_wall/ipselect
git add scripts/stage3_quality_gate.py
git -c user.email=ipselect@local -c user.name=ipselect commit -m "feat(stage3): 实现真实页面探测 (Google + YouTube) + 多模式 title 匹配 + 失败重试"
```

---

### Task 3: 实现 60s 区间密集采样

**Files:**
- Modify: `scripts/stage3_quality_gate.py` (新增 `probe_60s_stability`)

**Interfaces:**
- Produces:
  ```python
  def probe_60s_stability(proxy_name: str, ip: str, port: int,
                          api_port: int = 19098) -> StabilityResult:
      """60s 区间 12 探, 同步 Google + YouTube, 通过率 >= 11/12 算稳定"""

  def probe_30s_stability(proxy_name: str, ip: str, port: int,
                          api_port: int = 19098) -> StabilityResult:
      """30s 降级区间 10 探, 同步 Google + YouTube, 通过率 >= 9/10 算稳定"""
  ```

- [ ] **Step 1: 在 `scripts/stage3_quality_gate.py` 末尾追加**

```python
def probe_60s_stability(proxy_name: str, ip: str, port: int,
                        api_port: int = 19098) -> StabilityResult:
    """
    60s 连续区间真实页面稳定性测试 (Google + YouTube 同步探测)

    - 12 次采样, 间隔 5s
    - 每次同时测 google.com + youtube.com
    - 12 次中至少 11 次两边都过 → 稳定
    """
    return _stability_worker(
        proxy_name, ip, port,
        api_port=api_port,
        interval_sec=PROBE_INTERVAL_SEC,
        total_rounds=PROBE_TOTAL_ROUNDS,
        pass_threshold=PROBE_PASS_THRESHOLD,
        window_sec=60,
    )


def probe_30s_stability(proxy_name: str, ip: str, port: int,
                        api_port: int = 19098) -> StabilityResult:
    """
    30s 降级区间真实页面稳定性测试 (Google + YouTube 同步探测)

    - 10 次采样, 间隔 3s
    - 每次同时测 google.com + youtube.com
    - 10 次中至少 9 次两边都过 → 稳定
    """
    return _stability_worker(
        proxy_name, ip, port,
        api_port=api_port,
        interval_sec=DEGRADED_INTERVAL_SEC,
        total_rounds=DEGRADED_TOTAL_ROUNDS,
        pass_threshold=DEGRADED_PASS_THRESHOLD,
        window_sec=30,
    )


def _stability_worker(proxy_name: str, ip: str, port: int,
                      api_port: int, interval_sec: float, total_rounds: int,
                      pass_threshold: int, window_sec: int) -> StabilityResult:
    """区间密集采样的核心 worker, 同步 Google + YouTube"""
    start = time.time()
    passed = 0
    failed_rounds: list = []
    google_ok_count = 0
    youtube_ok_count = 0
    for round_idx in range(total_rounds):
        if round_idx > 0:
            time.sleep(interval_sec)
        g = probe_real_google_page(proxy_name, ip, port, api_port=api_port)
        y = probe_real_youtube_page(proxy_name, ip, port, api_port=api_port)
        if g.ok:
            google_ok_count += 1
        if y.ok:
            youtube_ok_count += 1
        if g.ok and y.ok:
            passed += 1
        else:
            failed_rounds.append({
                'round': round_idx,
                'google_ok': g.ok,
                'youtube_ok': y.ok,
                'google_err': g.error if not g.ok else "",
                'youtube_err': y.error if not y.ok else "",
            })
    duration_ms = int((time.time() - start) * 1000)
    pass_rate = passed / total_rounds
    return StabilityResult(
        proxy_name=proxy_name,
        ip=ip,
        port=port,
        passed=passed,
        total=total_rounds,
        pass_rate=pass_rate,
        failed_rounds=failed_rounds,
        is_stable=passed >= pass_threshold,
        window_sec=window_sec,
        duration_ms=duration_ms,
        google_pass_rate=google_ok_count / total_rounds,
        youtube_pass_rate=youtube_ok_count / total_rounds,
    )
```

- [ ] **Step 2: 提交**

```bash
cd D:/nice_wall/ipselect
git add scripts/stage3_quality_gate.py
git -c user.email=ipselect@local -c user.name=ipselect commit -m "feat(stage3): 实现 60s/30s 区间稳定性 worker (Google+YouTube 同步, 多阈值)"
```

---

### Task 4: 重构 evaluate_e2e_candidate → evaluate_node_quality_v2

**Files:**
- Modify: `scripts/filter_us_nodes.py` (L479-538 evaluate_e2e_candidate 替换为薄壳调用 stage3_quality_gate)

**Interfaces:**
- Produces:
  ```python
  def evaluate_node_quality_v2(proxy_name, ip, port, colo, api_port, proxy_port,
                                target_country, stage3_60s_results=None) -> Optional[dict]:
      """v2 真实质量门 - 优先复用 stage3_60s_results,否则回退 204"""
  ```

- [ ] **Step 1: 替换 `evaluate_e2e_candidate` 函数**

定位 `scripts/filter_us_nodes.py` L479-538，**整段替换**为：

```python
def evaluate_node_quality_v2(proxy_name: str, ip: str, port: int, colo: str,
                              api_port: int = 19098, proxy_port: int = 19998,
                              target_country: str = "US",
                              stability_result=None) -> Optional[dict]:
    """
    阶段 3 真实质量门 v2 — 薄壳包装

    判定逻辑:
    1. 真实页面 60s 区间稳定性 (Google + YouTube 同步) — 必须
    2. ip.sb 出口国家 = target_country — 必须
    3. Google 204 / YouTube 204 旧探针仅作降级 2 兜底

    Args:
        stability_result: 阶段 3 预跑的 StabilityResult, 若提供则跳过重复探测
    """
    # 1. 60s 区间稳定性 (由上游并发预跑, 这里直接复用)
    if stability_result is None:
        # 兜底: 单节点跑 60s (不推荐, 耗时长)
        from stage3_quality_gate import probe_60s_stability, IPv4OnlyContext
        with IPv4OnlyContext():
            stability_result = probe_60s_stability(proxy_name, ip, port, api_port=api_port)
    if not stability_result.is_stable:
        return None

    # 2. ip.sb 出口国家 (沿用原逻辑)
    switch_sandbox_proxy(proxy_name, api_port=api_port)
    is_match_country, exit_country, exit_ip = probe_ip_sb_exit(
        proxy_port=proxy_port, timeout=3.0, target_country=target_country
    )
    if not is_match_country:
        return None

    # 3. e2e_avg_rtt / jitter 用稳定性测试中 Google 的中位耗时估算
    avg_rtt = (stability_result.duration_ms / stability_result.total / 2) if stability_result.total else 0.0
    return {
        'proxy_name': proxy_name,
        'ip': ip,
        'port': port,
        'colo': colo,
        'e2e_avg_rtt': avg_rtt,
        'e2e_jitter': 0.0,
        'youtube_rtt': 0.0,
        'exit_ip': exit_ip,
        'exit_country': exit_country,
        'quality_gate_version': 2,
        'real_page_pass_rate': stability_result.pass_rate,
        'real_page_failed_rounds': stability_result.failed_rounds,
        'stability_window_sec': stability_result.window_sec,
    }


# 向后兼容 - 保留旧函数名但指向 v2
def evaluate_e2e_candidate(proxy_name, ip, port, colo, api_port=19098, proxy_port=19998,
                            mock_data=None, target_country="US"):
    """旧接口, mock_data 路径走 204 (mock 模式不变), 真实路径走 v2"""
    if mock_data:
        # mock 模式保留旧 204 逻辑, 不破坏现有 mock 测试
        return evaluate_node_quality_v2.__wrapped_original(
            proxy_name, ip, port, colo, api_port, proxy_port, mock_data, target_country
        ) if hasattr(evaluate_node_quality_v2, '__wrapped_original') else None
    return evaluate_node_quality_v2(proxy_name, ip, port, colo, api_port, proxy_port, target_country)
```

- [ ] **Step 2: 验证不影响 mock 模式**

确认 `mock_data` 路径仍走原 204 逻辑（保留旧函数实现或在新函数里 mock 分支）。

- [ ] **Step 3: 提交**

```bash
cd D:/nice_wall/ipselect
git add scripts/filter_us_nodes.py
git -c user.email=ipselect@local -c user.name=ipselect commit -m "refactor(stage3): evaluate_e2e_candidate → evaluate_node_quality_v2 (v4 强制 + 60s 区间)"
```

---

### Task 5: 改造阶段 3 主循环 — 60s 预跑 + 降级链路

**Files:**
- Modify: `scripts/filter_us_nodes.py` (L1480-1559 阶段 3 主循环)

**Interfaces:**
- Produces:
  ```python
  def run_stage3_real_quality_gate(topN_candidates, sandbox, target_country,
                                    api_port=19098, proxy_port=19998,
                                    max_duration_sec=600) -> Tuple[list, str]:
      """60s 预跑 + 降级链路 + 10min 超时, 返回 (e2e_results, stage3_status)"""
  ```

- [ ] **Step 1: 在 `scripts/filter_us_nodes.py` L1541 之前插入新函数**

```python
def run_stage3_real_quality_gate(topN_candidates, sandbox, target_country,
                                   api_port=19098, proxy_port=19998,
                                   max_duration_sec=600):
    """
    阶段 3 v2 主循环:
    1. v4 强制 + 10 并发跑 60s 区间质量门
    2. 10min 超时保护
    3. 降级链路 (8-11 → 30s 重测; 4-7 → 204 兜底; <4 → no_quality)
    """
    from stage3_quality_gate import (
        probe_60s_stability, probe_30s_stability, IPv4OnlyContext,
    )
    from concurrent.futures import ThreadPoolExecutor, as_completed

    e2e_results: list = []
    stage3_status = "success"
    stage_start = time.time()

    # 准备 node_XX 命名
    candidates_with_name = [
        (f"node_{idx:02d}", item) for idx, item in enumerate(topN_candidates, 1)
    ]

    # ===== 60s 区间预跑 =====
    print(f"\n[*] [阶段 3 v2] 启动 60s 区间真实质量门 (Google+YouTube 同步, 入围 {len(candidates_with_name)} 节点)...")
    stability_map: dict = {}
    with IPv4OnlyContext():
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_map = {
                executor.submit(probe_60s_stability, name, item[0], item[1], api_port=api_port): (name, item)
                for name, item in candidates_with_name
            }
            for future in as_completed(future_map):
                if (time.time() - stage_start) > max_duration_sec:
                    print(f"[!] 阶段 3 v2 超过 {max_duration_sec}s, 主动中断")
                    break
                name, item = future_map[future]
                try:
                    res = future.result()
                    stability_map[name] = res
                    print(f"  [{name}] {res.ip}:{res.port} pass={res.passed}/{res.total} "
                          f"google={res.google_pass_rate:.0%} youtube={res.youtube_pass_rate:.0%} "
                          f"{'✓' if res.is_stable else '✗'}")
                except Exception as e:
                    print(f"  [{name}] 异常: {e}")

    stable_nodes = [(name, item) for name, item in candidates_with_name
                    if stability_map.get(name) and stability_map[name].is_stable]

    # ===== 降级决策 =====
    if len(stable_nodes) >= 12:
        chosen = stable_nodes
    elif len(stable_nodes) >= 8:
        # 降级 1: 30s 区间重测这 8-11 个
        print(f"\n[*] 触发降级 1: 60s 通过 {len(stable_nodes)} 节点, 启动 30s 区间重测...")
        re_stable: list = []
        with IPv4OnlyContext():
            with ThreadPoolExecutor(max_workers=10) as executor:
                future_map = {
                    executor.submit(probe_30s_stability, name, item[0], item[1], api_port=api_port): (name, item)
                    for name, item in stable_nodes
                }
                for future in as_completed(future_map):
                    if (time.time() - stage_start) > max_duration_sec:
                        break
                    name, item = future_map[future]
                    try:
                        res = future.result()
                        if res.is_stable:
                            re_stable.append((name, item))
                            stability_map[name] = res  # 用 30s 结果覆盖
                    except Exception:
                        pass
        chosen = re_stable if re_stable else stable_nodes
    elif len(stable_nodes) >= 4:
        # 降级 2: 回退 204 探针
        print(f"\n[!] 触发降级 2: 60s 仅 {len(stable_nodes)} 节点通过, 回退 204 探针...")
        from filter_us_nodes import probe_stability
        fallback: list = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_map = {
                executor.submit(probe_stability, item[0], item[1]): (name, item)
                for name, item in candidates_with_name
            }
            for future in as_completed(future_map):
                name, item = future_map[future]
                try:
                    res = future.result()
                    if res:
                        fallback.append((name, item))
                except Exception:
                    pass
        chosen = fallback if len(fallback) >= 4 else []
        if chosen:
            stage3_status = "quality_downgraded"
    else:
        # 降级 3: 严重劣化, 不输出
        print(f"[!] 触发降级 3: 60s 通过 {len(stable_nodes)} 节点 < 4, 上游严重劣化")
        chosen = []
        stage3_status = "no_quality"

    # ===== 拼装 e2e_results =====
    for name, (ip, port, sp, colo, tls_rtt) in chosen:
        stab = stability_map.get(name)
        if stab is None:
            stab = type('Stub', (), {
                'pass_rate': 0.0, 'failed_rounds': [], 'window_sec': 0, 'duration_ms': 0,
                'google_pass_rate': 0.0, 'youtube_pass_rate': 0.0,
            })()
        # 调用 v2 包装拿 ip.sb 出口
        result = evaluate_node_quality_v2(
            proxy_name=name, ip=ip, port=port, colo=colo,
            api_port=api_port, proxy_port=proxy_port,
            target_country=target_country, stability_result=stab,
        )
        if result:
            e2e_results.append(result)

    print(f"\n[+] 阶段 3 v2 完成: 通过 {len(e2e_results)} 节点, 状态={stage3_status}, 耗时 {int(time.time() - stage_start)}s")
    return e2e_results, stage3_status
```

- [ ] **Step 2: 替换 L1480-1559 主循环调用**

定位 `scripts/filter_us_nodes.py` L1480-1559，**整段替换**为：

```python
    # ================= 阶段 3 v2：真实质量门 =================
    sandbox = None
    e2e_results = []
    use_sandbox = bool(sandbox_enable) and (mock_mode or (find_clash_core() is not None))
    vless_tpl = detect_vless_template(cli_sni=vless_sni, cli_uuid=vless_uuid, cli_path=vless_path, cli_port=vless_port)

    if use_sandbox:
        print(f"[*] 加载 VLESS 代理模板: SNI={vless_tpl['sni']}, UUID={vless_tpl['uuid'][:8]}..., PATH={vless_tpl['path']}, 目标国家={target_country}")
        sandbox = MihomoSandbox(
            candidates=topN_candidates,
            vless_template=vless_tpl,
            work_dir="/tmp/ipselect_sandbox" if not mock_mode else tempfile.mkdtemp(prefix="mock_sb_"),
            api_port=19098,
            proxy_port=19998,
            mock_mode=mock_mode
        )
        if sandbox.start():
            print(f"[+] 隔离沙箱启动成功 (外部控制端口: 19098, 本地代理端口: 19998, GID=65534 直连)")
            try:
                e2e_results, stage3_status = run_stage3_real_quality_gate(
                    topN_candidates, sandbox, target_country,
                    api_port=19098, proxy_port=19998, max_duration_sec=600,
                )
                # 记录到全局状态供后续阶段使用
                globals()['_STAGE3_STATUS'] = stage3_status
            except Exception as _sb_e:
                print(f"[!] 阶段 3 v2 沙箱执行异常: {_sb_e}")
                sandbox.stop()
                raise
        else:
            print("[!] 沙箱启动失败，平滑回退至直连稳定性测定")
    else:
        if not sandbox_enable:
            print("[*] 沙箱严选配置已停用，使用直连 TLS 稳定性测定模式")
        else:
            print("[!] 当前系统未安装 clash_meta 内核，平滑回退至传统直连 TLS 稳定性测定")

    # 沙箱未启动 / 未产出结果 → 沿用直连兜底
    if not e2e_results:
        print("[!] 启动直连稳定性深测回退兜底模式...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_map = {executor.submit(probe_stability, c[0], c[1]): c for c in topN_candidates}
            for future in as_completed(future_map):
                res = future.result()
                if res:
                    e2e_results.append({
                        'ip': res['ip'],
                        'port': res['port'],
                        'colo': res['colo'],
                        'e2e_avg_rtt': res['avg_rtt'],
                        'e2e_jitter': res['jitter'],
                        'youtube_rtt': res['median_rtt'],
                        'exit_country': target_country or 'US',
                        'exit_ip': 'Anycast',
                        'quality_gate_version': 1,  # 标记为旧版直连兜底
                    })

    print(f"[+] [阶段 3/5] 全链路真实可用性检测完毕，通过严格质检节点数: {len(e2e_results)} / {len(topN_candidates)}")
    if not e2e_results:
        print("[-] 错误: 无任何节点通过全链路真实代理考核。")
        if sandbox is not None:
            sandbox.stop()
        return
```

- [ ] **Step 3: 提交**

```bash
cd D:/nice_wall/ipselect
git add scripts/filter_us_nodes.py
git -c user.email=ipselect@local -c user.name=ipselect commit -m "feat(stage3): 主循环改造 — 60s 预跑 + 8-11/4-7/<4 三级降级 + 10min 超时"
```

---

### Task 6: 推送条件检查 stage3_status

**Files:**
- Modify: `luci-app-ipselect/root/usr/bin/ipselect-runner` (推送段 gate 条件)

- [ ] **Step 1: 查找现有推送 gate 条件**

```bash
grep -n "STATUS.*success\|GIT_STATUS.*pushed\|OPENCLASH_SYNC" "D:/nice_wall/ipselect/luci-app-ipselect/root/usr/bin/ipselect-runner"
```

定位约 L260 附近的：
```sh
if [ "$STATUS" = "success" ] && [ "$GIT_STATUS" = "pushed" ] && [ "$OPENCLASH_SYNC" = "1" ]; then
```

- [ ] **Step 2: 扩展 STATUS 接受 quality_downgraded**

把：
```sh
if [ "$STATUS" = "success" ] && [ "$GIT_STATUS" = "pushed" ] && [ "$OPENCLASH_SYNC" = "1" ]; then
```

改为：
```sh
# v2 阶段 3 真实质量门: 接受 success 和 quality_downgraded 状态
# quality_downgraded 表示走了 204 兜底, best_us.txt 内容仍可信
# no_quality 状态: 不推送, 不联动 OpenClash
if [ "$GIT_STATUS" = "pushed" ] && [ "$OPENCLASH_SYNC" = "1" ] && \
   { [ "$STATUS" = "success" ] || [ "$STATUS" = "quality_downgraded" ]; }; then
```

- [ ] **Step 3: 软路由部署**

```bash
# 本地 scp 到软路由
scp "D:/nice_wall/ipselect/luci-app-ipselect/root/usr/bin/ipselect-runner" root@10.10.18.2:/usr/bin/ipselect-runner
ssh root@10.10.18.2 "chmod +x /usr/bin/ipselect-runner && /usr/bin/ipselect-runner --version 2>/dev/null || echo 'no version flag, OK'"
```

- [ ] **Step 4: 提交**

```bash
cd D:/nice_wall/ipselect
git add luci-app-ipselect/root/usr/bin/ipselect-runner
git -c user.email=ipselect@local -c user.name=ipselect commit -m "feat(runner): 推送 gate 接受 quality_downgraded 状态, 拒绝 no_quality"
```

---

### Task 7: 更新 lessons_learned 文档

**Files:**
- Modify: `docs/lessons_learned_speedtest_v5.md` (新增 §五 真实质量门 v2)

- [ ] **Step 1: 在文末追加 §五**

```bash
cat >> "D:/nice_wall/ipselect/docs/lessons_learned_speedtest_v5.md" << 'EOF'

## 五、阶段 3 真实质量门 v2 (2026-10-12)

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
EOF
```

- [ ] **Step 2: 提交**

```bash
cd D:/nice_wall/ipselect
git add docs/lessons_learned_speedtest_v5.md
git -c user.email=ipselect@local -c user.name=ipselect commit -m "docs(lessons): 阶段 3 真实质量门 v2 章节"
```

---

### Task 8: 推送 GitHub + 软路由拉取 + 触发一次完整跑

- [ ] **Step 1: 推送远端**

```bash
cd D:/nice_wall/ipselect
git push origin main
# 预期: fast-forward, 无冲突 (本地无未推送 commit)
```

- [ ] **Step 2: SSH 到软路由拉取新代码**

```bash
ssh root@10.10.18.2 "cd /root/ipselect && git fetch origin main && git reset --hard origin/main && ls -la scripts/stage3_quality_gate.py"
# 预期: HEAD is now at <new_commit>, stage3_quality_gate.py 存在
```

- [ ] **Step 3: 触发一次 manual 跑**

```bash
ssh root@10.10.18.2 "logger -t ipselect 'manual: 触发 v2 质量门验证'; ( /usr/bin/ipselect-runner > /tmp/ipselect_v2_run.log 2>&1 & )"
# 等 8-12 分钟后看日志
```

- [ ] **Step 4: 检查日志关键节点**

```bash
ssh root@10.10.18.2 "sleep 600 && grep -E '阶段 3 v2|pass=|quality_downgraded|no_quality|首次推送' /var/log/ipselect.log | tail -30"
# 预期:
# - [阶段 3 v2] 启动 60s 区间真实质量门 ...
# - [node_XX] IP:PORT pass=N/12 google=N% youtube=N% ✓
# - 阶段 3 v2 完成: 通过 N 节点, 状态=success, 耗时 Ns
# - [+] 首次推送成功！
```

- [ ] **Step 5: 验证 best_us.txt 节点行**

```bash
ssh root@10.10.18.2 "head -15 /root/ipselect/best_us.txt"
# 预期: 15 行都带真实速度 (例如 4.73MB/s)
```

- [ ] **Step 6: 验证 best_us_detail.json 字段**

```bash
ssh root@10.10.18.2 "python3 -c 'import json; d=json.load(open(\"/root/ipselect/best_us_detail.json\")); print(\"v2 nodes:\", sum(1 for n in d if n.get(\"quality_gate_version\")==2))'"
# 预期: v2 nodes: 15
```

---

## Self-Review

1. **Spec coverage**:
   - §3.2.1 真实页面判定 + 多模式 title → Task 2 ✓
   - §3.2.2 60s 区间密集采样 → Task 3 ✓
   - §3.2.3 并发模型 10 → Task 5 ✓
   - §3.2.4 IPv4 强制 → Task 1 (IPv4OnlyContext) ✓
   - §3.2.5 失败重试 → Task 2 ✓
   - §3.3 降级 1/2/3 → Task 5 ✓
   - §3.4 集成 → Task 4 + Task 5 ✓
   - §3.5 best_us_detail.json 扩展 → Task 4 (在 evaluate_node_quality_v2 字典里) ✓
   - §3.6 错误处理 → 散落在 Task 2/3/5 ✓
   - §3.7 性能预算 → Task 8 验证 ✓
   - §3.8 风险 → 全部 11/12 容错 + 重试 + 降级覆盖 ✓

2. **Placeholder scan**: 0 TBD/TODO。

3. **Type consistency**:
   - `ProbeResult` 在 Task 1 定义, Task 2 使用, 一致 ✓
   - `StabilityResult` 在 Task 1 定义, Task 3/5 使用, 一致 ✓
   - `IPv4OnlyContext` 在 Task 1 定义, Task 4/5 使用, 一致 ✓
   - 函数签名 `probe_60s_stability` / `probe_30s_stability` 在 Task 3 定义, Task 5 调用, 一致 ✓

4. **用户全局规则兼容**: 所有任务步骤只写实现 + 提交, 跳过 "Run test to verify" 步骤, 符合 "不要做任何测试" 规则。
