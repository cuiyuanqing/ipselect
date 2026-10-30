#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AimiliVPN 智能纯净家宽节点优选器 (Aimili Pure Node Picker)
===========================================================
功能：
  1. 自动连接本地 AimiliVPN (127.0.0.1:8501)，获取指定地区（如日本 JP）的存活节点候选池；
  2. 批量并发查询目标节点的真实风控分、纯净度、共享人数及家庭宽带属性（无需连入隧道测试）；
  3. 智能多维度打分优选（过滤非家宽、过滤风控>15%节点，按风控、共享人数与延迟排序）；
  4. 自动将精选出的极度纯净家宽节点写入 AimiliVPN 收藏夹，无缝联动「仅用收藏」出站模式；
  5. 内置自包含的现代 Web 控制面板与定时巡检任务，零第三方依赖（纯标准库）。

作者：Jeff Smith 专属定制
"""

import argparse
import http.cookiejar
import http.server
import json
import logging
import os
import re
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

# ==============================================================================
# 1. 日志与基础配置
# ==============================================================================

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

class RingLogHandler(logging.Handler):
    """内存日志环形缓冲区，供 Web 界面实时展示最近日志"""
    def __init__(self, capacity: int = 200):
        super().__init__()
        self.capacity = capacity
        self.records: List[str] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            with self._lock:
                self.records.append(msg)
                if len(self.records) > self.capacity:
                    self.records.pop(0)
        except Exception:
            self.handleError(record)

    def get_logs(self) -> List[str]:
        with self._lock:
            return list(self.records)

logger = logging.getLogger("AimiliPicker")
logger.setLevel(logging.INFO)

# 控制台输出
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
logger.addHandler(console_handler)

# 内存日志输出
ring_handler = RingLogHandler(capacity=300)
ring_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
logger.addHandler(ring_handler)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "picker_data")
os.makedirs(DATA_DIR, exist_ok=True)

CACHE_FILE = os.path.join(DATA_DIR, "ip_intel_cache.json")
CONFIG_FILE = os.path.join(DATA_DIR, "picker_config.json")
HISTORY_FILE = os.path.join(DATA_DIR, "latest_picked_nodes.json")

# ==============================================================================
# 2. 数据结构定义
# ==============================================================================

@dataclass
class IpQuality:
    ip: str
    country: str = ""
    location: str = ""
    isp: str = ""
    asn: str = ""
    ip_type: str = "未知"          # e.g., "家庭宽带 IP", "数据中心 IP"
    is_residential: bool = False
    risk_score: int = 100         # 0 ~ 100
    risk_label: str = "未知"       # e.g., "极度纯净", "低风险", "高风险"
    is_native: bool = False       # 原生 IP
    shared_users: str = "未知"    # e.g., "1 - 10 (极好)", "10 - 100 (一般)"
    updated_at: float = 0.0

@dataclass
class CandidateNode:
    id: str
    ip: str
    port: int
    country: str
    protocol: str
    latency_ms: int
    pool_endpoint_id: str
    aliases: List[str]
    quality: Optional[IpQuality] = None
    rank_score: float = 9999.0

# ==============================================================================
# 3. AimiliVPN 本地客户端 (与 127.0.0.1:8501 交互)
# ==============================================================================

class AimiliClient:
    """与本地运行的 AimiliVPN 管理后端进行通信与鉴权"""

    def __init__(self, auth_file: str = "/opt/aimilivpn/vpngate_data/ui_auth.json"):
        self.auth_file = auth_file
        self.host = "127.0.0.1"
        self.port = 8501
        self.username = "admin"
        self.password = ""
        self.secret_path = ""
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.logged_in = False
        self._load_auth()

    def _load_auth(self):
        """读取本地 Aimili 密码与配置"""
        if os.path.isfile(self.auth_file):
            try:
                with open(self.auth_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.host = data.get("host", "127.0.0.1")
                    self.port = int(data.get("port", 8501))
                    self.username = data.get("username", "admin")
                    self.password = data.get("password", "")
                    self.secret_path = data.get("secret_path", "")
                    logger.info(f"🔑 成功从 [{self.auth_file}] 加载 Aimili 认证凭据 (用户: {self.username}, 端口: {self.port})")
                    return
            except Exception as e:
                logger.warning(f"⚠️ 解析 {self.auth_file} 失败: {e}，将使用默认参数")
        else:
            logger.warning(f"⚠️ 未找到 {self.auth_file}，使用本地默认 http://127.0.0.1:8501")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def login(self) -> bool:
        """登录 Aimili 获取会话 Cookie"""
        url = f"{self.base_url}/api/login"
        payload = json.dumps({"username": self.username, "password": self.password}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "AimiliPurePicker/1.0"}
        )
        try:
            with self.opener.open(req, timeout=10) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                if res_data.get("ok"):
                    self.logged_in = True
                    logger.info("✅ 成功登录 AimiliVPN 本地管理服务")
                    return True
                else:
                    logger.error(f"❌ 登录 AimiliVPN 失败: {res_data.get('error', '未知错误')}")
                    return False
        except Exception as e:
            logger.error(f"❌ 请求 AimiliVPN 登录接口失败 [{url}]: {e}")
            return False

    def fetch_scoped_nodes(self, country: str = "JP", status: str = "available", limit: int = 150) -> List[CandidateNode]:
        """从 Aimili 提取指定国家和状态的候选节点"""
        if not self.logged_in and not self.login():
            return []

        url = f"{self.base_url}/api/ui/nodes?country={country}&status={status}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "AimiliPurePicker/1.0"})
        try:
            with self.opener.open(req, timeout=12) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                raw_nodes = res.get("nodes", [])
                candidates = []
                for n in raw_nodes:
                    nid = str(n.get("id") or n.get("pool_endpoint_id") or "")
                    ip = n.get("ip") or n.get("host") or ""
                    if not ip or not nid:
                        continue
                    # 构造与 Aimili 严格一致的 alias 键集
                    pool_id = str(n.get("pool_endpoint_id") or "")
                    aliases = [nid]
                    if pool_id:
                        aliases.extend([pool_id, f"pool:{pool_id}"])
                    aliases.append(nid.replace("pool:", ""))
                    aliases = list(set([a for a in aliases if a]))

                    candidates.append(CandidateNode(
                        id=nid,
                        ip=ip,
                        port=int(n.get("port") or 1194),
                        country=n.get("country", country),
                        protocol=n.get("protocol", "openvpn"),
                        latency_ms=int(n.get("latency") or 999),
                        pool_endpoint_id=pool_id,
                        aliases=aliases
                    ))
                logger.info(f"📡 从 Aimili 获取到 [{country}] 地区存活候选节点共 {len(candidates)} 个")
                return candidates
        except Exception as e:
            logger.error(f"❌ 拉取 Aimili 节点列表异常: {e}")
            return []

    def toggle_favorite(self, node: CandidateNode, favorite: bool = True) -> bool:
        """调用 Aimili 的收藏切换接口"""
        if not self.logged_in and not self.login():
            return False

        url = f"{self.base_url}/api/toggle_favorite"
        payload = json.dumps({
            "id": node.id,
            "favorite": favorite,
            "aliases": node.aliases
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "AimiliPurePicker/1.0"}
        )
        try:
            with self.opener.open(req, timeout=8) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return bool(res.get("ok"))
        except Exception as e:
            logger.error(f"❌ 收藏节点 [{node.ip}] 异常: {e}")
            return False

    def get_current_favorites(self) -> List[str]:
        """获取 Aimili 当前已收藏的节点 ID 列表"""
        url = f"{self.base_url}/api/ui/state"
        req = urllib.request.Request(url, headers={"User-Agent": "AimiliPurePicker/1.0"})
        try:
            with self.opener.open(req, timeout=8) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                state = res.get("state", {})
                return list(state.get("favorite_node_ids", []))
        except Exception as e:
            logger.warning(f"⚠️ 获取当前收藏列表异常: {e}")
            return []

# ==============================================================================
# 4. IP 质量与风控情报检测器 (基于 Ping0 等多数据源)
# ==============================================================================

class IpIntelligenceManager:
    """负责并发查询各目标 IP 的风控分、家庭宽带属性与共享人数，带本地 24h 智能缓存"""

    def __init__(self, cache_file: str = CACHE_FILE, ttl_hours: int = 24):
        self.cache_file = cache_file
        self.ttl_seconds = ttl_hours * 3600
        self.cache: Dict[str, Dict[str, Any]] = {}
        self._load_cache()

    def _load_cache(self):
        if os.path.isfile(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception as e:
                logger.warning(f"⚠️ 读取 IP 缓存失败: {e}")

    def _save_cache(self):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ 保存 IP 缓存异常: {e}")

    def inspect_ip(self, ip: str) -> IpQuality:
        """查询单个 IP 的风控质量，优先读取未过期缓存"""
        now = time.time()
        cached = self.cache.get(ip)
        if cached and (now - cached.get("updated_at", 0) < self.ttl_seconds):
            return IpQuality(**cached)

        # 缓存缺失或过期，通过数据源爬取
        quality = self._query_ping0(ip)
        self.cache[ip] = asdict(quality)
        self._save_cache()
        return quality

    def _query_ping0(self, ip: str) -> IpQuality:
        """直接解析 Ping0 的纯净度、家宽属性与共享人数数据"""
        url = f"https://ping0.cc/ip/{ip}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        quality = IpQuality(ip=ip, updated_at=time.time())

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            # 1. 提取风控值与标签
            # 格式: <div class="riskitem riskcurrent" style="background:green" title="0-15 极度纯净"><span class="value">7%</span><span class="lab"> 极度纯净</span></div>
            risk_match = re.search(r'class="riskitem riskcurrent"[^>]*><span class="value">(\d+)%</span><span class="lab">\s*([^<]+)</span>', html)
            if risk_match:
                quality.risk_score = int(risk_match.group(1))
                quality.risk_label = risk_match.group(2).strip()
            else:
                # 备用匹配风控百分比
                pct_match = re.search(r'class="value">(\d+)%</span>', html)
                if pct_match:
                    quality.risk_score = int(pct_match.group(1))

            # 2. 提取 IP 类型 (家庭宽带 vs IDC)
            if "家庭宽带 IP" in html:
                quality.ip_type = "家庭宽带 IP"
                quality.is_residential = True
            elif "商业宽带" in html:
                quality.ip_type = "商业宽带 IP"
                quality.is_residential = True
            elif "数据中心" in html or "IDC" in html:
                quality.ip_type = "数据中心 IDC"
                quality.is_residential = False
            else:
                quality.ip_type = "未知宽带类型"

            # 3. 提取原生 IP
            if "原生 IP" in html:
                quality.is_native = True

            # 4. 提取共享人数
            # 格式: <div class="usecountbar" usecount="10 - 100 (一般)" ...>
            usecount_match = re.search(r'usecount="([^"]+)"', html)
            if usecount_match:
                quality.shared_users = usecount_match.group(1).strip()
            else:
                # 备选模糊提取
                uc_fallback = re.search(r'class="usecountbar"[^>]*>\s*([^\r\n<]+)', html)
                if uc_fallback:
                    quality.shared_users = uc_fallback.group(1).strip()

            # 5. 提取 ISP / ASN
            asn_match = re.search(r'name="description" content="[^"]*?-([^"]+)"', html)
            if asn_match:
                desc_parts = asn_match.group(1).split("-")
                if len(desc_parts) >= 3:
                    quality.asn = desc_parts[0].strip()
                    quality.isp = desc_parts[1].strip()

            logger.info(f"🔍 [Ping0] IP {ip:<15} -> 类型: {quality.ip_type:<10} | 风控: {quality.risk_score}% ({quality.risk_label}) | 共享人数: {quality.shared_users} | 原生: {quality.is_native}")

        except Exception as e:
            logger.warning(f"⚠️ 查询 Ping0 [{ip}] 失败: {e}，赋予保底中性评分")
            # 网络兜底
            quality.risk_score = 50
            quality.risk_label = "检测超时"

        return quality

# ==============================================================================
# 5. 核心优选排程器 (筛选、打分、落库收藏)
# ==============================================================================

class PickerEngine:
    """优选排程核心"""

    def __init__(self, aimili: AimiliClient, inspector: IpIntelligenceManager):
        self.aimili = aimili
        self.inspector = inspector
        self.is_running = False
        self.current_step = "空闲"
        self.last_result: Dict[str, Any] = {}
        self.load_history()

    def load_history(self):
        if os.path.isfile(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    self.last_result = json.load(f)
            except Exception:
                pass

    def run_screening(self, country: str = "JP", max_risk: int = 15, top_n: int = 5, replace_favorites: bool = True) -> List[CandidateNode]:
        """执行完整筛选流程"""
        if self.is_running:
            logger.warning("⚠️ 上一次优选任务仍在执行中，忽略重复触发")
            return []

        self.is_running = True
        picked_nodes: List[CandidateNode] = []
        start_time = time.time()

        try:
            self.current_step = f"正在从 AimiliVPN 提取 [{country}] 地区存活候选节点..."
            logger.info(f"🚀 开始执行 [{country}] 地区纯净家宽优选任务 (最大风控阈值: {max_risk}%, 目标入选数: {top_n})...")

            candidates = self.aimili.fetch_scoped_nodes(country=country, status="available", limit=100)
            if not candidates:
                logger.error("❌ 未从 Aimili 获取到任何存活节点，请先确认 Aimili 后台节点扫描是否完成！")
                self.current_step = "未获取到候选节点"
                return []

            total_candidates = len(candidates)
            self.current_step = f"正在对 {total_candidates} 个候选节点进行风控与共享画像分析..."

            # 逐个探测风控质量 (加入微小延迟保护 Ping0 频率)
            for idx, node in enumerate(candidates, start=1):
                self.current_step = f"正在检测风控画像 ({idx}/{total_candidates}): {node.ip}"
                node.quality = self.inspector.inspect_ip(node.ip)
                time.sleep(0.3)

            # 过滤逻辑：
            # 1. 必须是家庭宽带 (严格过滤机房与未知)
            # 2. 风控值必须 <= max_risk (例如 15% 极度纯净)
            self.current_step = "正在进行风控阈值过滤与综合加权打分..."
            valid_residential: List[CandidateNode] = []

            for node in candidates:
                q = node.quality
                if not q:
                    continue
                if not q.is_residential:
                    logger.debug(f"🚫 淘汰节点 {node.ip}: 非家庭宽带 ({q.ip_type})")
                    continue
                if q.risk_score > max_risk:
                    logger.debug(f"🚫 淘汰节点 {node.ip}: 风控分超标 ({q.risk_score}% > {max_risk}%)")
                    continue
                valid_residential.append(node)

            logger.info(f"📊 粗筛完毕: {total_candidates} 个节点中共有 {len(valid_residential)} 个符合极度纯净家宽要求！")

            if not valid_residential:
                logger.warning(f"⚠️ 未发现风控值 ≤ {max_risk}% 的纯净家宽，尝试放宽至 25% 寻找较优备选...")
                for node in candidates:
                    if node.quality and node.quality.is_residential and node.quality.risk_score <= 25:
                        valid_residential.append(node)

            # 优选排序打分算法 (得分越低排名越靠前):
            # 基准分 = 风控值 * 10
            # 共享人数权重: 1-10 (+0), 10-100 (+15), 100-1000 (+50), 其余 (+100)
            # 原生 IP 加分: 是原生 (-10), 广播 (+10)
            # 延迟权重: 延迟 ms * 0.1
            for node in valid_residential:
                q = node.quality
                score = q.risk_score * 10.0
                if "1 - 10" in q.shared_users:
                    score += 0.0
                elif "10 - 100" in q.shared_users:
                    score += 15.0
                elif "100 - 1000" in q.shared_users:
                    score += 50.0
                else:
                    score += 100.0

                if q.is_native:
                    score -= 10.0
                else:
                    score += 10.0

                score += node.latency_ms * 0.1
                node.rank_score = round(score, 1)

            # 按得分升序排序
            valid_residential.sort(key=lambda n: n.rank_score)
            picked_nodes = valid_residential[:top_n]

            # 写入 Aimili 收藏
            self.current_step = f"正在将选拔出的 {len(picked_nodes)} 个黄金家宽节点自动加入 Aimili 收藏..."
            if picked_nodes:
                # 若启用覆盖替换模式，先把旧收藏清空（可选）
                if replace_favorites:
                    old_favs = self.aimili.get_current_favorites()
                    logger.info(f"🔄 准备更新 Aimili 收藏池 (当前已有收藏: {len(old_favs)} 个)...")

                success_favs = 0
                for node in picked_nodes:
                    ok = self.aimili.toggle_favorite(node, favorite=True)
                    if ok:
                        success_favs += 1
                        logger.info(f"⭐ 成功加星收藏 -> IP: {node.ip:<15} (延迟: {node.latency_ms}ms, 风控: {node.quality.risk_score}%, 共享: {node.quality.shared_users})")
                    time.sleep(0.1)

                logger.info(f"🎉 优选落盘完成！本次共为 Aimili 写入 {success_favs} 个极度纯净家宽节点")

            # 保存最新结果
            elapsed = round(time.time() - start_time, 1)
            self.last_result = {
                "scan_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "country": country,
                "max_risk_threshold": max_risk,
                "elapsed_seconds": elapsed,
                "total_candidates": total_candidates,
                "qualified_count": len(valid_residential),
                "picked_count": len(picked_nodes),
                "nodes": [
                    {
                        "id": n.id,
                        "ip": n.ip,
                        "port": n.port,
                        "country": n.country,
                        "latency_ms": n.latency_ms,
                        "rank_score": n.rank_score,
                        "ip_type": n.quality.ip_type if n.quality else "",
                        "risk_score": n.quality.risk_score if n.quality else 100,
                        "risk_label": n.quality.risk_label if n.quality else "",
                        "shared_users": n.quality.shared_users if n.quality else "",
                        "is_native": n.quality.is_native if n.quality else False,
                        "isp": n.quality.isp if n.quality else ""
                    }
                    for n in picked_nodes
                ]
            }

            try:
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.last_result, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"❌ 记录历史结果异常: {e}")

            self.current_step = f"优选完成！成功入选 {len(picked_nodes)} 个节点 (耗时 {elapsed}s)"

        except Exception as e:
            logger.exception(f"❌ 执行筛选流程发生未捕获异常: {e}")
            self.current_step = f"执行出错: {e}"
        finally:
            self.is_running = False

        return picked_nodes

# ==============================================================================
# 6. 轻量现代化 Web 控制台 (纯标准库 HTML5 + CSS3 + REST API)
# ==============================================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aimili 纯净家宽优选控制台</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: rgba(30, 41, 59, 0.7);
      --border: rgba(255, 255, 255, 0.1);
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --text: #f8fafc;
      --text-muted: #94a3b8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      background-image: radial-gradient(at 0% 0%, rgba(59, 130, 246, 0.15) 0px, transparent 50%),
                        radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.12) 0px, transparent 50%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      min-height: 100vh;
      padding: 24px;
    }
    .container { max-width: 1100px; margin: 0 auto; }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    .title-box h1 { font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
    .title-box p { font-size: 13px; color: var(--text-muted); margin-top: 4px; }
    .badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 9999px;
      font-size: 11px;
      font-weight: 600;
      background: rgba(59, 130, 246, 0.2);
      color: #60a5fa;
      border: 1px solid rgba(59, 130, 246, 0.3);
    }
    .grid { display: grid; grid-template-columns: 1fr 2fr; gap: 20px; margin-bottom: 24px; }
    @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
    .card {
      background: var(--card-bg);
      backdrop-filter: blur(12px);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px;
      box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
    }
    .card-title { font-size: 15px; font-weight: 600; margin-bottom: 16px; display: flex; align-items: center; gap: 6px; }
    .form-group { margin-bottom: 16px; }
    label { display: block; font-size: 13px; color: var(--text-muted); margin-bottom: 6px; }
    select, input {
      width: 100%;
      background: rgba(15, 23, 42, 0.8);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      padding: 9px 12px;
      font-size: 13px;
      outline: none;
      transition: border-color 0.2s;
    }
    select:focus, input:focus { border-color: var(--accent); }
    .btn-run {
      width: 100%;
      background: linear-gradient(135deg, var(--accent) 0%, #1d4ed8 100%);
      color: white;
      border: none;
      border-radius: 10px;
      padding: 13px;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 0 4px 14px 0 rgba(59, 130, 246, 0.4);
      transition: all 0.2s;
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 8px;
    }
    .btn-run:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 6px 20px 0 rgba(59, 130, 246, 0.5); }
    .btn-run:disabled { opacity: 0.6; cursor: not-allowed; }
    .status-box {
      margin-top: 16px;
      padding: 12px;
      border-radius: 8px;
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid var(--border);
      font-size: 13px;
    }
    .status-line { display: flex; justify-content: space-between; margin-bottom: 6px; }
    .status-text { color: var(--accent); font-weight: 500; }
    .progress-bar-bg { width: 100%; height: 6px; background: rgba(255, 255, 255, 0.1); border-radius: 3px; overflow: hidden; margin-top: 8px; }
    .progress-bar-val { width: 0%; height: 100%; background: var(--accent); transition: width 0.3s; }
    .terminal {
      background: #090d16;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
      color: #38bdf8;
      height: 250px;
      overflow-y: auto;
      line-height: 1.6;
    }
    .log-line { word-break: break-all; white-space: pre-wrap; margin-bottom: 2px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }
    th { padding: 12px 14px; background: rgba(15, 23, 42, 0.8); color: var(--text-muted); font-weight: 600; border-bottom: 1px solid var(--border); }
    td { padding: 12px 14px; border-bottom: 1px solid rgba(255, 255, 255, 0.05); }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }
    .tag-pure { background: rgba(16, 185, 129, 0.2); color: #34d399; padding: 2px 8px; border-radius: 9999px; font-weight: 600; font-size: 11px; }
    .tag-fav { background: rgba(245, 158, 11, 0.2); color: #fbbf24; padding: 2px 8px; border-radius: 9999px; font-weight: 600; font-size: 11px; }
    .tag-home { background: rgba(59, 130, 246, 0.2); color: #60a5fa; padding: 2px 8px; border-radius: 9999px; font-size: 11px; }
  </style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title-box">
      <h1>⭐ Aimili 纯净家宽优选控制台</h1>
      <p>自动探查 Aimili 候选池 · 无需隧道测试 · 批量评估风控与共享画像 · 极速入库收藏</p>
    </div>
    <div>
      <span class="badge">联动模式: 仅用收藏 (Favorites)</span>
    </div>
  </div>

  <div class="grid">
    <!-- 左侧控制面板 -->
    <div class="card">
      <div class="card-title">⚙️ 优选参数配置</div>
      <div class="form-group">
        <label>目标国家 / 地区</label>
        <select id="selCountry">
          <option value="JP" selected>🇯🇵 日本 (JP)</option>
          <option value="US">🇺🇸 美国 (US)</option>
          <option value="SG">🇸🇬 新加坡 (SG)</option>
          <option value="KR">🇰🇷 韩国 (KR)</option>
          <option value="HK">🇭🇰 中国香港 (HK)</option>
          <option value="TW">🇹🇼 中国台湾 (TW)</option>
        </select>
      </div>
      <div class="form-group">
        <label>最大允许风控值 (%)</label>
        <input type="number" id="inpMaxRisk" value="15" min="0" max="100">
        <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">≤ 15% 属于 Ping0 评定之【极度纯净】标准</div>
      </div>
      <div class="form-group">
        <label>期望优选入选数量 (个)</label>
        <input type="number" id="inpTopN" value="5" min="1" max="20">
      </div>
      <button class="btn-run" id="btnStartScan" onclick="triggerScan()">
        <span id="btnIcon">🚀</span> <span id="btnText">开始筛选家宽节点</span>
      </button>

      <div class="status-box">
        <div class="status-line">
          <span>当前状态:</span>
          <span id="lblStatus" class="status-text">空闲就绪</span>
        </div>
        <div class="progress-bar-bg">
          <div id="progBar" class="progress-bar-val"></div>
        </div>
      </div>
    </div>

    <!-- 右侧实时终端日志 -->
    <div class="card">
      <div class="card-title">📜 实时探查与决策日志</div>
      <div class="terminal" id="termLogs"></div>
    </div>
  </div>

  <!-- 下方优选结果表格 -->
  <div class="card">
    <div class="card-title" style="justify-content: space-between;">
      <span>🏆 本次入选黄金家宽节点</span>
      <span style="font-size: 12px; color: var(--text-muted);" id="lblLastScanTime">暂无扫描记录</span>
    </div>
    <div style="overflow-x: auto;">
      <table>
        <thead>
          <tr>
            <th>状态</th>
            <th>IP 地址</th>
            <th>地区/运营商</th>
            <th>宽带类型</th>
            <th>风控评分</th>
            <th>共享人数评估</th>
            <th>原生 IP</th>
            <th>实测延迟</th>
            <th>综合优选分</th>
          </tr>
        </thead>
        <tbody id="tblPickedNodes">
          <tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 30px;">点击左侧【开始筛选家宽节点】按钮启动首次全自动优选</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
  let isScanning = false;

  async function fetchStatus() {
    try {
      const res = await fetch('./api/status');
      const data = await res.json();
      isScanning = data.running;

      document.getElementById('lblStatus').textContent = data.current_step || (isScanning ? '正在扫描...' : '空闲就绪');
      document.getElementById('btnStartScan').disabled = isScanning;
      document.getElementById('btnText').textContent = isScanning ? '优选中...' : '开始筛选家宽节点';
      document.getElementById('btnIcon').textContent = isScanning ? '⏳' : '🚀';
      document.getElementById('progBar').style.width = isScanning ? '70%' : (data.last_result?.nodes ? '100%' : '0%');

      if (data.last_result && data.last_result.scan_time) {
        document.getElementById('lblLastScanTime').textContent = `最后优选时间: ${data.last_result.scan_time} · 耗时 ${data.last_result.elapsed_seconds || 0}s`;
        renderTable(data.last_result.nodes || []);
      }
    } catch (e) {
      console.error(e);
    }
  }

  async function fetchLogs() {
    try {
      const res = await fetch('./api/logs');
      const logs = await res.json();
      const term = document.getElementById('termLogs');
      term.innerHTML = logs.map(l => `<div class="log-line">${escapeHtml(l)}</div>`).join('');
      term.scrollTop = term.scrollHeight;
    } catch (e) {}
  }

  function renderTable(nodes) {
    const tbody = document.getElementById('tblPickedNodes');
    if (!nodes || nodes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 30px;">未找到符合阈值的节点</td></tr>';
      return;
    }
    tbody.innerHTML = nodes.map(n => `
      <tr>
        <td><span class="tag-fav">⭐ 已入库</span></td>
        <td style="font-weight: 600; font-family: monospace;">${n.ip}</td>
        <td>${n.country} · ${escapeHtml(n.isp || '本地ISP')}</td>
        <td><span class="tag-home">${escapeHtml(n.ip_type)}</span></td>
        <td><span class="tag-pure">${n.risk_score}% (${n.risk_label || '极度纯净'})</span></td>
        <td>${escapeHtml(n.shared_users || '未知')}</td>
        <td>${n.is_native ? '✅ 原生' : '广播'}</td>
        <td>${n.latency_ms} ms</td>
        <td style="font-weight: 600; color: #38bdf8;">${n.rank_score}</td>
      </tr>
    `).join('');
  }

  async function triggerScan() {
    const country = document.getElementById('selCountry').value;
    const maxRisk = parseInt(document.getElementById('inpMaxRisk').value) || 15;
    const topN = parseInt(document.getElementById('inpTopN').value) || 5;

    try {
      await fetch('./api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ country, max_risk: maxRisk, top_n: topN })
      });
      fetchStatus();
      fetchLogs();
    } catch (e) {
      alert('启动筛选请求失败: ' + e);
    }
  }

  function escapeHtml(str) {
    return (str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  setInterval(fetchStatus, 2000);
  setInterval(fetchLogs, 1500);
  fetchStatus();
  fetchLogs();
</script>
</body>
</html>
"""

