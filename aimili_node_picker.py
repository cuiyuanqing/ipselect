#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AimiliVPN 智能纯净家宽节点优选器 (Aimili Pure Node Picker v3.5)
===========================================================
核心升级特性：
  1. 带宽速率硬性过滤门槛：自动识别 Aimili 测速数据，剔除 < 10 Mbps 的小水管节点；
  2. 多通道备用质量评估矩阵 (Multi-Source Quality Assessment & Failover)：
     - 主通道：Ping0.cc（高精度风控分、使用人数、原生标签）；
     - 备选通道 1：IP-API（极速免封，精准识别住宅 ISP 与机房 IDC 属性）；
     - 备选通道 2：IPWho.is（国际威胁情报，全面校验 VPN/Hosting/Proxy）；
     - 支持智能自动熔断降级 (Auto Failover) 与极速直通备选源模式；
  3. 优先验身与继承存量收藏：每次优选先复检当前已有收藏，合格节点直接保留，无需重复全量清空；
  4. 节点经验与信誉成长机制 (Experience & Reputation)：
     - 为每个节点建立专属成长档案，记录连续达标轮次、受检次数与信誉评分；
     - 徽章段位成长：Lv.1 新秀 -> Lv.2 稳态家宽 -> Lv.3 骨干老兵 -> Lv.4 传奇标杆；
     - 高段位老兵节点享有优先优选加权与轻度网络抖动保护机制；
  5. 故障/超标节点精准淘汰与差额补位：
     - 失效离线、风控超标或速率不达标节点自动精准移出收藏；
     - 从在库候选池中按需挑选最佳新生力量补足目标配额（如 20 个）；
  6. 双重持久化同步：同时保证 Aimili 内存与本地底层 ui_auth.json 强一致性；
  7. 现代化自包含 Web 仪表盘与定时自动巡检服务（零第三方依赖，纯 Python 标准库）。

