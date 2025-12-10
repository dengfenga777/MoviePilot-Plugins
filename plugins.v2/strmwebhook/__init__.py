from datetime import datetime
from typing import Any, List, Dict, Tuple

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.schemas import TransferInfo
from app.core.context import MediaInfo
from app.utils.http import RequestUtils


class StrmWebhookNotify(_PluginBase):
    """
    入库完成 → 发送 Webhook 通知，用于 STRM 秒生成
    """

    # ===== 插件元信息 =====
    plugin_name = "STRM Webhook 通知"
    plugin_desc = "入库成功后发送 Webhook，供外部服务器生成 STRM 文件"
    plugin_icon = "webhook.png"
    plugin_version = "1.0.0"
    plugin_author = "misaya"
    author_url = "https://github.com"
    plugin_config_prefix = "strmwebhook_"
    plugin_order = 15
    auth_level = 1

    # ===== 配置 =====
    _enabled = False
    _webhook_url = ""
    _secret_key = ""
    _timeout = 10
    _retry = 3
    _send_mediainfo = True

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._webhook_url = config.get("webhook_url", "")
            self._secret_key = config.get("secret_key", "")
            self._timeout = int(config.get("timeout", 10))
            self._retry = int(config.get("retry", 3))
            self._send_mediainfo = config.get("send_mediainfo", True)

        logger.info(
            f"STRM Webhook 插件初始化完成："
            f"{'启用' if self._enabled else '禁用'}"
        )

    def get_state(self) -> bool:
        return self._enabled

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        插件配置 UI
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用 STRM Webhook 通知"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "webhook_url",
                            "label": "Webhook URL",
                            "placeholder": "http://strm-server:58090/mp_notify"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "secret_key",
                            "label": "密钥（可选）"
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "send_mediainfo",
                            "label": "发送媒体详细信息"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "timeout",
                            "label": "超时时间（秒）",
                            "type": "number"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "retry",
                            "label": "失败重试次数",
                            "type": "number"
                        }
                    }
                ]
            }
        ], {
            "enabled": False,
            "webhook_url": "",
            "secret_key": "",
            "timeout": 10,
            "retry": 3,
            "send_mediainfo": True
        }

    def get_page(self) -> List[dict]:
        return []

    # ===== 核心逻辑：只监听入库完成 =====

    @eventmanager.register(EventType.TransferComplete)
    def notify(self, event: Event):
        if not self._enabled:
            return

        event_data = event.event_data or {}
        transferinfo: TransferInfo = event_data.get("transferinfo")
        mediainfo: MediaInfo = event_data.get("mediainfo")

        if not transferinfo or not transferinfo.target_diritem:
            logger.warning("Webhook：未获取到 target_diritem，跳过")
            return

        dest_path = str(transferinfo.target_diritem.path)

        payload = {
            "event": "transfer_complete",
            "timestamp": datetime.now().isoformat(),
            "data": {
                "dest_path": dest_path
            }
        }

        if self._send_mediainfo and mediainfo:
            payload["data"].update({
                "media_type": mediainfo.type,
                "category": mediainfo.category,
                "title": mediainfo.title,
                "year": mediainfo.year,
                "season": getattr(mediainfo, "season", None),
                "episode": getattr(mediainfo, "episode", None),
                "tmdb_id": getattr(mediainfo, "tmdbid", None),
            })

        headers = {"Content-Type": "application/json"}
        if self._secret_key:
            headers["X-Secret-Key"] = self._secret_key

        logger.info(f"📡 STRM Webhook -> {self._webhook_url}")
        logger.info(f"📂 入库路径: {dest_path}")

        request = RequestUtils(headers=headers, timeout=self._timeout)

        for i in range(1, self._retry + 1):
            try:
                resp = request.post_res(self._webhook_url, json=payload)
                if resp and resp.status_code in (200, 201, 202):
                    logger.info("✅ STRM Webhook 发送成功")
                    return
                else:
                    logger.warning(f"Webhook 失败 [{i}/{self._retry}]")
            except Exception as e:
                logger.error(f"Webhook 异常 [{i}/{self._retry}]: {e}")

        logger.error("❌ STRM Webhook 发送失败（已达最大重试次数）")

    def stop_service(self):
        pass
