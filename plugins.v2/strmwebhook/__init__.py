# -*- coding: utf-8 -*-
"""
StrmWebhook 插件：在媒体整理/入库成功后，通过 Webhook 通知远程服务器生成 .strm。
"""

import json, time, requests
from app.log import logger
from app.plugins import _PluginBase
from app.core.event import eventmanager, EventType
from app.core.plugin import plugin_manager


class StrmWebhook(_PluginBase):
    plugin_name = "StrmWebhook"
    plugin_desc = "整理/入库完成后调用 Webhook，用于远程生成 STRM"
    plugin_version = "0.1"
    plugin_author = "dengfenga777"
    plugin_icon = "link_B.png"
    plugin_order = 99
    auth_level = 1

    _enabled = False
    _webhook_url = ""
    _secret = ""
    _timeout = 5

    def init_plugin(self, config: dict = None):
        if not config:
            return
        self._enabled = bool(config.get("enabled", False))
        self._webhook_url = (config.get("webhook_url") or "").strip()
        self._secret = (config.get("secret") or "").strip()
        try:
            self._timeout = int(config.get("timeout") or 5)
        except Exception:
            self._timeout = 5
        logger.info("[StrmWebhook] 插件已初始化 enabled=%s url=%s", self._enabled, self._webhook_url)

    def get_state(self):
        return self._enabled

    def get_form(self):
        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
                    {"component": "VTextField", "props": {"model": "webhook_url", "label": "Webhook 地址"}},
                    {"component": "VTextField", "props": {"model": "secret", "label": "Secret（可选）"}},
                    {"component": "VTextField", "props": {"model": "timeout", "label": "超时（秒）", "type": "number"}}
                ]
            }
        ], {"enabled": False, "webhook_url": "", "secret": "", "timeout": 5}

    def send_webhook(self, payload):
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["X-Strm-Secret"] = self._secret
        try:
            resp = requests.post(self._webhook_url, json=payload, headers=headers, timeout=self._timeout)
            logger.info("[StrmWebhook] 已发送 webhook：%s %s", resp.status_code, resp.text[:150])
        except Exception as e:
            logger.error("[StrmWebhook] 发送 webhook 失败：%s", e)

    def on_transfer_complete(self, event):
        if not self._enabled or not self._webhook_url:
            return
        data = getattr(event, "event_data", {}) or {}
        payload = {
            "event": "transfer.complete",
            "timestamp": int(time.time()),
            "data": data
        }
        self.send_webhook(payload)


@eventmanager.register(EventType.TransferComplete)
def _(event):
    plugin_manager.run_plugin_method("StrmWebhook", "on_transfer_complete", event)