作者：Jeff Smith 专属定制
"""

import argparse
import http.cookiejar
import http.server
import json
import logging
import os
import random
import re
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ==============================================================================
# 1. 日志与基础配置
# ==============================================================================

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

class RingLogHandler(logging.Handler):
    """内存日志环形缓冲区，供 Web 界面实时展示最近日志"""
    def __init__(self, capacity: int = 350):
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

# 确保标准输出与标准错误流在任何系统环境下(包含 Windows 与 Linux)统一使用 UTF-8 编码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 控制台输出
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
logger.addHandler(console_handler)

# 内存日志输出
ring_handler = RingLogHandler(capacity=350)
ring_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
logger.addHandler(ring_handler)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "picker_data")
os.makedirs(DATA_DIR, exist_ok=True)

CACHE_FILE = os.path.join(DATA_DIR, "ip_intel_cache.json")
CONFIG_FILE = os.path.join(DATA_DIR, "picker_config.json")
HISTORY_FILE = os.path.join(DATA_DIR, "latest_picked_nodes.json")
EXPERIENCE_FILE = os.path.join(DATA_DIR, "node_experience.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "ip_blacklist.json")

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
    ip_type: str = "未知"          # e.g., "家庭宽带 IP", "数据中心 IDC"
    is_residential: bool = False
    risk_score: int = 100         # 0 ~ 100
    risk_label: str = "未知"       # e.g., "极度纯净", "低风险", "高风险"
    is_native: bool = False       # 原生 IP
    shared_users: str = "未知"    # e.g., "1 - 10 (极好)", "10 - 100 (一般)"
    updated_at: float = 0.0

@dataclass
class NodeExperience:
    ip: str
    canonical_id: str = ""
    isp: str = ""
    first_seen: str = ""
    last_tested: str = ""
    consecutive_passes: int = 0      # 连续达标轮次 (经验值成长核心)
    total_inspections: int = 0       # 累计受检次数
    total_passes: int = 0            # 累计合格次数
    total_fails: int = 0             # 累计不合格次数
    reputation_score: float = 80.0   # 综合信誉分 (0 ~ 100)
    last_risk_score: int = 100
    last_latency_ms: int = 999
    status: str = "candidate"        # favorited / candidate / demoted

    @property
    def level_label(self) -> str:
        """根据连续达标轮次生成段位徽章"""
        cp = self.consecutive_passes
        if cp >= 10:
            return "🏅 Lv.4 传奇标杆"
        elif cp >= 5:
            return "🎖️ Lv.3 骨干老兵"
        elif cp >= 2:
            return "⭐ Lv.2 稳态家宽"
        elif cp == 1:
            return "🌱 Lv.1 新晋新秀"
        else:
            return "⚠️ 观察中"

@dataclass
class CandidateNode:
    id: str
    ip: str
    port: int
    country: str
    protocol: str
    latency_ms: int
    speed_bps: int = 0
    speed_mbps: float = 0.0
    pool_endpoint_id: str = ""
    aliases: List[str] = field(default_factory=list)
    quality: Optional[IpQuality] = None
    experience: Optional[NodeExperience] = None
    is_inherited: bool = False
    rank_score: float = 9999.0
    handshake_ok: bool = False
    real_latency_ms: Optional[int] = None

    @property
    def canonical_fav_id(self) -> str:
        """Aimili 规范收藏 ID: 带有 pool_endpoint_id 则必须加 pool: 前缀"""
        if self.pool_endpoint_id:
            return f"pool:{self.pool_endpoint_id}"
        return str(self.id)

# ==============================================================================
# 3. 节点经验与信誉成长档案管理器 (Experience Manager)
# ==============================================================================

class NodeExperienceManager:
    """管理收藏节点的成长履历、连续达标轮次与信誉评分"""

    def __init__(self, exp_file: str = EXPERIENCE_FILE):
        self.exp_file = exp_file
        self.records: Dict[str, NodeExperience] = {}
        self._load()

    def _load(self):
        if os.path.isfile(self.exp_file):
            try:
                with open(self.exp_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for ip, entry in data.items():
                        self.records[ip] = NodeExperience(**entry)
                    logger.info(f"📜 成功加载节点信誉与经验成长档案共 {len(self.records)} 条")
            except Exception as e:
                logger.warning(f"⚠️ 读取节点经验档案异常: {e}")

    def save(self):
        try:
            with open(self.exp_file, "w", encoding="utf-8") as f:
                dump_data = {ip: asdict(exp) for ip, exp in self.records.items()}
                json.dump(dump_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ 保存节点经验档案异常: {e}")

    def get_or_create(self, ip: str, canonical_id: str = "", isp: str = "") -> NodeExperience:
        if ip in self.records:
            rec = self.records[ip]
            if canonical_id and not rec.canonical_id:
                rec.canonical_id = canonical_id
            if isp and not rec.isp:
                rec.isp = isp
            return rec
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        rec = NodeExperience(
            ip=ip,
            canonical_id=canonical_id,
            isp=isp,
            first_seen=now_str,
            last_tested=now_str
        )
        self.records[ip] = rec
        return rec

    def record_pass(self, ip: str, canonical_id: str, isp: str, risk_score: int, latency_ms: int) -> NodeExperience:
        """记录一次合格达标"""
        rec = self.get_or_create(ip, canonical_id, isp)
        rec.last_tested = time.strftime("%Y-%m-%d %H:%M:%S")
        rec.total_inspections += 1
        rec.total_passes += 1
        rec.consecutive_passes += 1
        rec.last_risk_score = risk_score
        rec.last_latency_ms = latency_ms
        rec.status = "favorited"

        bonus = max(0.0, (15.0 - risk_score) * 0.4) + (1.0 if latency_ms < 60 else 0.0)
        rec.reputation_score = min(100.0, round(rec.reputation_score + 1.5 + bonus, 1))
        self.save()
        return rec

    def record_fail(self, ip: str, reason: str = "") -> NodeExperience:
        """记录一次失效或风控超标淘汰"""
        rec = self.get_or_create(ip)
        rec.last_tested = time.strftime("%Y-%m-%d %H:%M:%S")
        rec.total_inspections += 1
        rec.total_fails += 1
        rec.consecutive_passes = 0
        rec.status = "demoted"
        rec.reputation_score = max(0.0, round(rec.reputation_score - 15.0, 1))
        self.save()
        return rec

    def get_experience_bonus(self, ip: str) -> float:
        """计算候选节点基于历史经验的排名加分（得分越低排名越靠前）"""
        if ip not in self.records:
            return 0.0
        rec = self.records[ip]
        pass_bonus = min(35.0, rec.consecutive_passes * 5.0)
        rep_bonus = (rec.reputation_score - 80.0) * 0.4 if rec.reputation_score > 80.0 else 0.0
        return pass_bonus + rep_bonus

# ==============================================================================
# ==============================================================================
# 3.5. IP 永久黑名单管理器 (Blacklist Manager)
# ==============================================================================

class BlacklistManager:
    """管理永久黑名单 IP，对风控>=10%或违规节点永久拉黑，永不录用"""

    def __init__(self, blacklist_file: str = BLACKLIST_FILE):
        self.blacklist_file = blacklist_file
        self.records: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if os.path.isfile(self.blacklist_file):
            try:
                with open(self.blacklist_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.records = data.get("ips", {})
                    logger.info(f"🚫 成功加载 IP 永久黑名单库共 {len(self.records)} 条")
            except Exception as e:
                logger.warning(f"⚠️ 读取黑名单文件异常: {e}")

    def save(self):
        try:
            with open(self.blacklist_file, "w", encoding="utf-8") as f:
                json.dump({
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "count": len(self.records),
                    "ips": self.records
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ 保存黑名单文件异常: {e}")

    def is_blacklisted(self, ip: str) -> bool:
        return ip in self.records

    def add(self, ip: str, reason: str, risk_score: int = 100, isp: str = ""):
        if ip not in self.records:
            self.records[ip] = {
                "reason": reason,
                "risk_score": risk_score,
                "isp": isp,
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            self.save()
            logger.warning(f"🚫 [永久拉黑] 节点 IP {ip:<15} (风控 {risk_score}%) 已永久拉黑: {reason}")

    def remove(self, ip: str) -> bool:
        if ip in self.records:
            del self.records[ip]
            self.save()
            return True
        return False

    def get_all(self) -> Dict[str, Any]:
        return dict(self.records)

    @property
    def count(self) -> int:
        return len(self.records)

# ==============================================================================
# 4. AimiliVPN 本地客户端 (与 127.0.0.1:8501 交互)
# ==============================================================================

class AimiliClient:
    """与本地运行的 AimiliVPN 管理后端进行通信与鉴权"""

    def __init__(
        self,
        auth_file: str = "/opt/aimilivpn/vpngate_data/ui_auth.json",
        username: Optional[str] = None,
        password: Optional[str] = None,
        secret_path: Optional[str] = None,
        port: Optional[int] = None,
        proxy_port: Optional[int] = None
    ):
        self.auth_file = auth_file
        self.host = "127.0.0.1"
        self.port = port or 8501
        self.proxy_port = proxy_port or 7928
        self.username = username or "admin"
        self.password = password or ""
        self.secret_path = secret_path or ""
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.logged_in = False
        self._working_base_url: Optional[str] = None

        self._load_auth(username=username, password=password, secret_path=secret_path, port=port, proxy_port=proxy_port)

    def _load_auth(self, username=None, password=None, secret_path=None, port=None, proxy_port=None):
        """读取本地 Aimili 密码与配置"""
        if os.path.isfile(self.auth_file):
            try:
                with open(self.auth_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.host = data.get("host", "127.0.0.1")
                    if not port:
                        self.port = int(data.get("port", 8501))
                    if not proxy_port:
                        self.proxy_port = int(data.get("proxy_port", 7928))
                    if not username:
                        self.username = data.get("username", "admin")
                    if not password:
                        self.password = data.get("password", "")
                    if not secret_path:
                        self.secret_path = data.get("secret_path", "")
                    logger.info(f"🔑 成功从 [{self.auth_file}] 加载 Aimili 认证凭据 (用户: {self.username}, 端口: {self.port}, 代理端口: {self.proxy_port}, 安全后缀: {self.secret_path})")
                    return
            except Exception as e:
                logger.warning(f"⚠️ 解析 {self.auth_file} 失败: {e}，将使用命令行/默认参数")
        else:
            logger.warning(f"⚠️ 未找到 {self.auth_file}，使用参数配置: http://{self.host}:{self.port}")

    @property
    def base_url(self) -> str:
        if self._working_base_url:
            return self._working_base_url
        s = (self.secret_path or "").strip("/")
        if s:
            return f"http://{self.host}:{self.port}/{s}"
        return f"http://{self.host}:{self.port}"

    def login(self) -> bool:
        """登录 Aimili 获取会话 Cookie，支持智能探测正确 API 路径"""
        candidates_urls = []
        s = (self.secret_path or "").strip("/")
        if s:
            candidates_urls.append(f"http://{self.host}:{self.port}/{s}/api/login")
        candidates_urls.append(f"http://{self.host}:{self.port}/api/login")

        payload = json.dumps({"username": self.username, "password": self.password}).encode("utf-8")

        for url in candidates_urls:
            logger.info(f"🔐 尝试向 Aimili 发起登录请求: {url} (用户名: {self.username})")
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
                        self._working_base_url = url.replace("/api/login", "")
                        logger.info(f"✅ 成功登录 AimiliVPN 本地管理服务 (已锁定基础路径: {self._working_base_url})")
                        return True
                    else:
                        logger.error(f"❌ 登录 AimiliVPN 失败 (账号或密码错误): {res_data.get('error', '未知错误')}")
                        return False
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    logger.debug(f"ℹ️ 路径 {url} 返回 404，尝试下一个候选路径...")
                    continue
                logger.error(f"❌ 登录 AimiliVPN 返回 HTTP {e.code}: {e.reason}")
            except Exception as e:
                logger.error(f"❌ 请求 AimiliVPN 登录接口失败 [{url}]: {e}")

        logger.error(f"❌ 所有登录路径尝试完毕，均未能成功连接到 Aimili 后端 (请确认端口 {self.port} 及安全路径 {self.secret_path} 是否正确)")
        return False

    def fetch_scoped_nodes(self, country: str = "JP", status: str = "available", limit: int = 150) -> List[CandidateNode]:
        """从 Aimili 提取指定国家和状态的候选节点（包含实测带宽数据）"""
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
                    pool_id = str(n.get("pool_endpoint_id") or "")
                    aliases = [nid]
                    if pool_id:
                        aliases.extend([pool_id, f"pool:{pool_id}"])
                    aliases.append(nid.replace("pool:", ""))
                    aliases = list(set([a for a in aliases if a]))

                    # 提取带宽数据 (Aimili speed/speed_bps 单位为 bps)
                    speed_bps = int(n.get("speed_bps") or n.get("speed") or 0)
                    speed_mbps = round(speed_bps / 1e6, 1)

                    candidates.append(CandidateNode(
                        id=nid,
                        ip=ip,
                        port=int(n.get("port") or 1194),
                        country=n.get("country", country),
                        protocol=n.get("protocol", "openvpn"),
                        latency_ms=int(n.get("latency") or 999),
                        speed_bps=speed_bps,
                        speed_mbps=speed_mbps,
                        pool_endpoint_id=pool_id,
                        aliases=aliases
                    ))
                logger.info(f"📡 从 Aimili 获取到 [{country}] 地区存活候选节点共 {len(candidates)} 个")
                return candidates
        except Exception as e:
            logger.error(f"❌ 拉取 Aimili 节点列表异常: {e}")
            return []

    def toggle_favorite(self, node: CandidateNode, favorite: bool = True, timeout: int = 30) -> bool:
        """调用 Aimili 的收藏切换接口 (timeout 延长至 30s，包容 Aimili 内部同步网络测活)"""
        if not self.logged_in and not self.login():
            return False

        url = f"{self.base_url}/api/toggle_favorite"
        fav_id = node.canonical_fav_id
        aliases = list(set([node.id, fav_id, node.pool_endpoint_id] + node.aliases))
        aliases = [a for a in aliases if a]

        payload = json.dumps({
            "id": fav_id,
            "favorite": favorite,
            "aliases": aliases
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "AimiliPurePicker/1.0"}
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return bool(res.get("ok"))
        except Exception as e:
            logger.error(f"❌ 收藏节点 [{node.ip}] (ID: {fav_id}) 异常: {e}")
            return False

    def get_state(self) -> Dict[str, Any]:
        """获取 Aimili 当前网关与连接运行状态"""
        if not self.logged_in and not self.login():
            return {}
        url = f"{self.base_url}/api/nodes?limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "AimiliPurePicker/1.0"})
        try:
            with self.opener.open(req, timeout=5) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return res.get("state", {})
        except Exception as e:
            logger.debug(f"⚠️ 获取 Aimili 状态异常: {e}")
            return {}

    def wait_for_idle(self, timeout: float = 12.0) -> bool:
        """等待 Aimili 连接或维护任务释放锁，恢复空闲就绪状态"""
        start = time.time()
        while time.time() - start < timeout:
            st = self.get_state()
            if not st:
                time.sleep(0.5)
                continue
            is_connecting = st.get("is_connecting", False)
            maintenance = st.get("maintenance_running", False)
            if not is_connecting and not maintenance:
                return True
            time.sleep(0.8)
        return False

    def wait_for_tunnel_ready(self, expected_node_id: str, timeout: float = 10.0) -> bool:
        """等待 Aimili 成功建立 OpenVPN 隧道与本地代理通道"""
        clean_id = expected_node_id.replace("pool:", "")
        start = time.time()
        while time.time() - start < timeout:
            st = self.get_state()
            if not st:
                time.sleep(0.5)
                continue
            if st.get("is_connecting"):
                time.sleep(0.8)
                continue
            active_id = str(st.get("active_openvpn_node_id") or "")
            # 严格握手验证：必须是 OpenVPN 隧道真正就绪，绝不能被本地 proxy_ready 敷衍
            tunnel_ready = bool(st.get("tunnel_ready"))
            if tunnel_ready and (clean_id in active_id or active_id in clean_id or not clean_id):
                return True
            if not st.get("is_connecting") and not tunnel_ready:
                msg = st.get("last_check_message", "")
                logger.warning(f"⚠️ 节点 {expected_node_id} 隧道建连失败: {msg}")
                return False
            time.sleep(0.8)
        return False

    def set_active_node(self, node: CandidateNode, timeout: int = 15, max_retries: int = 3) -> bool:
        """调用 Aimili 将指定优选节点切换为当前活动出口节点，带 409 互斥锁感知与重试"""
        if not self.logged_in and not self.login():
            return False

        # 1. 尝试前确保 Aimili 不在其他并发连接锁中
        self.wait_for_idle(timeout=6.0)

        url = f"{self.base_url}/api/connect"
        clean_id = node.id.replace("pool:", "")
        cand_ids = [clean_id, node.id, f"pool:{clean_id}"]
        seen = set()
        unique_ids = [x for x in cand_ids if x and not (x in seen or seen.add(x))]

        for attempt in range(1, max_retries + 1):
            for test_id in unique_ids:
                payload = json.dumps({"id": test_id}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "AimiliPurePicker/1.0"}
                )
                try:
                    with self.opener.open(req, timeout=timeout) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                        ok = bool(res.get("ok") or res.get("running") or "state" in res)
                        if ok:
                            logger.info(f"🚀 成功向 Aimili 下发节点切换指令 -> 活动出口: {node.ip} (ID: {test_id})")
                            ready = self.wait_for_tunnel_ready(test_id, timeout=8.0)
                            if ready:
                                return True
                            else:
                                logger.warning(f"⚠️ 节点 {node.ip} 握手未就绪或建连超时")
                                return False
                        else:
                            logger.warning(f"⚠️ 切换节点返回异常: {res.get('message', res)}")
                except urllib.error.HTTPError as e:
                    if e.code == 409:
                        logger.warning(f"⏳ Aimili 正在执行前序建连 (HTTP 409 互斥)，等待 2.5 秒后重试 (第 {attempt}/{max_retries} 次)...")
                        time.sleep(2.5)
                        self.wait_for_idle(timeout=6.0)
                        break
                    else:
                        logger.warning(f"⚠️ 尝试使用 ID [{test_id}] 切换返回 HTTP {e.code}: {e.reason}")
                except Exception as e:
                    logger.warning(f"⚠️ 请求 Aimili 切换接口异常 [{test_id}]: {e}")

        logger.error(f"❌ 最终未能成功将活动节点切换到 [{node.ip}]")
        return False

    def remove_favorite_by_id(self, fav_id: str, timeout: int = 3) -> bool:
        """移除指定收藏 ID (Aimili 在 favorite=False 时无需网络测活，毫秒级极速返回)"""
        if not self.logged_in and not self.login():
            return False

        url = f"{self.base_url}/api/toggle_favorite"
        clean_id = fav_id.replace("pool:", "")
        aliases = [fav_id, clean_id, f"pool:{clean_id}"]
        aliases = list(set([a for a in aliases if a]))

        payload = json.dumps({
            "id": fav_id,
            "favorite": False,
            "aliases": aliases
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "AimiliPurePicker/1.0"}
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return bool(res.get("ok"))
        except Exception as e:
            logger.warning(f"⚠️ 移除旧收藏 [{fav_id}] 异常: {e}")
            return False

    def refresh_nodes(self, country: str = "JP", timeout: int = 15) -> bool:
        """向 Aimili 发送节点刷新指令，触发后台从 VPNGate 发现新节点 (带 409 互斥避让)"""
        if not self.logged_in and not self.login():
            return False
        self.wait_for_idle(timeout=8.0)
        url = f"{self.base_url}/api/refresh_nodes"
        payload = json.dumps({"discovery_countries": [country]}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "AimiliPurePicker/1.0"}
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return bool(res.get("ok"))
        except Exception as e:
            logger.warning(f"⚠️ 触发 Aimili 刷新节点库异常: {e}")
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

    def sync_auth_file_favorites(self, fav_ids: List[str]):
        """直接更新本地 ui_auth.json 文件中的 favorite_node_ids，实现底层配置双重持久化"""
        if not os.path.isfile(self.auth_file):
            return
        try:
            with open(self.auth_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["favorite_node_ids"] = fav_ids
            tmp_file = self.auth_file + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.auth_file)
            logger.info(f"💾 已同步将 {len(fav_ids)} 个节点 ID 写入底层文件 {self.auth_file}")
        except Exception as e:
            logger.warning(f"⚠️ 同步底层 {self.auth_file} 异常: {e}")

# ==============================================================================
# 5. IP 质量与风控情报检测器 (多通道: Ping0 + IP-API + IPWho.is 智能熔断矩阵)
# ==============================================================================

class IpIntelligenceManager:
    """负责并发查询各目标 IP 的风控分、家庭宽带属性与共享人数，带防反爬自愈与多备选源降级矩阵"""

    RESIDENTIAL_ISP_KEYWORDS = [
        "sony", "so-net", "tokai", "kddi", "softbank", "ocn", "ntt",
        "optage", "jcom", "asahi", "plala", "u-next", "biglobe",
        "comcast", "spectrum", "at&t", "verizon", "charter", "cox",
        "singtel", "starhub", "myrepublic", "chunghwa", "hkt", "hkbn", "kt", "sk broadband", "lgu+"
    ]

    def __init__(self, cache_file: str = CACHE_FILE, ttl_hours: int = 24):
        self.cache_file = cache_file
        self.ttl_seconds = ttl_hours * 3600
        self.cache: Dict[str, Dict[str, Any]] = {}
        self._cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._cookie_jar))
        self._ping0_block_count: int = 0
        self._ping0_circuit_broken: bool = False
        self._load_cache()

    def reset_circuit_breaker(self):
        """重置熔断器状态，供新一轮扫描开始时调用"""
        self._ping0_block_count = 0
        self._ping0_circuit_broken = False

    def _load_cache(self):
        if os.path.isfile(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    raw_cache = json.load(f)
                    clean = {}
                    for ip, entry in raw_cache.items():
                        if entry.get("ip_type") != "未知宽带类型" and entry.get("risk_label") not in ["未知", "检测超时", "反爬拦截"]:
                            clean[ip] = entry
                    self.cache = clean
                    logger.info(f"📂 成功加载 IP 情报有效缓存共 {len(self.cache)} 条 (已自动净化历史异常条目)")
            except Exception as e:
                logger.warning(f"⚠️ 读取 IP 缓存失败: {e}")

    def _save_cache(self):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ 保存 IP 缓存异常: {e}")

    def inspect_ip(self, ip: str, source_mode: str = "auto") -> IpQuality:
        """
        查询单个 IP 的风控质量，支持多源模式：
          - auto: 智能自动降级 (Ping0 优先 -> 熔断直通 / IP-API 兜底 -> IPWho.is 补偿)
          - fallback_only: 极速备选源通道 (仅走 IP-API + IPWho.is，完全规避 Ping0 频控)
          - ping0_only: 严格主通道 (仅走 Ping0.cc)
        """
        now = time.time()
        cached = self.cache.get(ip)
        if cached and (now - cached.get("updated_at", 0) < self.ttl_seconds):
            if cached.get("ip_type") != "未知宽带类型" and cached.get("risk_label") not in ["未知", "检测超时", "反爬拦截"]:
                logger.debug(f"⚡ [Cache] 命中 IP {ip} 缓存数据")
                return IpQuality(**cached)

        quality = IpQuality(ip=ip, updated_at=time.time())

        if source_mode == "fallback_only":
            # 极速备用源模式
            quality = self._query_ip_api(ip, quality)
            if quality.ip_type == "未知宽带类型":
                quality = self._query_ipwhois(ip, quality)
        elif source_mode == "ping0_only":
            # 仅主数据源
            quality = self._query_ping0_with_retry(ip)
        else:
            # 智能自动融合降级模式 (auto)
            if self._ping0_circuit_broken:
                # 熔断直通：避免每节点耗时 6 秒等待无效的 Ping0 反爬重试
                logger.debug(f"⚡ [熔断直通] IP {ip} 直通备选源 (IP-API)...")
                quality = self._query_ip_api(ip, quality)
                if quality.risk_label in ["未知", "反爬拦截", "检测超时"] or quality.ip_type == "未知宽带类型":
                    quality = self._query_ipwhois(ip, quality)
            else:
                quality = self._query_ping0_with_retry(ip)
                if quality.risk_label in ["未知", "反爬拦截", "检测超时"] or quality.ip_type == "未知宽带类型":
                    logger.info(f"🔄 触发自动降级：无缝切换备选接口 1 (IP-API) 复检 IP: {ip}...")
                    quality = self._query_ip_api(ip, quality)
                    if quality.risk_label in ["未知", "反爬拦截", "检测超时"] or quality.ip_type == "未知宽带类型":
                        logger.info(f"🔄 触发二级自动降级：切换备选接口 2 (IPWho.is) 复检 IP: {ip}...")
                        quality = self._query_ipwhois(ip, quality)

        # 仅当结果为有效数据时才写入持久化缓存
        if quality.ip_type != "未知宽带类型" and quality.risk_label not in ["未知", "检测超时", "反爬拦截"]:
            self.cache[ip] = asdict(quality)
            self._save_cache()

        return quality

    def _query_ping0_with_retry(self, ip: str, max_retries: int = 2) -> IpQuality:
        """带重试保护的 Ping0 查询与连续失败智能熔断"""
        quality = IpQuality(ip=ip, updated_at=time.time())
        for attempt in range(1, max_retries + 1):
            q, ok = self._query_ping0(ip)
            if ok:
                self._ping0_block_count = 0  # 恢复正常，计数器归零
                return q
            quality = q
            self._ping0_block_count += 1
            if self._ping0_block_count >= 2:
                self._ping0_circuit_broken = True
                logger.warning("🚨 [Ping0 智能熔断激活] 检测到 Ping0 连续返回拦截或频控，已激活智能熔断直通模式！本轮剩余节点将直接走 IP-API + IPWhois 高速通道，避免无效等待。")
                break
            if attempt < max_retries:
                backoff = 3.0 * attempt
                logger.warning(f"⚠️ [Ping0] IP {ip} 触发反爬防护或频控，等待 {backoff:.1f} 秒后重试 (第 {attempt}/{max_retries} 次)...")
                time.sleep(backoff)
        return quality

    def _query_ping0(self, ip: str) -> Tuple[IpQuality, bool]:
        """直接解析 Ping0 的纯净度、家宽属性与共享人数数据"""
        url = f"https://ping0.cc/ip/{ip}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
            "Referer": "https://ping0.cc/",
            "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1"
        }
        quality = IpQuality(ip=ip, updated_at=time.time())

        try:
            req = urllib.request.Request(url, headers=headers)
            with self._opener.open(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            if len(html) < 15000 or any(k in html for k in ["Just a moment...", "429 Too Many Requests", "访问过于频繁", "cf-turnstile", "人机验证"]):
                logger.warning(f"⚠️ [Ping0] IP {ip} 响应疑似被反爬或频控拦截 (长度: {len(html)} 字节)")
                quality.risk_label = "反爬拦截"
                return quality, False

            # 1. 提取风控值与标签
            risk_match = re.search(r'class="riskitem riskcurrent"[^>]*><span class="value">(\d+)%</span><span class="lab">\s*([^<]+)</span>', html)
            if risk_match:
                quality.risk_score = int(risk_match.group(1))
                quality.risk_label = risk_match.group(2).strip()
            else:
                pct_match = re.search(r'class="value">(\d+)%</span>', html)
                if pct_match:
                    quality.risk_score = int(pct_match.group(1))
                    quality.risk_label = "自检估算"

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
            usecount_match = re.search(r'usecount="([^"]+)"', html)
            if usecount_match:
                quality.shared_users = usecount_match.group(1).strip()
            else:
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

            if quality.ip_type != "未知宽带类型" and quality.risk_label not in ["未知", "反爬拦截"]:
                logger.info(f"🔍 [Ping0] IP {ip:<15} -> 类型: {quality.ip_type:<10} | 风控: {quality.risk_score}% ({quality.risk_label}) | 共享人数: {quality.shared_users} | 原生: {quality.is_native}")
                return quality, True

        except Exception as e:
            logger.warning(f"⚠️ 查询 Ping0 [{ip}] 异常: {e}")
            quality.risk_label = "检测超时"

        return quality, False

    def _query_ip_api(self, ip: str, fallback_quality: IpQuality) -> IpQuality:
        """备选通道 1: 通过 ip-api.com 高速精准识别住宅 ISP 与机房属性"""
        url = f"http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,regionName,city,isp,org,as,mobile,proxy,hosting"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("status") == "success":
                    hosting = data.get("hosting", False)
                    isp = data.get("isp", "")
                    org = data.get("org", "")
                    asn = data.get("as", "")
                    country = data.get("countryCode", "")

                    fallback_quality.ip = ip
                    fallback_quality.isp = isp
                    fallback_quality.asn = asn
                    fallback_quality.country = country
                    fallback_quality.updated_at = time.time()

                    is_isp_match = any(k in isp.lower() or k in org.lower() for k in self.RESIDENTIAL_ISP_KEYWORDS)

                    if not hosting and is_isp_match:
                        fallback_quality.ip_type = "家庭宽带 IP"
                        fallback_quality.is_residential = True
                        fallback_quality.risk_score = 5
                        fallback_quality.risk_label = "极度纯净(IP-API)"
                        fallback_quality.shared_users = "1 - 10 (极好)"
                        fallback_quality.is_native = True
                    elif not hosting:
                        fallback_quality.ip_type = "住宅/商业宽带"
                        fallback_quality.is_residential = True
                        fallback_quality.risk_score = 12
                        fallback_quality.risk_label = "低风险(IP-API)"
                        fallback_quality.shared_users = "10 - 100 (一般)"
                        fallback_quality.is_native = True
                    else:
                        fallback_quality.ip_type = "数据中心 IDC"
                        fallback_quality.is_residential = False
                        fallback_quality.risk_score = 85
                        fallback_quality.risk_label = "高风险机房"

                    logger.info(f"🔍 [备选源: IP-API] IP {ip:<15} -> 类型: {fallback_quality.ip_type:<10} | 风控: {fallback_quality.risk_score}% ({fallback_quality.risk_label}) | ISP: {isp}")
                    return fallback_quality
        except Exception as e:
            logger.warning(f"⚠️ [IP-API] 查询 IP {ip} 失败: {e}")
        return fallback_quality

    def _query_ipwhois(self, ip: str, fallback_quality: IpQuality) -> IpQuality:
        """备选通道 2: 通过 ipwho.is 国际威胁情报精准识别 Hosting 与 VPN 状态"""
        url = f"https://ipwho.is/{ip}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("success"):
                    conn = data.get("connection", {})
                    sec = data.get("security", {}) or {}
                    isp = conn.get("isp") or conn.get("org") or ""
                    asn = str(conn.get("asn") or "")
                    country = data.get("country_code", "")

                    fallback_quality.ip = ip
                    fallback_quality.isp = isp
                    fallback_quality.asn = asn
                    fallback_quality.country = country
                    fallback_quality.updated_at = time.time()

                    is_hosting = bool(sec.get("hosting", False))
                    is_vpn = bool(sec.get("vpn", False))
                    is_proxy = bool(sec.get("proxy", False))

                    is_isp_match = any(k in isp.lower() for k in self.RESIDENTIAL_ISP_KEYWORDS)

                    if not is_hosting and is_isp_match:
                        fallback_quality.ip_type = "家庭宽带 IP"
                        fallback_quality.is_residential = True
                        fallback_quality.risk_score = 5
                        fallback_quality.risk_label = "极度纯净(IPWhois)"
                        fallback_quality.shared_users = "1 - 10 (极好)"
                        fallback_quality.is_native = True
                    elif not is_hosting and not is_vpn and not is_proxy:
                        fallback_quality.ip_type = "住宅/商业宽带"
                        fallback_quality.is_residential = True
                        fallback_quality.risk_score = 12
                        fallback_quality.risk_label = "低风险(IPWhois)"
                        fallback_quality.shared_users = "10 - 100 (一般)"
                        fallback_quality.is_native = True
                    else:
                        fallback_quality.ip_type = "数据中心 IDC"
                        fallback_quality.is_residential = False
                        fallback_quality.risk_score = 85
                        fallback_quality.risk_label = "高风险机房"

                    logger.info(f"🔍 [备选源: IPWhois] IP {ip:<15} -> 类型: {fallback_quality.ip_type:<10} | 风控: {fallback_quality.risk_score}% ({fallback_quality.risk_label}) | ISP: {isp}")
                    return fallback_quality
        except Exception as e:
            logger.warning(f"⚠️ [IPWhois] 查询 IP {ip} 失败: {e}")
        return fallback_quality

# ==============================================================================
# 6. 核心优选排程器 (三阶段智能流水线: 速率过滤 + 存量验身 + 经验加权补位)
# ==============================================================================

# ==============================================================================
# 5.5. 真实下行带宽测速器 (通过 Aimili 本地代理进行微吞吐实测)
# ==============================================================================

TEST_SPEED_URLS = [
    "http://cachefly.cachefly.net/1mb.test",
    "https://speed.cloudflare.com/__down?bytes=1572864",
    "http://speedtest.tele2.net/1MB.zip"
]

def probe_socket_reachability(ip: str, port: int, protocol: str = "tcp", timeout: float = 1.5) -> Tuple[bool, int]:
    """
    对目标节点端口进行轻量直连网络预检，测量真实 RTT 延迟 (毫秒)。
    若端口不可达（连接拒绝、无响应超时），提前淘汰死节点，避免耗费后台切换开销。
    """
    t0 = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        err = sock.connect_ex((ip, port))
        rtt = int((time.time() - t0) * 1000)
        sock.close()
        if err == 0:
            return True, max(1, rtt)
        if protocol.lower() == "tcp":
            return False, 999
        return True, max(1, rtt)
    except Exception:
        if protocol.lower() == "tcp":
            return False, 999
        return True, 999

def probe_real_download_speed(
    proxy_port: int = 7928,
    proxy_host: str = "127.0.0.1",
    max_bytes: int = 2 * 1024 * 1024,
    max_duration: float = 3.0,
    test_urls: Optional[List[str]] = None,
    mock_speed: Optional[float] = None
) -> float:
    """
    通过 Aimili 本地代理进行真实微吞吐测速：
    拉取数据切片，限制在 max_duration 秒内，测算实际下行速率 (Mbps)。
    """
    if mock_speed is not None:
        return round(float(mock_speed), 2)

    urls = test_urls or TEST_SPEED_URLS
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(proxy_handler)

    for url in urls:
        start_t = time.time()
        received = 0
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with opener.open(req, timeout=max_duration + 1.2) as res:
                while True:
                    if (time.time() - start_t) >= max_duration:
                        break
                    chunk = res.read(16384)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received >= max_bytes:
                        break
            duration = max(0.08, time.time() - start_t)
            if received > 2048:
                speed_mbps = round((received * 8.0) / (duration * 1e6), 2)
                logger.debug(f"⚡ [测速成功] 源: {url} -> 传输 {received} 字节，耗时 {duration:.2f}s，流速: {speed_mbps} Mbps")
                return speed_mbps
        except Exception as e:
            logger.debug(f"⚠️ [测速异常] 源 {url} 请求失败: {e}，尝试下一个备用源...")
            continue

    return 0.0

class PickerEngine:
    """优选排程核心"""

    def __init__(self, aimili: AimiliClient, inspector: IpIntelligenceManager, exp_mgr: NodeExperienceManager, probe_speed_func=None, blacklist: Optional[BlacklistManager] = None):
        self.blacklist = blacklist or BlacklistManager()
        self.aimili = aimili
        self.inspector = inspector
        self.exp_mgr = exp_mgr
        self.probe_speed_func = probe_speed_func or probe_real_download_speed
        self.is_running = False
        self.current_step = "空闲就绪"
        self.last_result: Dict[str, Any] = {}
        self.load_history()

    def load_history(self):
        if os.path.isfile(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    self.last_result = json.load(f)
            except Exception:
                pass

    def run_screening(
        self,
        country: str = "JP",
        max_risk: int = 15,
        min_speed_mbps: float = 5.0,
        top_n: int = 20,
        inherit_existing: bool = True,
        source_mode: str = "auto",
        max_rounds: int = 3
    ) -> List[CandidateNode]:
        """执行全自动优选排程流程"""
        if self.is_running:
            logger.warning("⚠️ 上一次优选任务仍在执行中，忽略重复触发")
            return []

        self.is_running = True
        picked_nodes: List[CandidateNode] = []
        start_time = time.time()
        self.inspector.reset_circuit_breaker()

        try:
            self.current_step = f"正在从 AimiliVPN 提取 [{country}] 地区存活候选节点..."
            logger.info(
                f"🚀 启动 [{country}] 智能优选流程 (目标配额: {top_n} 节点，最低速率门槛: {min_speed_mbps} Mbps，"
                f"风控上限: {max_risk}%, 评估模式: {source_mode}, 优先继承存量: {inherit_existing})..."
            )

            # 1. 获取在库候选池
            candidates = self.aimili.fetch_scoped_nodes(country=country, status="available", limit=150)
            if not candidates:
                logger.error("❌ 未从 Aimili 获取到任何存活节点，请先确认 Aimili 后台节点扫描是否完成！")
                self.current_step = "未获取到候选节点"
                return []

            total_candidates = len(candidates)
            logger.info(f"📡 Aimili 在库 [{country}] 地区存活候选节点共 {total_candidates} 个 (当前永久黑名单: {self.blacklist.count} 个)")
            # 过滤处于永久黑名单中的候选节点
            candidates = [c for c in candidates if not self.blacklist.is_blacklisted(c.ip)]

            # 建立候选节点快速索引表
            cand_by_fav_id: Dict[str, CandidateNode] = {}
            for c in candidates:
                cand_by_fav_id[c.canonical_fav_id] = c
                cand_by_fav_id[c.id] = c
                if c.pool_endpoint_id:
                    cand_by_fav_id[f"pool:{c.pool_endpoint_id}"] = c
                    cand_by_fav_id[c.pool_endpoint_id] = c

            retained_nodes: List[CandidateNode] = []
            live_evicted_fav_ids: List[str] = []
            dead_evicted_fav_ids: List[str] = []
            checked_fav_ips = set()
            evaluated_candidates: List[CandidateNode] = []

            # 2. 阶段一：存量收藏验身与经验结算
            current_fav_ids = self.aimili.get_current_favorites()
            logger.info(f"🔍 [阶段一] 开始验身当前 Aimili 收藏池 (当前已有: {len(current_fav_ids)} 个)...")

            if inherit_existing and current_fav_ids:
                for fav_id in current_fav_ids:
                    node = cand_by_fav_id.get(fav_id)
                    if not node:
                        logger.warning(f"🚫 [存量清退] 收藏节点 [{fav_id}] 已在候选池中离线/不可用，标记底层配置清退 (无需API耗时操作)")
                        self.exp_mgr.record_fail(fav_id, "节点离线/不可用")
                        dead_evicted_fav_ids.append(fav_id)
                        continue

                    if node.ip in checked_fav_ips:
                        continue
                    checked_fav_ips.add(node.ip)

                    # 探测存量收藏的风控与宽带类型
                    self.current_step = f"正在复检存量收藏质量: {node.ip}"
                    cached = self.inspector.cache.get(node.ip)
                    node.quality = self.inspector.inspect_ip(node.ip, source_mode=source_mode)
                    if not cached and source_mode != "fallback_only" and not self.inspector._ping0_circuit_broken:
                        time.sleep(random.uniform(1.2, 1.8))
                    else:
                        time.sleep(0.05)

                    q = node.quality
                    exp = self.exp_mgr.get_or_create(node.ip, node.canonical_fav_id, q.isp if q else "")
                    risk_limit = (max_risk + 3) if getattr(exp, "consecutive_passes", 0) >= 5 else max_risk

                    if q and q.risk_score >= 10:
                        self.blacklist.add(node.ip, reason=f"纯净度/风控超标 ({q.risk_score}% >= 10%)", risk_score=q.risk_score, isp=q.isp)
                        logger.warning(f"🚫 [存量淘汰并拉黑] 收藏节点 {node.ip:<15} 风控超标 ({q.risk_score}% ≥ 10%)，永久拉黑并移出收藏")
                        self.exp_mgr.record_fail(node.ip, f"风控超标永久拉黑({q.risk_score}%>=10%)")
                        live_evicted_fav_ids.append(fav_id)
                        continue

                    if q and q.is_residential and q.risk_score <= risk_limit:
                        # 存量节点真实测速校验 (若低于门槛则淘汰)
                        if min_speed_mbps > 0:
                            self.current_step = f"正在实测存量节点带宽: {node.ip}..."
                            logger.info(f"⚡ [实测存量] 正在通过 Aimili 测试存量节点 {node.ip} 真实带宽...")
                            self.aimili.set_active_node(node, timeout=12)
                            time.sleep(1.2)
                            real_speed = self.probe_speed_func(
                                proxy_port=self.aimili.proxy_port,
                                proxy_host=self.aimili.host,
                                max_duration=3.0
                            )
                            node.speed_mbps = real_speed
                            node.speed_bps = int(real_speed * 1e6)
                            evaluated_candidates.append(node)
                            if real_speed < min_speed_mbps:
                                reason = f"实测速率不达标 ({real_speed} Mbps < {min_speed_mbps} Mbps)"
                                logger.warning(f"🚫 [存量淘汰] 收藏节点 {node.ip:<15} {reason}，标记移出收藏")
                                self.exp_mgr.record_fail(node.ip, reason)
                                live_evicted_fav_ids.append(fav_id)
                                continue
                        
                        updated_exp = self.exp_mgr.record_pass(
                            node.ip, node.canonical_fav_id, q.isp, q.risk_score, node.latency_ms
                        )
                        node.experience = updated_exp
                        node.is_inherited = True
                        retained_nodes.append(node)
                        speed_info = f"{node.speed_mbps} Mbps" if node.speed_mbps > 0 else "未测速"
                        logger.info(
                            f"🛡️ [存量保留] 收藏节点 {node.ip:<15} 持续达标 (速率: {speed_info}, 风控: {q.risk_score}%, 延迟: {node.latency_ms}ms) -> "
                            f"经验晋升: {updated_exp.level_label} (连续达标 {updated_exp.consecutive_passes} 轮, 信誉分: {updated_exp.reputation_score})"
                        )
                    else:
                        reason = f"非家宽({q.ip_type})" if (not q or not q.is_residential) else f"风控超标({q.risk_score}%>{risk_limit}%)"
                        logger.warning(f"🚫 [存量淘汰] 收藏节点 {node.ip:<15} 验身未达标 ({reason})，标记移出收藏")
                        self.exp_mgr.record_fail(node.ip, reason)
                        live_evicted_fav_ids.append(fav_id)

                total_evicted = len(live_evicted_fav_ids) + len(dead_evicted_fav_ids)
                logger.info(
                    f"📊 [阶段一结算] 存量收藏复检完毕: 合格保留 {len(retained_nodes)} 个，淘汰清退 {total_evicted} 个 "
                    f"(离线已剔除: {len(dead_evicted_fav_ids)}, 在线未达标: {len(live_evicted_fav_ids)})"
                )
            else:
                for fav_id in current_fav_ids:
                    if fav_id in cand_by_fav_id:
                        live_evicted_fav_ids.append(fav_id)
                    else:
                        dead_evicted_fav_ids.append(fav_id)

            # 3. 阶段二：容量评估与多轮差额智能补位 (支持最多 max_rounds 轮补足)


            picked_nodes = list(retained_nodes)


            tested_ips = set(checked_fav_ips)


            all_newly_picked: List[CandidateNode] = []


            rounds_run = 0



            for current_round in range(1, max_rounds + 1):


                rounds_run = current_round


                needed = top_n - len(picked_nodes)


                if needed <= 0:


                    logger.info(f"🎉 目标配额已满员 ({len(picked_nodes)}/{top_n} 个)，提前完成优选！")


                    break



                logger.info(


                    f"🔍 [第 {current_round}/{max_rounds} 轮优选] 当前已入选 {len(picked_nodes)} 个，"


                    f"尚缺 {needed} 个目标节点，开始扫描候选并实测带宽 (门槛: ≥{min_speed_mbps} Mbps)..."


                )



                if current_round > 1:


                    self.current_step = f"[第 {current_round}/{max_rounds} 轮] 正在触发 Aimili 刷新 VPNGate 节点池..."


                    logger.info(f"🔄 [第 {current_round}/{max_rounds} 轮] 触发 Aimili 刷新节点库并等待 5 秒...")


                    self.aimili.refresh_nodes(country=country)


                    time.sleep(5.0)



                    candidates = self.aimili.fetch_scoped_nodes(country=country, status="available", limit=200)


                    total_candidates = max(total_candidates, len(candidates))



                picked_ips = {n.ip for n in picked_nodes}


                picked_ids = {n.canonical_fav_id for n in picked_nodes}


                fresh_candidates = [


                    c for c in candidates


                    if c.ip not in picked_ips and c.canonical_fav_id not in picked_ids and c.ip not in tested_ips


                ]



                logger.info(f"📋 [第 {current_round}/{max_rounds} 轮] 待检测优质候选节点池: {len(fresh_candidates)} 个")



                for idx, node in enumerate(fresh_candidates, start=1):


                    if len(picked_nodes) >= top_n:


                        logger.info(f"🎉 本轮已成功补足目标配额 ({len(picked_nodes)}/{top_n} 个)！")


                        break



                    tested_ips.add(node.ip)



                    # 1. IP 纯净度与家宽属性检测


                    self.current_step = f"[第 {current_round}/{max_rounds} 轮] 正在质检 IP ({idx}/{len(fresh_candidates)}): {node.ip}"


                    cached = self.inspector.cache.get(node.ip)


                    node.quality = self.inspector.inspect_ip(node.ip, source_mode=source_mode)



                    if not cached and source_mode != "fallback_only" and not self.inspector._ping0_circuit_broken:


                        time.sleep(random.uniform(1.2, 1.8))


                    else:


                        time.sleep(0.05)



                    if self.blacklist.is_blacklisted(node.ip):
                        continue

                    q = node.quality
                    if not q or q.risk_score >= 10:
                        score_val = q.risk_score if q else 100
                        isp_val = q.isp if q else ""
                        self.blacklist.add(node.ip, reason=f"纯净度/风控超标 ({score_val}% >= 10%)", risk_score=score_val, isp=isp_val)
                        logger.warning(f"🚫 [永久拉黑] 节点 {node.ip:<15} 风控超标 ({score_val}% ≥ 10%)，已加入永久黑名单，永不选用！")
                        continue

                    if not q.is_residential or q.risk_score > max_risk:
                        continue



                    # 2. 真实下行带宽测速 (Real Speed Test)


                    if min_speed_mbps > 0:


                        self.current_step = f"[第 {current_round}/{max_rounds} 轮] 正在真实测速 ({node.ip})..."


                        logger.info(f"⚡ [实测开始] 节点 {node.ip:<15} (风控 {q.risk_score}%) 通过初筛，正在连接测试真实带宽...")


                        connected = self.aimili.set_active_node(node, timeout=12)
                        if not connected:
                            logger.warning(f"❌ [握手失败] 无法切换到活动节点 {node.ip} (OpenVPN握手失败或Aimili正忙)，标记握手未通")
                            node.handshake_ok = False
                            self.exp_mgr.record_fail(node.ip, "OpenVPN握手或建连失败")
                            continue

                        time.sleep(1.2)


                        real_speed = self.probe_speed_func(


                            proxy_port=self.aimili.proxy_port,


                            proxy_host=self.aimili.host,


                            max_duration=3.0


                        )


                        node.speed_mbps = real_speed
                        node.speed_bps = int(real_speed * 1e6)

                        if real_speed <= 0.0:
                            logger.warning(f"❌ [出站不通] 节点 {node.ip:<15} 虽已建立隧道但代理出站流量为 0.0 Mbps，握手未真正打通，已淘汰")
                            node.handshake_ok = False
                            self.exp_mgr.record_fail(node.ip, "出站流量不通(0.0Mbps)")
                            continue

                        node.handshake_ok = True
                        evaluated_candidates.append(node)



                        if real_speed < min_speed_mbps:


                            logger.warning(f"🚫 [实测淘汰] 节点 {node.ip:<15} 实测带宽不足 ({real_speed} Mbps < {min_speed_mbps} Mbps)，已淘汰")


                            self.exp_mgr.record_fail(node.ip, f"实测速率不足 ({real_speed} Mbps < {min_speed_mbps} Mbps)")


                            continue


                        else:


                            logger.info(f"✅ [实测达标] 节点 {node.ip:<15} 实测速率: {real_speed} Mbps (≥ {min_speed_mbps} Mbps) 达标！")



                    # 3. 综合加权评分计算


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



                    # 实测带宽速率加权奖励 (实测带宽越高排名越靠前)


                    if node.speed_mbps > 0:


                        score -= min(25.0, node.speed_mbps * 0.5)



                    try:
                        exp_bonus = float(self.exp_mgr.get_experience_bonus(node.ip))
                    except Exception:
                        exp_bonus = 0.0
                    score -= exp_bonus



                    node.rank_score = round(score, 1)



                    # 4. 入选记录


                    exp = self.exp_mgr.record_pass(


                        node.ip, node.canonical_fav_id, q.isp if q else "", q.risk_score if q else 10, node.latency_ms


                    )


                    node.experience = exp


                    node.is_inherited = False



                    speed_info = f"{node.speed_mbps} Mbps" if node.speed_mbps > 0 else "未测速"


                    logger.info(


                        f"🌟 [新晋入选] 补位成功 {node.ip:<15} (实测: {speed_info}, 风控: {q.risk_score}%, 延迟: {node.latency_ms}ms) -> "


                        f"段位: {exp.level_label} (信誉分: {exp.reputation_score}) [当前总计: {len(picked_nodes) + 1}/{top_n}]"


                    )


                    picked_nodes.append(node)


                    all_newly_picked.append(node)



                if len(picked_nodes) < top_n and current_round < max_rounds:


                    logger.info(


                        f"⚠️ [第 {current_round}/{max_rounds} 轮小结] 本轮测完，入选数 ({len(picked_nodes)} 个) 仍不足 {top_n} 个，"


                        f"准备进入第 {current_round + 1} 轮补充测试..."


                    )



            # 核心保底策略：当3轮后无任何达标节点时，降级选取最优的存活节点保底 (必须真正握手成功且有流量)
            SAFETY_FLOOR = min(5, top_n)
            if len(picked_nodes) == 0:
                picked_ips = {n.ip for n in picked_nodes}
                picked_ids = {n.canonical_fav_id for n in picked_nodes}
                # 极其严格：只有真正握手成功 (handshake_ok=True 且 speed_mbps > 0)，且非黑名单、风控严格 < 10% 的节点才允许保底！
                valid_fallbacks = [
                    c for c in evaluated_candidates
                    if getattr(c, "handshake_ok", False) and c.speed_mbps > 0
                    and c.ip not in picked_ips and c.canonical_fav_id not in picked_ids
                    and not self.blacklist.is_blacklisted(c.ip)
                    and (c.quality and c.quality.risk_score < 10)
                ]
                seen_fb_ips = set()
                deduped_fallbacks = []
                for c in valid_fallbacks:
                    if c.ip not in seen_fb_ips and c.ip not in picked_ips:
                        seen_fb_ips.add(c.ip)
                        deduped_fallbacks.append(c)

                if deduped_fallbacks:
                    logger.warning(
                        f"🚨 [触发安全保底降级机制] {rounds_run} 轮筛选后实测无任何节点达到 {min_speed_mbps} Mbps，"
                        f"从真正握手成功的候选池中选取前 {min(SAFETY_FLOOR, len(deduped_fallbacks))} 个最优节点保底！"
                    )
                    deduped_fallbacks.sort(key=lambda n: (-n.speed_mbps, float(getattr(n, "rank_score", 9999.0) or 9999.0), n.latency_ms))
                    needed_floor = SAFETY_FLOOR - len(picked_nodes)
                    fallback_picked = deduped_fallbacks[:needed_floor]

                    for fn in fallback_picked:
                        exp = self.exp_mgr.record_pass(
                            fn.ip, fn.canonical_fav_id, fn.quality.isp if fn.quality else "", fn.quality.risk_score if fn.quality else 10, fn.latency_ms
                        )
                        fn.experience = exp
                        fn.is_inherited = False
                        speed_str = f"{fn.speed_mbps} Mbps" if fn.speed_mbps > 0 else "未达5Mbps"
                        logger.info(
                            f"🛡️ [降级保底入选] 节点 {fn.ip:<15} (实测: {speed_str}, 风控: {fn.quality.risk_score if fn.quality else 10}%, 延迟: {fn.latency_ms}ms) -> "
                            f"保底补充入选成功 (当前总计: {len(picked_nodes) + 1}/{SAFETY_FLOOR})"
                        )
                        picked_nodes.append(fn)
                        all_newly_picked.append(fn)
                else:
                    logger.error("🚨 3 轮筛选未找到任何能成功握手且有出站流量的节点！拒绝将死节点写入收藏库。")

            # 紧急熔断：如果依然没有节点且存量收藏不为空，拒绝清空底层配置，保留旧收藏
            if not picked_nodes and current_fav_ids:
                logger.error("🚨 本次筛选未获得任何可用节点，启动紧急熔断保护：保留上一期旧收藏，拒绝写入空列表！")
                final_target_fav_ids = list(current_fav_ids)
            else:
                final_target_fav_ids = [n.canonical_fav_id for n in picked_nodes]

            try:
                picked_nodes.sort(key=lambda n: float(getattr(n, "rank_score", 9999.0) or 9999.0))
            except Exception:
                pass

            # 4. 阶段三：同步落库 Aimili 收藏夹
            total_evicted_count = len(live_evicted_fav_ids) + len(dead_evicted_fav_ids)
            self.current_step = f"正在同步 Aimili 收藏状态 (保留: {len(retained_nodes)}, 淘汰清退: {total_evicted_count}, 新入库: {len(all_newly_picked)})..."

            # 移出在线淘汰节点 (离线节点直接由底层配置文件同步清理，无需且不能调用 API，避免 Aimili 阻塞超时)
            if live_evicted_fav_ids:
                logger.info(f"🧹 正在从 Aimili 内存收藏库移除 {len(live_evicted_fav_ids)} 个在库但超标的旧节点 (离线节点 {len(dead_evicted_fav_ids)} 个由底层配置自动同步清理)...")
                removed_count = 0
                for old_id in live_evicted_fav_ids:
                    if self.aimili.remove_favorite_by_id(old_id, timeout=3):
                        removed_count += 1
                    time.sleep(0.02)
                logger.info(f"🧹 内存旧节点移出完毕: 成功移出 {removed_count}/{len(live_evicted_fav_ids)} 个")

            # 增量写入新入选节点
            added_count = 0
            for idx, node in enumerate(all_newly_picked, start=1):
                self.current_step = f"正在写入新晋节点到 Aimili 收藏 ({idx}/{len(all_newly_picked)}): {node.ip}..."
                ok = self.aimili.toggle_favorite(node, favorite=True, timeout=30)
                if ok:
                    added_count += 1
                    speed_info = f"{node.speed_mbps} Mbps" if node.speed_mbps > 0 else "未测速"
                    logger.info(f"⭐ 成功加星入库 ({idx}/{len(all_newly_picked)}) -> IP: {node.ip:<15} (速率: {speed_info}, 延迟: {node.latency_ms}ms, 等级: {node.experience.level_label if node.experience else '新晋'})")
                else:
                    logger.warning(f"⚠️ 节点 [{node.ip}] 接口响应异常，底层持久化文件保障同步")
                time.sleep(0.2)

            # 底层持久化双重保证 (清空所有离线及超标节点，仅保留本次优选的节点)
            self.aimili.sync_auth_file_favorites(final_target_fav_ids)

            # 5. 阶段四：自动切换 Aimili 活动出口为第 1 名最佳节点
            active_node_ip = ""
            if picked_nodes:
                best_node = picked_nodes[0]
                active_node_ip = best_node.ip
                self.current_step = f"正在将第 1 名最佳节点设为 Aimili 当前活动出口: {best_node.ip}..."
                logger.info(f"🎯 开始将优选评分第一的黄金节点自动切换为 Aimili 当前活动出口 -> IP: {best_node.ip} (延迟: {best_node.latency_ms}ms, 速率: {best_node.speed_mbps}Mbps)")
                switched = self.aimili.set_active_node(best_node, timeout=15)
                if switched:
                    logger.info(f"✨ [活动出口已切换] Aimili 当前活动出口已成功锁定为: {best_node.ip}！")
                else:
                    logger.warning(f"⚠️ 自动切换活动出口返回异常，已在收藏夹首位保留，可从 Aimili 网页手动点击连接")

            final_favs = self.aimili.get_current_favorites()
            logger.info(
                f"🎉 优选落盘完毕！当前 Aimili 收藏库总计: {len(final_favs)} 个纯净家宽节点 "
                f"(传承老兵: {len(retained_nodes)} 个, 新秀补位: {added_count} 个, 淘汰清退: {total_evicted_count} 个, 当前出口: {active_node_ip or '未变更'})！"
            )

            # 6. 保存最新结果并持久化
            elapsed = round(time.time() - start_time, 1)
            self.last_result = {
                "scan_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "country": country,
                "max_risk_threshold": max_risk,
                "min_speed_threshold_mbps": min_speed_mbps,
                "source_mode": source_mode,
                "elapsed_seconds": elapsed,
                "total_candidates": total_candidates,
                "retained_count": len(retained_nodes),
                "evicted_count": total_evicted_count,
                "newly_added_count": len(all_newly_picked),
                "picked_count": len(picked_nodes),
                "active_node_ip": active_node_ip,
                "nodes": [
                    {
                        "id": n.id,
                        "ip": n.ip,
                        "port": n.port,
                        "country": n.country,
                        "latency_ms": n.latency_ms,
                        "speed_mbps": n.speed_mbps,
                        "speed_bps": n.speed_bps,
                        "rank_score": n.rank_score,
                        "is_inherited": n.is_inherited,
                        "is_active": (idx == 0),
                        "experience_level": n.experience.level_label if n.experience else "Lv.1 新秀",
                        "consecutive_passes": n.experience.consecutive_passes if n.experience else 1,
                        "reputation_score": n.experience.reputation_score if n.experience else 80.0,
                        "ip_type": n.quality.ip_type if n.quality else "",
                        "risk_score": n.quality.risk_score if n.quality else 100,
                        "risk_label": n.quality.risk_label if n.quality else "",
                        "shared_users": n.quality.shared_users if n.quality else "",
                        "is_native": n.quality.is_native if n.quality else False,
                        "isp": n.quality.isp if n.quality else ""
                    }
                    for idx, n in enumerate(picked_nodes)
                ]
            }

            try:
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.last_result, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"❌ 记录历史结果异常: {e}")

            self.current_step = f"优选完成！收藏池就绪: 保持 {len(retained_nodes)} 个, 新增 {len(all_newly_picked)} 个 (共完成 {rounds_run}/{max_rounds} 轮，耗时 {elapsed}s)"

        except Exception as e:
            logger.exception(f"❌ 执行筛选流程发生未捕获异常: {e}")
            self.current_step = f"执行出错: {e}"
        finally:
            self.is_running = False

        return picked_nodes

# ==============================================================================
# 7. 轻量现代化 Web 控制台 (纯标准库 HTML5 + CSS3 + REST API)
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
    .container { max-width: 1200px; margin: 0 auto; }
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
    .grid { display: grid; grid-template-columns: 1.1fr 2fr; gap: 20px; margin-bottom: 24px; }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
    .card {
      background: var(--card-bg);
      backdrop-filter: blur(12px);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px;
      box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
    }
    .card-title { font-size: 15px; font-weight: 600; margin-bottom: 16px; display: flex; align-items: center; gap: 6px; }
    .form-group { margin-bottom: 14px; }
    label { display: block; font-size: 13px; color: var(--text-muted); margin-bottom: 6px; }
    select, input[type="number"], input[type="text"] {
      width: 100%;
      background: rgba(15, 23, 42, 0.8);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      padding: 8px 12px;
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
      margin-top: 8px;
    }
    .btn-run:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 6px 20px 0 rgba(59, 130, 246, 0.5); }
    .btn-run:disabled { opacity: 0.6; cursor: not-allowed; }
    .status-box {
      margin-top: 14px;
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
      height: 320px;
      overflow-y: auto;
      line-height: 1.6;
    }
    .log-line { word-break: break-all; white-space: pre-wrap; margin-bottom: 2px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }
    th { padding: 12px 14px; background: rgba(15, 23, 42, 0.8); color: var(--text-muted); font-weight: 600; border-bottom: 1px solid var(--border); }
    td { padding: 12px 14px; border-bottom: 1px solid rgba(255, 255, 255, 0.05); }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }
    .tag-pure { background: rgba(16, 185, 129, 0.2); color: #34d399; padding: 2px 8px; border-radius: 9999px; font-weight: 600; font-size: 11px; }
    .tag-active { background: rgba(239, 68, 68, 0.2); color: #f87171; padding: 2px 8px; border-radius: 9999px; font-weight: 600; font-size: 11px; border: 1px solid rgba(239, 68, 68, 0.4); }
    .tag-fav { background: rgba(245, 158, 11, 0.2); color: #fbbf24; padding: 2px 8px; border-radius: 9999px; font-weight: 600; font-size: 11px; }
    .tag-fresh { background: rgba(168, 85, 247, 0.2); color: #c084fc; padding: 2px 8px; border-radius: 9999px; font-weight: 600; font-size: 11px; }
    .tag-home { background: rgba(59, 130, 246, 0.2); color: #60a5fa; padding: 2px 8px; border-radius: 9999px; font-size: 11px; }
    .tag-speed { background: rgba(14, 165, 233, 0.2); color: #38bdf8; padding: 2px 8px; border-radius: 9999px; font-weight: 600; font-size: 11px; }
    .tag-level { background: rgba(234, 179, 8, 0.15); color: #fde047; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: 600; border: 1px solid rgba(234, 179, 8, 0.3); }
  </style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title-box">
      <h1>⭐ Aimili 纯净家宽优选控制台 <span style="font-size: 13px; font-weight: normal; color: #38bdf8;">v3.6 极速联动版</span></h1>
      <p>速率门槛过滤 · 智能熔断防卡死 · 自动切换最优出口 · 老兵信誉成长 · 差额按需补位 · 双重持久化落盘</p>
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
        <label>最低带宽速率要求 (Mbps)</label>
        <input type="number" id="inpMinSpeed" value="5" min="0" max="1000">
        <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">过滤低于此速率的节点 (填 0 为不限带宽)</div>
      </div>
      <div class="form-group">
        <label>最大允许风控值 (%)</label>
        <input type="number" id="inpMaxRisk" value="15" min="0" max="100">
        <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">≤ 15% 属于 Ping0 评定之【极度纯净】标准</div>
      </div>
      <div class="form-group">
        <label>期望优选入选数量 (个)</label>
        <input type="number" id="inpTopN" value="20" min="1" max="50">
      </div>
      <div class="form-group">
        <label>风控与质量评估数据源模式</label>
        <select id="selSourceMode">
          <option value="auto" selected>⚡ 智能融合 (Ping0 优先 + IP-API / IPWhois 兜底) [推荐]</option>
          <option value="fallback_only">🚀 极速备选源 (IP-API + IPWhois, 免受 Ping0 频控)</option>
          <option value="ping0_only">🎯 严格模式 (仅 Ping0.cc)</option>
        </select>
      </div>
      <div class="form-group" style="display: flex; align-items: center; gap: 8px; margin-top: 6px;">
        <input type="checkbox" id="chkInherit" checked style="width: 17px; height: 17px; cursor: pointer; accent-color: var(--accent);">
        <label for="chkInherit" style="margin-bottom: 0; cursor: pointer; color: var(--text); font-size: 13px;">优先继承合格存量 (智能经验成长机制)</label>
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
      <div class="card-title" style="justify-content: space-between;">
        <span>📜 实时探查与决策日志</span>
        <label style="font-size: 12px; color: var(--text-muted); cursor: pointer; display: flex; align-items: center; gap: 5px; font-weight: normal;">
          <input type="checkbox" id="chkPauseLog" style="width: 14px; height: 14px; cursor: pointer; accent-color: var(--accent);"> 暂停日志刷新
        </label>
      </div>
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
            <th>来源状态</th>
            <th>IP 地址</th>
            <th>带宽速率</th>
            <th>经验段位 / 连续达标</th>
            <th>信誉得分</th>
            <th>地区/运营商</th>
            <th>宽带类型</th>
            <th>风控评分</th>
            <th>共享人数</th>
            <th>原生 IP</th>
            <th>实测延迟</th>
          </tr>
        </thead>
        <tbody id="tblPickedNodes">
          <tr><td colspan="11" style="text-align: center; color: var(--text-muted); padding: 30px;">点击左侧【开始筛选家宽节点】按钮启动全自动优选与收藏池自进化</td></tr>
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

      document.getElementById('lblStatus').textContent = data.current_step || (isScanning ? '正在优选...' : '空闲就绪');
      document.getElementById('btnStartScan').disabled = isScanning;
      document.getElementById('btnText').textContent = isScanning ? '优选中...' : '开始筛选家宽节点';
      document.getElementById('btnIcon').textContent = isScanning ? '⏳' : '🚀';
      document.getElementById('progBar').style.width = isScanning ? '75%' : (data.last_result?.nodes ? '100%' : '0%');

      if (data.last_result && data.last_result.scan_time) {
        const r = data.last_result;
        const activeIp = r.active_node_ip || (r.nodes && r.nodes[0] ? r.nodes[0].ip : '未切换');
        document.getElementById('lblLastScanTime').textContent = 
          `时间: ${r.scan_time} · 🔥 当前活动出口: ${activeIp} · 耗时 ${r.elapsed_seconds || 0}s · 门槛: ≥${r.min_speed_threshold_mbps || 0}Mbps · 传承保留: ${r.retained_count || 0} · 新选补位: ${r.newly_added_count || 0} · 淘汰清退: ${r.evicted_count || 0} · 收藏总计: ${r.picked_count || 0}`;
        renderTable(r.nodes || []);
      }
    } catch (e) {
      console.error(e);
    }
  }

  async function fetchLogs() {
    if (document.getElementById('chkPauseLog')?.checked) return;
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
      tbody.innerHTML = '<tr><td colspan="11" style="text-align: center; color: var(--text-muted); padding: 30px;">未找到符合阈值的节点</td></tr>';
      return;
    }
    tbody.innerHTML = nodes.map(n => `
      <tr>
        <td>${n.is_active ? '<span class="tag-active">🔥 当前出口</span> ' : ''}${n.is_inherited ? '<span class="tag-fav">⭐ 传承老兵</span>' : '<span class="tag-fresh">🌟 新选补位</span>'}</td>
        <td style="font-weight: 600; font-family: monospace;">${n.ip}</td>
        <td><span class="tag-speed">⚡ ${n.speed_mbps > 0 ? n.speed_mbps + ' Mbps' : '未测速'}</span></td>
        <td><span class="tag-level">${escapeHtml(n.experience_level || 'Lv.1 新秀')} (${n.consecutive_passes || 1}轮)</span></td>
        <td style="font-weight: 600; color: #38bdf8;">${n.reputation_score || 80.0}</td>
        <td>${n.country} · ${escapeHtml(n.isp || '家庭宽带ISP')}</td>
        <td><span class="tag-home">${escapeHtml(n.ip_type)}</span></td>
        <td><span class="tag-pure">${n.risk_score}% (${n.risk_label || '极度纯净'})</span></td>
        <td>${escapeHtml(n.shared_users || '未知')}</td>
        <td>${n.is_native ? '✅ 原生' : '广播'}</td>
        <td>${n.latency_ms} ms</td>
      </tr>
    `).join('');
  }

  async function triggerScan() {
    const country = document.getElementById('selCountry').value;
    const maxRisk = parseInt(document.getElementById('inpMaxRisk').value) || 15;
    const minSpeed = parseFloat(document.getElementById('inpMinSpeed').value) || 5.0;
    const topN = parseInt(document.getElementById('inpTopN').value) || 20;
    const sourceMode = document.getElementById('selSourceMode').value || 'auto';
    const inheritExisting = document.getElementById('chkInherit').checked;

    try {
      await fetch('./api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          country,
          max_risk: maxRisk,
          min_speed_mbps: minSpeed,
          top_n: topN,
          source_mode: sourceMode,
          inherit_existing: inheritExisting
        })
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
            min_speed_mbps = float(params.get("min_speed_mbps", 5.0))
            top_n = int(params.get("top_n", 20))
            source_mode = str(params.get("source_mode", "auto"))
            inherit_existing = bool(params.get("inherit_existing", True))
            max_rounds = int(params.get("max_rounds", 3))

            if self.engine and not self.engine.is_running:
                threading.Thread(
                    target=self.engine.run_screening,
                    kwargs={
                        "country": country,
                        "max_risk": max_risk,
                        "min_speed_mbps": min_speed_mbps,
                        "top_n": top_n,
                        "inherit_existing": inherit_existing,
                        "source_mode": source_mode,
                        "max_rounds": max_rounds
                    },
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
# 8. 主程序入口与定时任务调度
# ==============================================================================

def cron_worker(engine: PickerEngine, interval_hours: int, country: str, max_risk: int, min_speed_mbps: float, top_n: int, inherit_existing: bool, source_mode: str, max_rounds: int = 3):
    """后台定时巡检任务"""
    logger.info(f"⏰ 定时巡检线程已就绪: 每隔 {interval_hours} 小时全自动重新优选入库一次 (目标: {top_n} 节点，门槛: ≥{min_speed_mbps}Mbps，模式: {source_mode})")
    while True:
        time.sleep(interval_hours * 3600)
        try:
            logger.info("⏰ 触发定时自动优选任务...")
            engine.run_screening(
                country=country,
                max_risk=max_risk,
                min_speed_mbps=min_speed_mbps,
                top_n=top_n,
                inherit_existing=inherit_existing,
                source_mode=source_mode,
                max_rounds=max_rounds
            )
        except Exception as e:
            logger.error(f"❌ 定时巡检异常: {e}")

def main():
    parser = argparse.ArgumentParser(description="AimiliVPN 智能纯净家宽节点优选器")
    parser.add_argument("--port", type=int, default=8585, help="Web 控制台服务端口 (默认 8585)")
    parser.add_argument("--country", type=str, default="JP", help="优选地区代码 (默认 JP)")
    parser.add_argument("--max-risk", type=int, default=15, help="最大允许风控分 (默认 15)")
    parser.add_argument("--min-speed", type=float, default=5.0, help="最低允许实测带宽速率 Mbps (默认 5.0，填 0 为不限)")
    parser.add_argument("--max-rounds", type=int, default=3, help="节点数量不足时的最大补测轮数 (默认 3 轮)")
    parser.add_argument("--proxy-port", type=int, default=None, help="Aimili 本地代理端口 (默认自动从 ui_auth.json 读取或使用 7928)")
    parser.add_argument("--top-n", type=int, default=20, help="优选收藏入库节点数 (默认 20)")
    parser.add_argument("--source-mode", type=str, default="auto", choices=["auto", "fallback_only", "ping0_only"], help="风控评估数据源模式 (auto/fallback_only/ping0_only)")
    parser.add_argument("--interval", type=int, default=6, help="定时自动扫描间隔小时 (默认 6 小时)")
    parser.add_argument("--scan-once", action="store_true", help="仅执行一次筛选并立刻退出 (适用于系统 crontab)")
    parser.add_argument("--no-inherit", action="store_true", help="不继承已有收藏，每次全量重置 (默认优先继承合格存量)")

    # Aimili 连接覆盖参数
    parser.add_argument("--secret-path", type=str, default=None, help="Aimili 网页安全后缀 (如 vuYkz89zKZtf，默认自动读取 ui_auth.json)")
    parser.add_argument("--username", type=str, default=None, help="Aimili 管理员账号 (默认自动读取 ui_auth.json)")
    parser.add_argument("--password", type=str, default=None, help="Aimili 管理员密码 (默认自动读取 ui_auth.json)")
    parser.add_argument("--aimili-port", type=int, default=None, help="Aimili 本地管理端口 (默认 8501)")
    parser.add_argument("--auth-file", type=str, default="/opt/aimilivpn/vpngate_data/ui_auth.json", help="Aimili 认证文件路径")
    args = parser.parse_args()

    aimili = AimiliClient(
        auth_file=args.auth_file,
        username=args.username,
        password=args.password,
        secret_path=args.secret_path,
        port=args.aimili_port,
        proxy_port=args.proxy_port
    )
    inspector = IpIntelligenceManager()
    exp_mgr = NodeExperienceManager()
    engine = PickerEngine(aimili=aimili, inspector=inspector, exp_mgr=exp_mgr)

    inherit_existing = not args.no_inherit

    # 1. 单次执行模式
    if args.scan_once:
        logger.info("🎯 运行于单次筛选模式 (--scan-once)")
        picked = engine.run_screening(
            country=args.country,
            max_risk=args.max_risk,
            min_speed_mbps=args.min_speed,
            top_n=args.top_n,
            inherit_existing=inherit_existing,
            source_mode=args.source_mode,
            max_rounds=args.max_rounds
        )
        print("\n" + "=" * 75)
        print(f"🎉 筛选完毕！最终入选收藏池列表 ({len(picked)} 个):")
        print("=" * 75)
        for idx, n in enumerate(picked, 1):
            q = n.quality
            exp = n.experience
            origin = "传承老兵" if n.is_inherited else "新晋补位"
            level = exp.level_label if exp else "新晋"
            speed_info = f"{n.speed_mbps:>5} Mbps" if n.speed_mbps > 0 else " 未测速 "
            print(f"[{idx}] {origin} | IP: {n.ip:<15} | 速率: {speed_info} | 等级: {level} | 延迟: {n.latency_ms:>3}ms | 风控: {q.risk_score:>2}% ({q.risk_label})")
        print("=" * 75 + "\n")
        sys.exit(0)

    # 2. Web 服务 + 定时后台巡检模式
    WebRequestHandler.engine = engine

    # 启动后台定时任务
    if args.interval > 0:
        threading.Thread(
            target=cron_worker,
            args=(engine, args.interval, args.country, args.max_risk, args.min_speed, args.top_n, inherit_existing, args.source_mode, args.max_rounds),
            daemon=True
        ).start()

    server = ThreadedHTTPServer(("0.0.0.0", args.port), WebRequestHandler)
    logger.info(f"🌐 优选 Web 控制台已启动，监听地址: http://0.0.0.0:{args.port}")
    logger.info(f"👉 浏览器访问: http://<你的VPS公网IP>:{args.port} (如果绑定了域名也可通过域名加端口访问)")
    logger.info(f"💡 运行参数: 地区 [{args.country}] | 最大风控 [{args.max_risk}%] | 最低速率 [≥{args.min_speed} Mbps] | 模式 [{args.source_mode}] | 目标配额 [{args.top_n} 个]")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("👋 服务已正常停止")

if __name__ == "__main__":
    main()
