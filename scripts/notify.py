#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/notify.py
通用通知推送模块: 支持 Telegram (频道/群组/Bot) 与飞书群机器人 Webhook。
支持从优选结果自动提取元数据并宏变量渲染通知文本。
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

DEFAULT_NOTIFICATION_TEMPLATE = (
    "🎉 每日订阅器测速维护完成！\n"
    "📅 更新时间: {datetime}\n"
    "🌐 测速线路: {tag}\n"
    "📍 测速位置: {colo_summary}\n"
    "📊 测速数量: {tested_count} TLS 已启用\n"
    "✨ 达标节点 (≥ {min_speed} MB/s):\n"
    "{regions_badge}\n"
    "🚀 每天 4 次同步上传更新订阅器成功。"
)

REGION_NAME_MAP = {
    "HK": "香港",
    "TW": "台湾",
    "SG": "狮城",
    "JP": "日本",
    "US": "美国",
    "KR": "韩国",
    "DE": "德国",
    "IN": "印度",
    "UK": "英国",
    "FR": "法国",
    "CA": "加拿大",
    "AU": "澳洲",
}

REGION_FLAGS = {
    "HK": "🇭🇰",
    "TW": "🇹🇼",
    "SG": "🇸🇬",
    "JP": "🇯🇵",
    "US": "🇺🇸",
    "KR": "🇰🇷",
    "DE": "🇩🇪",
    "IN": "🇮🇳",
}


def build_regions_badge(nodes: List[Dict]) -> str:
    """根据入选节点列表汇总各区域数量并格式化为徽章网格文本"""
    counts: Dict[str, int] = {}
    for n in nodes:
        # 优先从 remark 推断 (如 HK-公司-01...)
        reg = n.get("exit_country", "")
        remark = n.get("remark", "")
        if not reg and "-" in remark:
            reg = remark.split("-")[0].upper()
        if reg:
            reg = reg.upper()
            counts[reg] = counts.get(reg, 0) + 1

    if not counts:
        return "暂无有效区域数据"

    # 优先顺序: HK, TW, SG, JP, US, KR 等
    preferred_order = ["HK", "TW", "SG", "JP", "US", "KR", "DE", "IN"]
    sorted_regions = [r for r in preferred_order if r in counts]
    for r in counts:
        if r not in sorted_regions:
            sorted_regions.append(r)

    items = []
    for r in sorted_regions:
        c = counts[r]
        name = REGION_NAME_MAP.get(r, r)
        flag = REGION_FLAGS.get(r, "🌐")
        items.append(f"{flag} {name} ({c})")

    # 每行 3 个排版
    lines = []
    for i in range(0, len(items), 3):
        lines.append("  ".join(items[i : i + 3]))
    return "\n".join(lines)


def format_message(template: str, context: Dict[str, str]) -> str:
    """宏变量文本替换"""
    text = template or DEFAULT_NOTIFICATION_TEMPLATE
    for key, val in context.items():
        macro = "{" + key + "}"
        text = text.replace(macro, str(val))
    return text