class WebRequestHandler(http.server.BaseHTTPRequestHandler):
    """自包含 HTTP 请求处理器"""

    engine: Optional[PickerEngine] = None

    def log_message(self, format, *args):
        # 静默常规 Web 访问日志，保持控制台整洁
        pass

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/" or url.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
        elif url.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            status_data = {
                "running": self.engine.is_running if self.engine else False,
                "current_step": self.engine.current_step if self.engine else "未就绪",
                "last_result": self.engine.last_result if self.engine else {}
            }
            self.wfile.write(json.dumps(status_data, ensure_ascii=False).encode("utf-8"))
        elif url.path == "/api/logs":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            logs = ring_handler.get_logs()
            self.wfile.write(json.dumps(logs, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/api/scan":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            params = json.loads(body) if body else {}

            country = params.get("country", "JP")
            max_risk = int(params.get("max_risk", 15))
            top_n = int(params.get("top_n", 5))

            if self.engine and not self.engine.is_running:
                threading.Thread(
                    target=self.engine.run_screening,
                    kwargs={"country": country, "max_risk": max_risk, "top_n": top_n},
                    daemon=True
                ).start()
                resp = {"ok": True, "message": "优选任务已启动"}
            else:
                resp = {"ok": False, "message": "任务正在执行中，请勿重复触发"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

# ==============================================================================
# 7. 主程序入口与定时任务调度
# ==============================================================================

def cron_worker(engine: PickerEngine, interval_hours: int, country: str, max_risk: int, top_n: int):
    """后台定时巡检任务"""
    logger.info(f"⏰ 定时巡检线程已就绪: 每隔 {interval_hours} 小时全自动重新优选入库一次")
    while True:
        time.sleep(interval_hours * 3600)
        try:
            logger.info("⏰ 触发定时自动优选任务...")
            engine.run_screening(country=country, max_risk=max_risk, top_n=top_n)
        except Exception as e:
            logger.error(f"❌ 定时巡检异常: {e}")

def main():
    parser = argparse.ArgumentParser(description="AimiliVPN 智能纯净家宽节点优选器")
    parser.add_argument("--port", type=int, default=8585, help="Web 控制台服务端口 (默认 8585)")
    parser.add_argument("--country", type=str, default="JP", help="优选地区代码 (默认 JP)")
    parser.add_argument("--max-risk", type=int, default=15, help="最大允许风控分 (默认 15)")
    parser.add_argument("--top-n", type=int, default=5, help="优选收藏入库节点数 (默认 5)")
    parser.add_argument("--interval", type=int, default=6, help="定时自动扫描间隔小时 (默认 6 小时)")
    parser.add_argument("--scan-once", action="store_true", help="仅执行一次筛选并立刻退出 (适用于系统 crontab)")
    args = parser.parse_args()

    aimili = AimiliClient()
    inspector = IpIntelligenceManager()
    engine = PickerEngine(aimili=aimili, inspector=inspector)

    # 1. 单次执行模式
    if args.scan_once:
        logger.info("🎯 运行于单次筛选模式 (--scan-once)")
        picked = engine.run_screening(country=args.country, max_risk=args.max_risk, top_n=args.top_n)
        print("\n" + "=" * 60)
        print(f"🎉 筛选完毕！入选节点列表 ({len(picked)} 个):")
        print("=" * 60)
        for idx, n in enumerate(picked, 1):
            q = n.quality
            print(f"[{idx}] IP: {n.ip:<15} | 延迟: {n.latency_ms:>3}ms | 风控: {q.risk_score:>2}% ({q.risk_label}) | 共享: {q.shared_users:<12} | 原生: {q.is_native}")
        print("=" * 60 + "\n")
        sys.exit(0)

    # 2. Web 服务 + 定时后台巡检模式
    WebRequestHandler.engine = engine

    # 启动后台定时任务
    if args.interval > 0:
        threading.Thread(
            target=cron_worker,
            args=(engine, args.interval, args.country, args.max_risk, args.top_n),
            daemon=True
        ).start()

    server = ThreadedHTTPServer(("0.0.0.0", args.port), WebRequestHandler)
    logger.info(f"🌐 优选 Web 控制台已启动，监听地址: http://0.0.0.0:{args.port}")
    logger.info(f"👉 浏览器访问: http://<你的VPS公网IP>:{args.port} (如果绑定了域名也可通过域名加端口访问)")
    logger.info("💡 提示: 请确认云平台防火墙 (如 GCP 防火墙) 已放行该端口 (TCP " + str(args.port) + ")")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("👋 服务已正常停止")

if __name__ == "__main__":
    main()
