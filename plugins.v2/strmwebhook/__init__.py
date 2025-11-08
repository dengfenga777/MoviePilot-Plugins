from typing import Any, List, Dict, Tuple, Optional
from datetime import datetime
import time
import requests

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
    plugin_version = "1.1.0"
    # 插件作者
    plugin_author = "MoviePilot"
    # 作者主页
    author_url = "https://github.com"
    # 插件配置项ID前缀
    plugin_config_prefix = "strmwebhook_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1 常量定义
    SUCCESS_CODES = [200, 201, 202, 204]  # 成功状态码
    RETRY_CODES = [408, 429, 500, 502, 503, 504]  # 可重试状态码
    MIN_TIMEOUT = 1  # 最小超时时间（秒）
    MAX_TIMEOUT = 60  # 最大超时时间（秒）
    MIN_RETRY = 1  # 最小重试次数
    MAX_RETRY = 10  # 最大重试次数

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
            self._webhook_url = config.get("webhook_url", "").strip()
            # 验证 URL 格式
            if self._enabled and self._webhook_url:
                if not self._webhook_url.startswith(('http://', 'https://')):
                    logger.error("❌ Webhook URL 格式错误，必须以 http:// 或 https:// 开头")
                    self._enabled = False
            
            self._webhook_method = config.get("webhook_method", "POST")
            # 限制超时时间范围
            try:
                self._timeout = max(self.MIN_TIMEOUT, min(int(config.get("timeout", 10)), self.MAX_TIMEOUT))
            except (ValueError, TypeError):
                self._timeout = 10
                logger.warn("⚠️ 超时时间配置无效，使用默认值: 10秒")
            
            # 限制重试次数范围
            try:
                self._retry_times = max(self.MIN_RETRY, min(int(config.get("retry_times", 3)), self.MAX_RETRY))
            except (ValueError, TypeError):
                self._retry_times = 3
                logger.warn("⚠️ 重试次数配置无效，使用默认值: 3次")
            
            self._send_media_info = config.get("send_media_info", True)
            self._secret_key = config.get("secret_key", "").strip()
            
            # 解析自定义请求头
            self._webhook_headers = self._parse_headers(config.get("webhook_headers", ""))
            
            # 解析自定义字段
            self._custom_fields = self._parse_custom_fields(config.get("custom_fields", ""))logger.info(f"✅ STRM Webhook插件初始化完成")
        logger.info(f"   状态: {'启用' if self._enabled else '禁用'}")
        if self._enabled and self._webhook_url:
            logger.info(f"   URL: {self._webhook_url}")logger.info(f"   方法: {self._webhook_method}")
            logger.info(f"   超时: {self._timeout}秒")
            logger.info(f"   重试: {self._retry_times}次")

    def _parse_headers(self, headers_str: str) -> dict:
        """
        解析自定义请求头
        """
        headers = {}
        if headers_str:
            try:
                for line in headers_str.strip().split("\n"):
                    line = line.strip()
                    if not line or line.startswith("#"):  # 跳过空行和注释
                        continue
                    if ":" in line:
                        key, value = line.split(":", 1)
                        headers[key.strip()] = value.strip()
            except Exception as e:
                logger.error(f"❌ 解析请求头失败: {str(e)}")
                return {}
         默认添加 Content-Type
        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        
        return headers

    def _parse_custom_fields(self, custom_fields_str: str) -> dict:
        """
        解析自定义字段
        """
        custom_fields = {}
        if custom_fields_str:
            try:
                for line in custom_fields_str.strip().split("\n"):
                    line = line.strip()
                    if not line or line.startswith("#"):  # 跳过空行和注释
                        continue
                    if ":" in line:
                        key, value = line.split(":", 1)
                        custom_fields[key.strip()] = value.strip()
            except Exception as e:
                logger.error(f"❌ 解析自定义字段失败: {str(e)}")
                return {}
        
        return custom_fields

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
        return [{
            "path": "/test_webhook",
            "endpoint": self.test_webhook,
            "methods": ["GET"],
            "summary": "测试Webhook连接",
            "description": "发送测试消息到配置的Webhook地址"
        }]

    def test_webhook(self) -> dict:
        """
        测试webhook连接
        """
        if not self._webhook_url:
            return {
                "success": False,
                "message": "❌ Webhook URL未配置"
            }
        
        test_payload = {
            "event": "test",
            "timestamp": datetime.now().isoformat(),
            "message": "这是一条来自 STRM Webhook 插件的测试消息",
            "plugin_version": self.plugin_version
        }
        
        # 添加自定义字段
        if self._custom_fields:
            test_payload.update(self._custom_fields)
        
        logger.info(f"🧪 开始测试Webhook连接: {self._webhook_url}")
        try:
            headers = self._webhook_headers.copy()
            if self._secret_key:
                headers["X-Secret-Key"] = self._secret_key
            
            request_utils = RequestUtils(headers=headers, timeout=self._timeout)
            
            if self._webhook_method == "POST":
                response = request_utils.post_res(url=self._webhook_url, json=test_payload)
            else:
                response = request_utils.put_res(url=self._webhook_url, json=test_payload)
            
            if response and response.status_code in self.SUCCESS_CODES:
                logger.info(f"✅ 测试成功！状态码: {response.status_code}")
                return {
                    "success": True,
                    "message": f"✅ 连接成功！状态码: {response.status_code}",
                    "status_code": response.status_code,
                    "response": response.text[:500] if response.text else "无响应内容"
                }
            else:
                status_code = response.status_code if response else "无响应"
                logger.warn(f"⚠️ 测试失败，状态码: {status_code}")
                return {
                    "success": False,
                    "message": f"⚠️ 连接失败，状态码: {status_code}",
                    "status_code": status_code,
                    "response": response.text[:500] if response and response.text else "无响应内容"
                
        except requests.exceptions.Timeout:
            logger.error(f"⏱️ 测试超时（{self._timeout}秒）")
            return {
                "success": False,
                "message": f"⏱️ 连接超时（{self._timeout}秒）"
            }
        except requests.exceptions.ConnectionError as e:
            logger.error(f"🔌 连接失败: {str(e)}")
            return {
                "success": False,
                "message": f"🔌 连接失败: 无法连接到目标服务器"
            }
        except Exception as e:
            logger.error(f"❌ 测试异常: {str(e)}", exc_info=True)
            return {
                "success": False,
                "message": f"❌ 测试失败: {str(e)}"
            }

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
                                'content': [        'component': 'VSwitch',
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
                                        }]
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
                                            'hint': '接收通知的服务器地址（必须以 http:// 或 https:// 开头）'
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
                                            'hint': '如设置，会在请求头中添加 X-Secret-Key'    }
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
                                                {'title': 'PUT', 'value': 'PUT'}        }
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
                                            'hint': f'范围: {self.MIN_TIMEOUT}-{self.MAX_TIMEOUT}秒'
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
                                'content': [        'component': 'VTextField',
                                        'props': {
                                            'model': 'retry_times',
                                            'label': '重试次数',
                                            'type': 'number',
                                            'hint': f'范围: {self.MIN_RETRY}-{self.MAX_RETRY}次'
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
                                            'placeholder': 'Authorization: Bearer your-token\nX-Custom-Header: value\n# 以 # 开头的行为注释',
                                            'hint': '每行一个，格式：Key: Value（支持 # 注释）',
                                            'rows': 4
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
                                            'placeholder': 'server_name: MyServer\napi_version: v1\n# 以 # 开头的行为注释',
                                            'hint': '每行一个，格式：Key: Value，将添加到发送的数据中（支持 # 注释）',
                                            'rows': 4
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
                                'content': [        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '📢 入库成功后会自动通知配置的Webhook地址，发送文件路径、媒体类型、标题等信息。可通过插件API测试连接：GET /api/v1/plugin/StrmWebhook/test_webhook'
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
                                'content': [        'component': 'VAlert',
                                        'props': {
                                            'type': 'success',
                                            'variant': 'tonal',
                                            'text': '💡 提示：配置完成后建议先使用测试功能验证连接是否正常'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }], {
            "enabled": False,
            "webhook_url": "",
            "webhook_method": "POST",
            "timeout": 10,
            "retry_times": 3,
            "send_media_info": True,
            "webhook_headers": "",
            "custom_fields": "",
            "secret_key": ""

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        return []

    def stop_service(self):
        """
        退出插件
        """
        logger.info("🛑 STRM Webhook插件已停止")eventmanager.register(EventType.TransferComplete)
    def send_webhook(self, event: Event):
        """
        发送webhook通知
        """
        if not self._enabled:
            return
        
        if not self._webhook_url:
            logger.warn("❌ Webhook URL未配置，跳过发送")
            return

        try:
            event_data = event.event_data
            if not event_data:
                logger.warn("❌ 事件数据为空，跳过发送")
                return

            # 获取媒体信息
            mediainfo = event_data.get("mediainfo") or {}
            
            # 构建发送数据
            payload = self._build_payload(event_data, mediainfo)
            
            # 发送请求
            success = self._send_request_with_retry(payload)
            
            if success:
                logger.info("🎉 Webhook通知处理完成")
            else:
                logger.error("💥 Webhook通知发送失败")
            
        except Exception as e:
            logger.error(f"❌ Webhook处理异常: {str(e)}", exc_info=True)

    def _build_payload(self, event_data: dict, mediainfo: dict) -> dict:
        """
        构建请求负载
        """
        payload = {
            "event": "transfer_complete",
            "timestamp": datetime.now().isoformat(),
            "plugin_version": self.plugin_version,
            "data": {
                "dest_path": event_data.get("dest"),  # 目标路径（入库后的路径）
                "src_path": event_data.get("src"),  # 源路径
                "dest_filename": event_data.get("dest_filename"),  # 目标文件名
            }
        }

        # 如果启用发送媒体详细信息
        if self._send_media_info and mediainfo:
            media_data = {
                "media_type": mediainfo.get("type"),  # 媒体类型：电影/电视剧
                "title": mediainfo.get("title"),  # 标题
                "year": mediainfo.get("year"),  # 年份
                "tmdb_id": mediainfo.get("tmdb_id"),  # TMDB ID
                "imdb_id": mediainfo.get("imdb_id"),  # IMDB ID
                "category": mediainfo.get("category"),  # 分类
             电视剧特有信息
            if mediainfo.get("type") == "tv":
                media_data.update({
                    "season": mediainfo.get("season"),  # 季
                    "episode": mediainfo.get("episode"),  # 集
                    "tvdb_id": mediainfo.get("tvdb_id"),  # TVDB ID
                }) 可选信息
            if mediainfo.get("overview"):
                media_data["overview"] = mediainfo.get("overview")  # 简介
            if mediainfo.get("douban_id"):
                media_data["douban_id"] = mediainfo.get("douban_id")  # 豆瓣 ID
            
            payload["data"].update(media_data) 添加自定义字段（添加到根级别）
        if self._custom_fields:
            payload.update(self._custom_fields)

        return payload

    def _send_request_with_retry(self, payload: dict) -> bool:
        """
        发送请求并重试
        """
        headers = self._webhook_headers.copy()
         如果设置了密钥，添加到请求头
        if self._secret_key:
            headers["X-Secret-Key"] = self._secret_key

        logger.info(f"🚀 准备发送Webhook通知")
        logger.info(f"   目标: {self._webhook_url}")
        logger.info(f"   方法: {self._webhook_method}")
        logger.debug(f"📦 发送数据: {payload}")

        for attempt in range(1, self._retry_times + 1):
            try:
                logger.info(f"📤 尝试发送 ({attempt}/{self._retry_times})...")
                
                request_utils = RequestUtils(
                    headers=headers,
                    timeout=self._timeout
                )
                
                # 发送请求
                if self._webhook_method == "POST":
                    response = request_utils.post_res(url=self._webhook_url, json=payload)
                else:
                    response = request_utils.put_res(url=self._webhook_url, json=payload) 检查响应
                if response:
                    if response.status_code in self.SUCCESS_CODES:
                        logger.info(f"✅ Webhook通知发送成功！状态码: {response.status_code}")
                        if response.text:
                            logger.debug(f"📝 响应内容: {response.text[:200]}")
                        return True
                    elif response.status_code in self.RETRY_CODES:
                        logger.warn(f"⚠️ 服务器临时错误 {response.status_code}，将重试")
                        if response.text:
                            logger.debug(f"错误信息: {response.text[:200]}")
                    else:
                        # 客户端错误（4xx），不重试
                        logger.error(f"❌ 客户端错误 {response.status_code}，停止重试")
                        if response.text:
                            logger.error(f"错误信息: {response.text[:200]}")
                        return False
                else:
                    logger.warn(f"⚠️ 无响应，将重试")
                        
            except requests.exceptions.Timeout:
                logger.error(f"⏱️ 请求超时（{self._timeout}秒）(尝试 {attempt}/{self._retry_times})")
            except requests.exceptions.ConnectionError as e:
                logger.error(f"🔌 连接失败 (尝试 {attempt}/{self._retry_times}): 无法连接到目标服务器")
                logger.debug(f"详细错误: {str(e)}")
            except requests.exceptions.RequestException as e:
                logger.error(f"🌐 请求异常 (尝试 {attempt}/{self._retry_times}): {str(e)}")except Exception as e:
                logger.error(f"❌ 未知异常 (尝试 {attempt}/{self._retry_times}): {str(e)}", exc_info=True) 如果不是最后一次尝试，等待后重试
            if attempt < self._retry_times:
                # 递增等待时间：3秒、6秒、9秒...，最多10秒
                wait_time = min(3 * attempt, 10)
                logger.info(f"⏳ 等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)

        logger.error(f"💥 Webhook通知发送失败，已重试 {self._retry_times} 次")
        return False