def send_telegram(bot_token: str, chat_id: str, text: str, timeout: int = 15) -> Tuple[bool, str]:
    """
    通过 Telegram Bot API 发送消息。
    支持向频道 (如 @my_channel)、群组或个人 Chat ID 发送。
    """
    if not bot_token or not chat_id:
        return False, "Telegram Bot Token 或 Chat ID 未配置"

    url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
    payload = {
        "chat_id": chat_id.strip(),
        "text": text,
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "iStoreOS-IPSelect/1.5"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            res_body = response.read().decode("utf-8")
            res_json = json.loads(res_body)
            if res_json.get("ok"):
                msg_id = res_json.get("result", {}).get("message_id", "unknown")
                return True, f"发送成功 (msg_id: {msg_id})"
            return False, f"Telegram API 错误: {res_body}"
    except urllib.error.HTTPError as e:
        err_content = e.read().decode("utf-8", errors="ignore")
        try:
            err_json = json.loads(err_content)
            desc = err_json.get("description", err_content)
        except Exception:
            desc = err_content
        return False, f"HTTP {e.code}: {desc}"
    except Exception as e:
        return False, f"网络请求异常: {str(e)}"


def generate_feishu_sign(secret: str, timestamp: int) -> str:
    """计算飞书机器人请求签名"""
    if not secret:
        return ""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def send_feishu(webhook_url: str, text: str, secret: str = "", timeout: int = 15) -> Tuple[bool, str]:
    """
    通过飞书群机器人 Webhook 发送消息 (支持可选密钥签名)。
    """
    if not webhook_url:
        return False, "飞书 Webhook URL 未配置"

    payload: Dict = {
        "msg_type": "text",
        "content": {"text": text},
    }

    if secret and secret.strip():
        now_ts = int(time.time())
        sign = generate_feishu_sign(secret.strip(), now_ts)
        payload["timestamp"] = str(now_ts)
        payload["sign"] = sign

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url.strip(),
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "iStoreOS-IPSelect/1.5"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            res_body = response.read().decode("utf-8")
            res_json = json.loads(res_body)
            # 飞书返回: {"code": 0, "msg": "success"} 或 {"StatusCode": 0, ...}
            code = res_json.get("code", res_json.get("StatusCode", -1))
            if code == 0:
                return True, "飞书推送成功"
            return False, f"飞书返回错误 (code: {code}): {res_json.get('msg', res_body)}"
    except urllib.error.HTTPError as e:
        err_content = e.read().decode("utf-8", errors="ignore")
        return False, f"HTTP {e.code}: {err_content}"
    except Exception as e:
        return False, f"网络请求异常: {str(e)}"


def build_context_from_results(txt_path: str, detail_path: str, env_tag: str = "默认") -> Dict[str, str]:
    """从优选生成的 best_us.txt 与 best_us_detail.json 提取通知上下文变量"""
    now_str = time.strftime("%Y-%m-%d %H:%M")
    context = {
        "datetime": now_str,
        "tag": env_tag or "自动线路",
        "colo_summary": "香港 / 亚太边缘机房",
        "tested_count": "1500+",
        "regions_badge": "暂无数据",
        "min_speed": "3.0",
        "total_nodes": "0",
    }

    nodes = []
    if os.path.exists(detail_path):
        try:
            with open(detail_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                nodes = d.get("nodes", [])
                context["min_speed"] = str(d.get("min_speed_threshold", "3.0"))
                tested_c = d.get("initial_candidates_count") or d.get("tested_count")
                if tested_c:
                    context["tested_count"] = str(tested_c)
                colos = set(n.get("colo", "") for n in nodes if n.get("colo"))
                if colos:
                    context["colo_summary"] = " / ".join(list(colos)[:4])
        except Exception:
            pass

    if not nodes and os.path.exists(txt_path):
        try:
            with open(txt_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("#")
                    remark = parts[1].strip() if len(parts) > 1 else ""
                    nodes.append({"remark": remark})
        except Exception:
            pass

    context["total_nodes"] = str(len(nodes))
    if nodes:
        context["regions_badge"] = build_regions_badge(nodes)

    return context


def main():
    parser = argparse.ArgumentParser(description="IPSelect 通知推送工具 (Telegram / 飞书)")
    parser.add_argument("--channel", choices=["telegram", "feishu", "all"], default="all")
    parser.add_argument("--telegram-token", default="")
    parser.add_argument("--telegram-chat-id", default="")
    parser.add_argument("--telegram-template", default="")
    parser.add_argument("--feishu-webhook", default="")
    parser.add_argument("--feishu-secret", default="")
    parser.add_argument("--feishu-template", default="")
    parser.add_argument("--text", default="", help="直接发送的固定文本(若提供则优先使用)")
    parser.add_argument("--txt-file", default="", help="best_us.txt 路径")
    parser.add_argument("--detail-file", default="", help="best_us_detail.json 路径")
    parser.add_argument("--tag", default="默认", help="环境标签")

    args = parser.parse_args()

    # 构造待发送文本
    msg_text = args.text
    if not msg_text:
        ctx = build_context_from_results(args.txt_file, args.detail_file, env_tag=args.tag)
        tpl = args.telegram_template or args.feishu_template or DEFAULT_NOTIFICATION_TEMPLATE
        msg_text = format_message(tpl, ctx)

    success_all = True

    # 1. Telegram
    if args.channel in ["telegram", "all"] and args.telegram_token and args.telegram_chat_id:
        tg_tpl = args.telegram_template or DEFAULT_NOTIFICATION_TEMPLATE
        if not args.text and args.txt_file:
            ctx = build_context_from_results(args.txt_file, args.detail_file, env_tag=args.tag)
            tg_text = format_message(tg_tpl, ctx)
        else:
            tg_text = msg_text
        ok, res = send_telegram(args.telegram_token, args.telegram_chat_id, tg_text)
        print(f"[Telegram] ok={ok} msg={res}")
        if not ok:
            success_all = False

    # 2. 飞书
    if args.channel in ["feishu", "all"] and args.feishu_webhook:
        fs_tpl = args.feishu_template or DEFAULT_NOTIFICATION_TEMPLATE
        if not args.text and args.txt_file:
            ctx = build_context_from_results(args.txt_file, args.detail_file, env_tag=args.tag)
            fs_text = format_message(fs_tpl, ctx)
        else:
            fs_text = msg_text
        ok, res = send_feishu(args.feishu_webhook, fs_text, secret=args.feishu_secret)
        print(f"[Feishu] ok={ok} msg={res}")
        if not ok:
            success_all = False

    sys.exit(0 if success_all else 1)


if __name__ == "__main__":
    main()
