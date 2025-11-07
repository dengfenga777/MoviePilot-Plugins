from typing import Any, List, Dict, Tuple
from datetime import datetime
import time

from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.log import logger
from app.utils.http import RequestUtils


class StrmWebhook(_PluginBase):
    # 插件名称
    plugin_name = "STRM Webhook通知"
    # 插件描述
    plugin_desc = "入库成功后通过Webhook通知其他服务器生成STRM链接"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/webhook.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "MoviePilot"
    # 作者主页
    author_url = "https://github.com/dengfenga777"
    # 插件配置项ID前缀
    plugin_config_prefix = "strmwebhook_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _webhook_url = None
    _webhook_method = "POST"
    _webhook_headers = {}
    _retry_times = 3
    _timeout = 10
    _send_media_info = True
    _custom_fields = {}
    _secret_key = ""

    def init_plugin(self, config: dict = None):
        """
        初始化插件
        """
        if config:
            self._enabled = config.get("enabled", False)
            self._webhook_url = config.get("webhook_url", "")
            self._webhook_method = config.get("webhook_method", "POST")
            self._timeout = int(config.get("timeout", 10))
            self._retry_times = int(config.get("retry_times", 3))
            self._send_media_info = config.get("send_media_info", True)
            self._secret_key = config.get("secret_key", "")
            
            # 解析自定义请求头
            headers_str = config.get("webhook_headers", "")
            if headers_str:
                try:
                    self._webhook_headers = {}
                    for line in headers_str.strip().split("\n"):
                        if ":" in line:
                            key, value = line.split(":", 1)
                            self._webhook_headers[key.strip()] = value.strip()
                except Exception as e:
                    logger.error(f"解析请求头失败: {str(e)}")
                    self._webhook_headers = {}
            else:
                self._webhook_headers = {}
            
            # 默认添加 Content-Type
            if "Content-Type" not in self._webhook_headers:
                self._webhook_headers["Content-Type"] = "application/json"
            
            # 解析自定义字段
            custom_fields_str = config.get("custom_fields", "")
            if custom_fields_str:
                try:
                    self._custom_fields = {}
                    for line in custom_fields_str.strip().split("\n"):
                        if ":" in line:
                            key, value = line.split(":", 1)
                            self._custom_fields[key.strip()] = value.strip()
                except Exception as e:
                    logger.error(f"解析自定义字段失败: {str(e)}")
                    self._custom_fields = {}
            else:
                self._custom_fields = {}

        logger.info(f"STRM Webhook插件初始化完成，状态: {'启用' if self._enabled else '禁用'}")

    def get_state(self) -> bool:
        """
        获取插件状态
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        """
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'send_media_info',
                                            'label': '发送媒体详细信息',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'webhook_url',
                                            'label': 'Webhook URL',
                                            'placeholder': 'http://your-server.com/api/webhook',
                                            'hint': '接收通知的服务器地址'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'secret_key',
                                            'label': '密钥（可选）',
                                            'placeholder': '用于验证请求的密钥',
                                            'hint': '如设置，会在请求头中添加 X-Secret-Key'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'webhook_method',
                                            'label': '请求方法',
                                            'items': [
                                                {'title': 'POST', 'value': 'POST'},
                                                {'title': 'PUT', 'value': 'PUT'}
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'timeout',
                                            'label': '超时时间(秒)',
                                            'type': 'number',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'retry_times',
                                            'label': '重试次数',
                                            'type': 'number',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'webhook_headers',
                                            'label': '自定义请求头',
                                            'placeholder': 'Authorization: Bearer your-token\nX-Custom-Header: value',
                                            'hint': '每行一个，格式：Key: Value',
                                            'rows': 3
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'custom_fields',
                                            'label': '自定义字段',
                                            'placeholder': 'server_name: MyServer\napi_version: v1',
                                            'hint': '每行一个，格式：Key: Value，将添加到发送的数据中',
                                            'rows': 3
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '📢 入库成功后会自动通知配置的Webhook地址，发送文件路径、媒体类型、标题等信息'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "webhook_url": "",
            "webhook_method": "POST",
            "timeout": 10,
            "retry_times": 3,
            "send_media_info": True,
            "webhook_headers": "",
            "custom_fields": "",
            "secret_key": ""
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        return []

    def stop_service(self):
        """
        退出插件
        """
        pass

    @eventmanager.register(EventType.TransferComplete)
    def send_webhook(self, event: Event):
        """
        发送webhook通知
        """
        if not self._enabled:
            return
        
        if not self._webhook_url:
            logger.warn("❌ Webhook URL未配置，跳过发送")
            return

        event_data = event.event_data
        if not event_data:
            logger.warn("❌ 事件数据为空，跳过发送")
            return

        # 获取媒体信息
        mediainfo = event_data.get("mediainfo") or {}
        
        # 构建发送数据
        payload = {
            "event": "transfer_complete",
            "timestamp": datetime.now().isoformat(),
            "data": {
                "dest_path": event_data.get("dest"),  # 目标路径（入库后的路径）
                "src_path": event_data.get("src"),  # 源路径
                "dest_filename": event_data.get("dest_filename"),  # 目标文件名
            }
        }

        # 如果启用发送媒体详细信息
        if self._send_media_info:
            payload["data"].update({
                "media_type": mediainfo.get("type"),  # 媒体类型：电影/电视剧
                "title": mediainfo.get("title"),  # 标题
                "year": mediainfo.get("year"),  # 年份
                "season": mediainfo.get("season"),  # 季（电视剧）
                "episode": mediainfo.get("episode"),  # 集（电视剧）
                "tmdb_id": mediainfo.get("tmdb_id"),  # TMDB ID
                "imdb_id": mediainfo.get("imdb_id"),  # IMDB ID
                "tvdb_id": mediainfo.get("tvdb_id"),  # TVDB ID
                "douban_id": mediainfo.get("douban_id"),  # 豆瓣 ID
                "overview": mediainfo.get("overview"),  # 简介
                "category": mediainfo.get("category"),  # 分类
            })

        # 添加自定义字段
        if self._custom_fields:
            payload.update(self._custom_fields)

        # 准备请求头
        headers = self._webhook_headers.copy()
        
        # 如果设置了密钥，添加到请求头
        if self._secret_key:
            headers["X-Secret-Key"] = self._secret_key

        logger.info(f"🚀 准备发送Webhook通知到: {self._webhook_url}")
        logger.debug(f"📦 发送数据: {payload}")

        # 发送请求
        success = False
        for attempt in range(1, self._retry_times + 1):
            try:
                logger.info(f"📤 尝试发送 ({attempt}/{self._retry_times})...")
                
                request_utils = RequestUtils(
                    headers=headers,
                    timeout=self._timeout
                )
                
                if self._webhook_method == "POST":
                    response = request_utils.post_res(url=self._webhook_url, json=payload)
                else:
                    response = request_utils.put_res(url=self._webhook_url, json=payload)

                if response and response.status_code in [200, 201, 202]:
                    logger.info(f"✅ Webhook通知发送成功！状态码: {response.status_code}")
                    logger.info(f"📝 响应内容: {response.text[:200] if response.text else 'Empty'}")
                    success = True
                    break
                else:
                    status_code = response.status_code if response else "无响应"
                    logger.warn(f"⚠️ Webhook通知发送失败，状态码: {status_code}")
                    if response and response.text:
                        logger.warn(f"错误信息: {response.text[:200]}")
                        
            except Exception as e:
                logger.error(f"❌ 发送Webhook通知异常 (尝试 {attempt}/{self._retry_times}): {str(e)}")
            
            # 如果不是最后一次尝试，等待后重试
            if attempt < self._retry_times:
                wait_time = 3
                logger.info(f"⏳ 等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)

        if not success:
            logger.error(f"💥 Webhook通知发送失败，已重试 {self._retry_times} 次")
