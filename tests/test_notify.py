# -*- coding: utf-8 -*-
"""
tests/test_notify.py
测试通知模块: 宏变量模板格式化、Telegram API 请求构造、飞书签名与 Webhook 请求构造。
"""

import json
import os
import sys
import time
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.notify import (
    format_message,
    build_regions_badge,
    generate_feishu_sign,
    send_telegram,
    send_feishu,
    DEFAULT_NOTIFICATION_TEMPLATE,
)


def test_build_regions_badge():
    nodes = [
        {"remark": "HK-公司-01-4.63MB/s", "exit_country": "HK"},
        {"remark": "HK-公司-02-5.46MB/s", "exit_country": "HK"},
        {"remark": "TW-公司-01-3.00MB/s", "exit_country": "TW"},
        {"remark": "JP-公司-01-3.54MB/s", "exit_country": "JP"},
        {"remark": "SG-公司-01-7.38MB/s", "exit_country": "SG"},
        {"remark": "US-公司-01-5.80MB/s", "exit_country": "US"},
        {"remark": "US-公司-02-6.42MB/s", "exit_country": "US"},
    ]
    badge = build_regions_badge(nodes)
    assert "香港" in badge and "2" in badge
    assert "台湾" in badge and "1" in badge
    assert "日本" in badge and "1" in badge
    assert "狮城" in badge and "1" in badge
    assert "美国" in badge and "2" in badge


def test_format_message_macros():
    context = {
        "datetime": "2026-10-13 12:27",
        "tag": "电信 移动 联通",
        "colo_summary": "香港 / 亚太边缘机房",
        "tested_count": "7570",
        "regions_badge": "香港 (3)  台湾 (1)\n日本 (6)  美国 (7)",
        "min_speed": "3.0",
        "total_nodes": "17",
    }
    result = format_message(DEFAULT_NOTIFICATION_TEMPLATE, context)
    assert "2026-10-13 12:27" in result
    assert "电信 移动 联通" in result
    assert "7570" in result
    assert "香港 (3)" in result
    assert "3.0" in result


def test_generate_feishu_sign():
    timestamp = 1791800000
    secret = "test_secret_key"
    sign = generate_feishu_sign(secret, timestamp)
    assert isinstance(sign, str)
    assert len(sign) > 10

    # 无 secret 时返回空字符串
    assert generate_feishu_sign("", timestamp) == ""


@patch("urllib.request.urlopen")
def test_send_telegram_success(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.getcode.return_value = 200
    mock_resp.read.return_value = json.dumps({"ok": True, "result": {"message_id": 123}}).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    ok, msg = send_telegram("123456:ABC-DEF", "@my_channel", "测试消息")
    assert ok is True
    assert "123" in msg

    # 验证请求参数
    args, kwargs = mock_urlopen.call_args
    req = args[0]
    assert req.full_url == "https://api.telegram.org/bot123456:ABC-DEF/sendMessage"
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["chat_id"] == "@my_channel"
    assert payload["text"] == "测试消息"


@patch("urllib.request.urlopen")
def test_send_telegram_failure(mock_urlopen):
    import urllib.error
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="https://api.telegram.org",
        code=400,
        msg="Bad Request: chat not found",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"ok":false,"description":"Bad Request: chat not found"}')
    )

    ok, msg = send_telegram("123456:ABC-DEF", "@invalid_chat", "测试消息")
    assert ok is False
    assert "chat not found" in msg


@patch("urllib.request.urlopen")
def test_send_feishu_success(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.getcode.return_value = 200
    mock_resp.read.return_value = json.dumps({"code": 0, "msg": "success"}).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/abc-123"
    ok, msg = send_feishu(webhook, "飞书测试内容", secret="my_secret")
    assert ok is True

    # 验证 payload
    req = mock_urlopen.call_args[0][0]
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["msg_type"] == "text"
    assert payload["content"]["text"] == "飞书测试内容"
    assert "timestamp" in payload
    assert "sign" in payload
