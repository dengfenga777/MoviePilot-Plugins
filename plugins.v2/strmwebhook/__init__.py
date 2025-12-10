# -*- coding: utf-8 -*-
"""
StrmWebhookNotify —— 专用于通知 STRM 服务器生成 .strm
不会刷新媒体库，只发送 webhook
"""

import threading
import time
import json
import requests
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional

from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import TransferInfo
from app.schemas.types import EventType


class StrmWebhookNotify(_PluginBase):
    plugin_name = "STRM Webhook 通知"
    plugin_desc = "入库完成后立即将入库目录结构发送到 STRM 服务器实现秒生成 .strm 文件。"
    plugin_icon = "refresh2.png"
    plugin_version = "1.0.0"
    plugin_author = "misaya + chatgpt"
    author_url = "https://github.com/misaya"
    plugin_config_prefix = "strmwebhook_"
    plugin_order = 5
    auth_level = 1

    # ============== 插件内部状态 ==============
    _enabled: bool = False
    _webhook_url: str = ""
    _token: str = ""
    _timeout: int = 5

    def init_plugin(self, config: dict = None):
        """读取插件配置"""
        if config:
            self._enabled = config.get("enabled", False)
            self._webhook_url = config.get("webhook_url", "")
            self._token = config.get("token", "")
            self._timeout = int(config.get("timeout", 5))

        logger.info(f"[STRM] 初始化插件 enabled={self._enabled}, url={self._webhook_url}")

    # ============== 插件配置页面 ==============
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        返回两个内容：
        1. 页面结构（前端表单）
        2. 默认值
        """
        return [
            {
                "component": "VForm",
                "content": [

                    # 开关
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用 STRM Webhook 功能",
                        }
                    },

                    # webhook_url
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "webhook_url",
                            "label": "Webhook URL（必填）",
                            "placeholder": "http://127.0.0.1:7001/strm_event"
                        }
                    },

                    # Token
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "token",
                            "label": "访问 Token（可选）"
                        }
                    },

                    # 超时
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "timeout",
                            "label": "请求超时（秒）",
                            "placeholder": "默认为 5 秒"
                        }
                    },
                ]
            }
        ], {
            "enabled": False,
            "webhook_url": "",
            "token": "",
            "timeout": 5
        }

    # ============== 插件无页面展示 ==============
    def get_page(self):
        return []

    # ============== 监听入库事件 ==============
    @eventmanager.register(EventType.TransferComplete)
    def handle_transfer_complete(self, event: Event):
        """入库完成发送 webhook"""

        if not self._enabled:
            return
        
        event_data = event.event_data
        if not event_data:
            return

        transfer: TransferInfo = event_data.get("transferinfo")
        mediainfo: MediaInfo = event_data.get("mediainfo")

        if not transfer or not mediainfo:
            return
        
        target_path = transfer.target_diritem.path
        logger.info(f"[STRM] 触发 STRM Webhook，入库路径：{target_path}")

        payload = {
            "title": mediainfo.title,
            "year": mediainfo.year,
            "type": mediainfo.type,
            "category": mediainfo.category,
            "path": str(target_path)
        }

        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            r = requests.post(
                self._webhook_url,
                data=json.dumps(payload),
                headers=headers,
                timeout=self._timeout
            )
            logger.info(f"[STRM] 通知完成：HTTP {r.status_code}")
        except Exception as e:
            logger.error(f"[STRM] Webhook 调用失败：{e}")

    def stop_service(self):
        """插件卸载时"""
        logger.info("[STRM] 插件已停止")
