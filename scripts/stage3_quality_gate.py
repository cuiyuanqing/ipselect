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


# ==================== 真实页面探测 ====================
def fetch_homepage_html(url: str, api_port: int = 19098, max_bytes: int = MAX_BYTES_PER_PROBE,
                         timeout_sec: float = PROBE_TIMEOUT_SEC) -> Tuple[Optional[bytes], str]:
    """
    通过沙箱代理拉取首 N 字节 HTML
    返回 (html_bytes_or_None, error_message)
    """
    proxy_url = f"http://127.0.0.1:{api_port + 90}"  # 19998 = 19098 + 90
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
    duration_ms = 0
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
