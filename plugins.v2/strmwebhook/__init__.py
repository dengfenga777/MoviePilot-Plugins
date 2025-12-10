from typing import Any, List, Dict, Tuple
from app.plugins import _PluginBase
from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from app.log import logger
from app.utils.http import RequestUtils


class Strmwebhook(_PluginBase):
    """
    ⚠️ 类名必须和目录名一致，否则前端不显示
    """

    plugin_name = "远程 STRM Webhook"
    plugin_desc = "入库整理完成后，通过 Webhook 通知远端生成 STRM 文件"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/webhook.png"
    plugin_version = "1.0.0"
    plugin_author = "misaya"
    author_url = "https://github.com/dengfenga777"

    plugin_config_prefix = "strmwebhook_"
    plugin_order = 15
    auth_level = 1

    _enabled = False
    _webhook_url = ""

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._webhook_url = config.get("webhook_url", "")
        logger.info(f"[StrmWebhook] init, enabled={self._enabled}")

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用插件"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "webhook_url",
                            "label": "Webhook URL",
                            "placeholder": "http://remote-server/webhook"
                        }
                    }
                ]
            }
        ], {
            "enabled": False,
            "webhook_url": ""
        }

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self._enabled or not self._webhook_url:
            return

        try:
            RequestUtils().post_res(
                url=self._webhook_url,
                json=event.event_data
            )
            logger.info("STRM Webhook 已发送")
        except Exception as e:
            logger.error(f"STRM Webhook 发送失败: {e}")
