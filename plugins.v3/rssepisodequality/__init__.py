import datetime
import hashlib
import re
import traceback
import time
import json
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel

from app import schemas
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import Context, MediaInfo, MetaInfo, TorrentInfo, resolve_media_identity
from app.sdk.network import RssHelper
from app.sdk.services import RuleHelper, MediaServerHelper, DownloaderHelper
from app.plugins import _PluginBase
from app.schemas.types import MediaSource, SystemConfigKey, MediaType

from .engine import Library, UncertainLibrary, UnsafeSelection, decide, quality, file_scope, VIDEO
from .adapters import query_library, submit_selected

lock = Lock()


class DeleteHistoryRequest(BaseModel):
    """删除单条 RSS 历史的请求体。"""

    record_id: str


class _SilentMessageHelper:
    """
    Drop system messages emitted by the underlying MoviePilot chains.
    """

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def put(self, *args, **kwargs):
        logger.info("RSS剧集优选：已屏蔽系统通知")
        return None

    def __getattr__(self, item):
        return getattr(self._wrapped, item)


class SilentDownloadChain(DownloadChain):
    """
    DownloadChain variant that keeps download behavior but suppresses notifications.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.messagehelper = _SilentMessageHelper(self.messagehelper)

    selection_season = None
    last_downloader = None
    last_error = None
    selection_upgrade = None

    def download(self, content, download_dir, cookie=None, episodes=None, category=None,
                 label=None, downloader=None):
        try:
            if self.selection_season is None or not episodes:
                raise UnsafeSelection("没有明确选集计划，拒绝提交")
            helper = DownloaderHelper()
            configs = helper.get_configs()
            if not downloader:
                defaults = [name for name, conf in configs.items() if conf.default]
                if len(defaults) != 1:
                    raise UnsafeSelection("默认下载器不唯一或未设置")
                downloader = defaults[0]
            service = helper.get_service(downloader)
            if not service:
                raise UnsafeSelection("下载器服务不可用")
            from app.runtime.settings import get_runtime_setting
            builtin_tag = get_runtime_setting("TORRENT_TAG")
            self.last_downloader = service.name
            self.last_error = None
            return submit_selected(service, content, download_dir, cookie, category,
                                   label, self.selection_season, set(episodes), self.selection_upgrade, builtin_tag)
        except UnsafeSelection as err:
            self.last_error = str(err)
            logger.warning(f"RSS严格选集提交失败：{err}")
            return None, None, None, self.last_error
        except Exception:
            self.last_error = "下载器或种子解析发生异常，未启动整包回退"
            logger.warning(self.last_error)
            return None, None, None, "严格选集提交失败，保留暂停任务供检查"

    def post_message(self, *args, **kwargs):
        logger.info("RSS剧集优选：已屏蔽下载链通知")
        return None

    async def async_post_message(self, *args, **kwargs):
        logger.info("RSS剧集优选：已屏蔽下载链异步通知")
        return None


class SilentSubscribeChain(SubscribeChain):
    """
    SubscribeChain variant that keeps subscription behavior but suppresses notifications.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.messagehelper = _SilentMessageHelper(self.messagehelper)

    def post_message(self, *args, **kwargs):
        logger.info("RSS剧集优选：已屏蔽订阅链通知")
        return None

    async def async_post_message(self, *args, **kwargs):
        logger.info("RSS剧集优选：已屏蔽订阅链异步通知")
        return None


class RssEpisodeQuality(_PluginBase):
    # 插件名称
    plugin_name = "RSS剧集优选"
    # 插件描述
    plugin_desc = "跨 RSS 源优选剧集，严格媒体库查重、缺集选下、可配置实际版本升级，不发送系统通知。"
    # 插件图标
    plugin_icon = "rss.png"
    # 插件版本
    plugin_version = "1.1.0"
    # 插件作者
    plugin_author = "jxxghp / misaya"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "rssepisodequality_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    _cache_path: Optional[Path] = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _notify: bool = False
    _onlyonce: bool = False
    _address: str = ""
    _include: str = ""
    _exclude: str = ""
    _proxy: bool = False
    _filter: bool = True
    _clear: bool = False
    _clearflag: bool = False
    _action: str = "download"
    _save_path: str = ""
    _size_range: str = ""
    _smart_quality: bool = True
    _skip_unknown_episodes: bool = True
    _history_limit: int = 1000
    _library_servers: str = ""
    _mode: str = "missing"
    _upgrade_policy: str = "balanced"
    _query_retries: int = 3
    _absence_minutes: int = 10
    _pending_hours: int = 6


    def init_plugin(self, config: dict = None):

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self.__validate_and_fix_config(config=config)
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = False
            self._onlyonce = config.get("onlyonce")
            self._address = config.get("address")
            self._include = config.get("include")
            self._exclude = config.get("exclude")
            self._proxy = config.get("proxy")
            self._filter = config.get("filter", True)
            self._smart_quality = config.get("smart_quality", True)
            self._skip_unknown_episodes = config.get("skip_unknown_episodes", True)
            self._clear = config.get("clear")
            self._action = config.get("action") or "download"
            self._save_path = str(config.get("save_path") or "").strip()
            self._size_range = config.get("size_range")
            self._library_servers = str(config.get("library_servers") or "").strip()
            self._mode = config.get("mode") or "missing"
            self._upgrade_policy = config.get("upgrade_policy") or "balanced"
            self._query_retries = int(config.get("query_retries", 3))
            self._absence_minutes = int(config.get("absence_minutes", 10))
            self._pending_hours = int(config.get("pending_hours", 6))

        self.__migrate_history_identity()

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("RSS剧集优选服务启动，立即运行一次")
            self._scheduler.add_job(func=self.check, trigger='date',
                                    run_date=datetime.datetime.now(
                                        tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                    )

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

        if self._onlyonce or self._clear:
            # 关闭一次性开关
            self._onlyonce = False
            # 记录清理缓存设置
            self._clearflag = self._clear
            # 关闭清理缓存开关
            self._clear = False
            # 保存设置
            self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    def __migrate_history_identity(self) -> None:
        """幂等迁移旧媒体身份，并为历史记录补充稳定记录 ID。"""
        history = self.get_data("history")
        if not isinstance(history, list):
            return

        changed = False
        for item in history:
            if not isinstance(item, dict):
                continue
            migrated = dict(item)
            media_source, media_id = resolve_media_identity(
                media_source=item.get("media_source"),
                media_id=item.get("media_id"),
            )
            if not media_source:
                media_source, media_id = resolve_media_identity(
                    media_source=MediaSource.TMDB,
                    media_id=item.get("tmdbid"),
                )
            if media_source:
                migrated.pop("tmdbid", None)
                migrated["media_source"] = media_source.value
                migrated["media_id"] = media_id
            if not str(migrated.get("record_id") or "").strip():
                migrated["record_id"] = self.__history_record_id(migrated)
            if migrated != item:
                item.clear()
                item.update(migrated)
                changed = True

        if len(history) > self._history_limit:
            del history[:-self._history_limit]
            changed = True
        if changed:
            self.save_data("history", history)

    @staticmethod
    def __digest(value: str) -> str:
        """生成短而稳定的记录指纹。"""
        return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:32]

    def __rss_key(self, result: dict) -> str:
        """优先使用 RSS GUID 或链接生成去重键，标题仅作为最后回退。"""
        for field in ("guid", "link", "enclosure", "title"):
            value = str(result.get(field) or "").strip()
            if value:
                return self.__digest(f"{field}:{value}")
        return ""

    def __history_record_id(self, item: dict) -> str:
        """为新旧历史生成可用于精确删除的稳定 ID。"""
        identity = "\x1f".join(
            str(item.get(field) or "")
            for field in ("key", "title", "time", "media_source", "media_id")
        )
        return self.__digest(identity)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除RSS剧集优选历史记录",
                "response_model": schemas.Response[None]
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "RssEpisodeQuality",
                "name": "RSS剧集优选服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.check,
                "kwargs": {}
            }]
        elif self._enabled:
            return [{
                "id": "RssEpisodeQuality",
                "name": "RSS剧集优选服务",
                "trigger": "interval",
                "func": self.check,
                "kwargs": {"minutes": 30}
            }]
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
                                    'md': 4
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
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
                                            'model': 'action',
                                            'label': '动作',
                                            'items': [
                                                {'title': '订阅', 'value': 'subscribe'},
                                                {'title': '下载', 'value': 'download'}
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'address',
                                            'label': 'RSS地址',
                                            'rows': 3,
                                            'placeholder': '每行一个RSS地址'
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
                                            'model': 'include',
                                            'label': '包含',
                                            'placeholder': '支持正则表达式'
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
                                            'model': 'exclude',
                                            'label': '排除',
                                            'placeholder': '支持正则表达式'
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
                                            'model': 'size_range',
                                            'label': '种子大小(GB)',
                                            'placeholder': '如：3 或 3-5'
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
                                            'model': 'save_path',
                                            'label': '保存目录',
                                            'placeholder': '下载时有效，留空自动'
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'proxy',
                                            'label': '使用代理服务器',
                                        }
                                    }
                                ]
                            }, {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4,
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'filter',
                                            'label': '使用MP订阅优先级规则',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear',
                                            'label': '清理历史记录',
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
                                'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'smart_quality',
                                        'label': '跨RSS优选排序（查重和提交去重始终启用）',
                                    },
                                }],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'skip_unknown_episodes',
                                        'label': '跳过标题无集数资源（关闭后读取种子文件确认）',
                                    },
                                }],
                            },
                        ],
                    }
                ]
            }
        ] + [{
            'component': 'VCard',
            'content': [{'component': 'VCardText', 'content': [
                {'component': 'VTextField', 'props': {'model': 'library_servers', 'label': '查重媒体服务器名称（逗号分隔；留空使用全部启用服务）'}},
                {'component': 'VSelect', 'props': {'model': 'mode', 'label': '下载模式', 'items': [
                    {'title': '只补缺集', 'value': 'missing'}, {'title': '补缺集并允许实际版本升级', 'value': 'upgrade'}]}},
                {'component': 'VSelect', 'props': {'model': 'upgrade_policy', 'label': '升级判断', 'items': [
                    {'title': '分辨率与HDR均不降级，至少一项提升', 'value': 'balanced'},
                    {'title': '只比较分辨率（可能改变HDR版本）', 'value': 'resolution'},
                    {'title': '同时比较分辨率、HDR与来源档次', 'value': 'source'}]}},
                {'component': 'VTextField', 'props': {'model': 'query_retries', 'label': '查询重试次数（1–5）', 'type': 'number'}},
                {'component': 'VTextField', 'props': {'model': 'absence_minutes', 'label': '删除集数复核间隔（1–120分钟）', 'type': 'number'}},
                {'component': 'VTextField', 'props': {'model': 'pending_hours', 'label': '已提交任务重复保护（1–72小时）', 'type': 'number'}},
            ]}],
        }], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "cron": "*/30 * * * *",
            "address": "",
            "include": "",
            "exclude": "",
            "proxy": False,
            "clear": False,
            "filter": True,
            "smart_quality": True,
            "skip_unknown_episodes": True,
            "action": "download",
            "save_path": "",
            "size_range": "",
            "library_servers": "", "mode": "missing", "upgrade_policy": "balanced",
            "query_retries": 3, "absence_minutes": 10, "pending_hours": 6,
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return self.__decision_page() + [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]
        # 数据按时间降序排序
        historys = sorted((item for item in historys if isinstance(item, dict)),
                          key=lambda x: str(x.get('time') or ''), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            title = history.get("title")
            record_id = history.get("record_id")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            "component": "VDialogCloseBtn",
                            "props": {
                                'innerClass': 'absolute top-0 right-0',
                            },
                            'events': {
                                'click': {
                                    'api': 'plugin/RssEpisodeQuality/delete_history',
                                    'method': 'post',
                                    'params': {
                                        'record_id': record_id
                                    }
                                }
                            },
                        },
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': poster,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VCardTitle',
                                            'props': {
                                                'class': 'pa-1 pe-5 break-words whitespace-break-spaces'
                                            },
                                            'text': title
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'类型：{mtype}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'时间：{time_str}'
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        return self.__decision_page() + [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]

    def __decision_page(self):
        records = self.get_data("decisions_v1") or []
        if not isinstance(records, list):
            return []
        labels = {"deferred": "暂缓", "skipped": "跳过", "planned": "待提交",
                  "failed": "失败", "submitted": "已提交", "reserved": "已记录提交计划"}
        return [{"component": "VCard", "content": [
            {"component": "VCardTitle", "text": "最近下载判断"},
            {"component": "VCardText", "content": [
                {"component": "div", "text":
                 f"{record.get('time')} · {labels.get(record.get('status'), record.get('status'))} · "
                 f"{record.get('title')} · {record.get('reason') or record.get('reasons')} · "
                 f"选择集数：{record.get('selected', [])}"}
                for record in reversed(records[-50:]) if isinstance(record, dict)
            ]},
        ]}]

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def delete_history(self, payload: DeleteHistoryRequest) -> schemas.Response[None]:
        """
        按记录 ID 精确删除同步历史记录。
        """
        record_id = payload.record_id.strip()
        historys = self.get_data('history')
        if not isinstance(historys, list) or not historys:
            return schemas.Response(success=False, message="未找到历史记录")
        remaining = [
            item for item in historys
            if not isinstance(item, dict) or item.get("record_id") != record_id
        ]
        if len(remaining) == len(historys):
            return schemas.Response(success=False, message="未找到指定历史记录")
        self.save_data('history', remaining)
        return schemas.Response(success=True, message="删除成功")

    def __update_config(self):
        """
        更新设置
        """
        self.update_config({
            "enabled": self._enabled,
            "notify": False,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "address": self._address,
            "include": self._include,
            "exclude": self._exclude,
            "proxy": self._proxy,
            "clear": self._clear,
            "filter": self._filter,
            "smart_quality": self._smart_quality,
            "skip_unknown_episodes": self._skip_unknown_episodes,
            "action": self._action,
            "save_path": self._save_path,
            "size_range": self._size_range,
            "library_servers": self._library_servers,
            "mode": self._mode,
            "upgrade_policy": self._upgrade_policy,
            "query_retries": self._query_retries,
            "absence_minutes": self._absence_minutes,
            "pending_hours": self._pending_hours,
        })

    def __audit(self, media, title, status, **details):
        """Bounded decision history contains no RSS URLs or service credentials."""
        record = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
                  "media": f"{media.media_source.value}:{media.media_id}",
                  "title": title, "status": status, **details}
        records = self.get_data("decisions_v1") or []
        if not isinstance(records, list):
            raise ValueError("下载决策记录格式异常")
        self.save_data("decisions_v1", (records + [record])[-500:])
        logger.info(f"RSS下载决策：{json.dumps(record, ensure_ascii=False)}")

    def __library(self, media, services):
        key = json.dumps([sorted(services), media.media_source.value, str(media.media_id)])
        snapshots = self.get_data("library_snapshot_v2") or {}
        if not isinstance(snapshots, dict):
            raise ValueError("媒体库缓存格式异常")
        now = time.time()
        snapshots = {k: v for k, v in snapshots.items() if isinstance(v, dict)
                     and 0 <= now-float(v.get("checked_at", 0)) < 86400}
        previous = snapshots.get(key)
        for attempt in range(self._query_retries):
            try:
                library = query_library(services, media)
                break
            except UncertainLibrary as err:
                logger.warning(f"{media.title_year} 媒体库查询 {attempt+1}/{self._query_retries}：{err}")
                if attempt + 1 == self._query_retries:
                    raise
                time.sleep(1)
        coverage = library.coverage()
        if previous and any(not set(episodes).issubset(set(coverage.get(season, [])))
                            for season, episodes in previous["coverage"].items()):
            fingerprint = json.dumps(coverage, sort_keys=True)
            pending = previous.get("pending") or {}
            if pending.get("coverage") != fingerprint:
                previous["pending"] = {"coverage": fingerprint, "since": now}
                snapshots[key] = previous
                self.save_data("library_snapshot_v2", snapshots)
                raise UncertainLibrary("已确认集数减少，等待下一轮成功查询复核")
            if now - float(pending["since"]) < self._absence_minutes * 60:
                raise UncertainLibrary("已确认集数减少，尚未达到删除复核间隔")
        snapshots[key] = {"checked_at": now, "coverage": coverage}
        self.save_data("library_snapshot_v2", snapshots)
        return library

    def __fetch_rss(self, url: str) -> List[dict]:
        """读取单个 RSS 源；任何异常只影响当前源。"""
        try:
            results = RssHelper().parse(url, proxy=self._proxy, timeout=20)
        except Exception as err:
            logger.error(
                f"刷新 RSS 源发生未预期异常，已跳过：{url}，"
                f"错误：{str(err)} - {traceback.format_exc()}"
            )
            return []

        if results is None:
            logger.warning(f"RSS 链接可能已经过期：{url}")
            return []
        if results is False:
            logger.error(f"RSS 请求或解析失败：{url}")
            return []
        if not isinstance(results, list):
            logger.error(f"RSS 返回了不支持的数据类型：{url}，类型：{type(results).__name__}")
            return []
        if not results:
            logger.info(f"RSS 没有可处理的数据：{url}")
        return results

    def __persist_history(self, history: List[dict]) -> None:
        """限制历史数量并立即保存当前源已经完成的结果。"""
        if len(history) > self._history_limit:
            del history[:-self._history_limit]
        self.save_data("history", history)

    def check(self):
        """串行执行 RSS 检查，避免定时任务与手动运行重叠。"""
        if not lock.acquire(blocking=False):
            logger.warning("RSS剧集优选正在运行，本次任务跳过")
            return
        try:
            self.__check()
        except Exception as err:
            logger.error(f'RSS剧集优选运行失败：{str(err)} - {traceback.format_exc()}')
        finally:
            lock.release()

    @staticmethod
    def __episode_keys(meta, mediainfo) -> List[str]:
        """Use stable media identity and explicit episode coverage; never guess a pack's coverage."""
        if mediainfo.type != MediaType.TV or not mediainfo.media_id:
            return []
        seasons = list(getattr(meta, "season_list", None) or [])
        episodes = list(getattr(meta, "episode_list", None) or [])
        season = getattr(meta, "begin_season", None)
        if season is None or len(seasons) > 1 or not episodes:
            return []
        source = getattr(mediainfo.media_source, "value", str(mediainfo.media_source))
        return [f"{source}:{mediainfo.media_id}:S{int(season)}:E{int(ep)}"
                for ep in sorted(set(episodes))]

    @staticmethod
    def __quality_rank(title: str, priority=0) -> Tuple[int, ...]:
        """MP priority scores are higher for better rules; size is deliberately excluded."""
        name = title or ""
        resolution = 2160 if re.search(r"(?i)\b(2160[pi]|4k|uhd)\b", name) else (
            1080 if re.search(r"(?i)\b1080[pi]\b", name) else (
                720 if re.search(r"(?i)\b720[pi]\b", name) else 0))
        source = (4 if re.search(r"(?i)\bremux\b", name) else
                  3 if re.search(r"(?i)blu[ ._-]?ray|\bb[dr]rip\b", name) else
                  2 if re.search(r"(?i)web[ ._-]?dl", name) else
                  1 if re.search(r"(?i)web[ ._-]?rip|\bhdtv\b", name) else 0)
        hdr = int(bool(re.search(r"(?i)\b(?:hdr\w*|dv|dovi)\b|dolby[ ._-]?vision", name)))
        fps = int(bool(re.search(r"(?i)\b(?:60|50|120)\s*fps\b", name)))
        hevc = int(bool(re.search(r"(?i)(?:[hx][ ._-]?265|hevc)", name)))
        return (int(priority or 0), resolution, source, hdr, fps, hevc)

    def __torrent_scope(self, context, downloader):
        from torrentool.api import Torrent
        content, _, _ = downloader.download_torrent(context.torrent_info)
        if not isinstance(content, bytes):
            raise UnsafeSelection("无法取得可信种子文件集数，暂缓")
        torrent = Torrent.from_string(content)
        season_episodes = {}
        for file in torrent.files:
            if Path(file.name).suffix.lower() not in VIDEO:
                continue
            season, episodes = file_scope(file.name, None)
            if season is None or not episodes:
                raise UnsafeSelection("种子视频文件没有明确季集编号")
            season_episodes.setdefault(season, set()).update(episodes)
        if len(season_episodes) != 1:
            raise UnsafeSelection("种子不是明确单季资源")
        season, episodes = next(iter(season_episodes.items()))
        return content, season, sorted(episodes)

    def __pending_active(self, record, services, cache):
        identity = (record.get("downloader"), record.get("hash"))
        if not all(identity) or identity[0] not in services:
            raise UncertainLibrary("已提交记录对应下载器不可用")
        if identity not in cache:
            tasks, error = services[identity[0]].instance.get_torrents(ids=identity[1])
            if error or tasks is None:
                raise UncertainLibrary("无法查询已提交任务状态")
            cache[identity] = tasks
        tasks = cache[identity]
        if not tasks:
            return False
        progress = [float(task.get("progress", -1)) for task in tasks]
        if any(not 0 <= value <= 1 for value in progress):
            raise UncertainLibrary("下载任务进度无效")
        return any(value < 1 for value in progress) or (
            time.time() - float(record.get("submitted_at", 0)) < self._pending_hours * 3600)

    def __check(self):
        """
        收集全部 RSS 剧集候选，优先提交高质量版本
        """
        if not self._address:
            return
        # 读取历史记录
        if self._clearflag:
            history = []
        else:
            history: List[dict] = self.get_data('history') or []
            if not isinstance(history, list):
                logger.warning("RSS剧集优选历史数据格式异常，将从空历史重新开始")
                history = []
        history = [item for item in history if isinstance(item, dict)]
        downloadchain = SilentDownloadChain()
        subscribechain = SilentSubscribeChain()
        rulehelper = RuleHelper()
        filter_groups = self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
        candidates = []
        feed_results = []
        for url in self._address.splitlines():
            url = url.strip()
            if url:
                logger.info("剧集优选：读取 RSS 源")
                feed_results.extend(self.__fetch_rss(url))
        seen_feed_keys = set()
        for result in feed_results:
            try:
                title = result.get("title")
                description = result.get("description")
                enclosure = result.get("enclosure")
                link = result.get("link")
                size = result.get("size")
                pubdate: datetime.datetime = result.get("pubdate")
                # 检查是否处理过
                rss_key = self.__rss_key(result)
                if not title or not rss_key:
                    continue
                if rss_key in seen_feed_keys:
                    continue
                seen_feed_keys.add(rss_key)
                # 检查规则
                if self._include and not re.search(r"%s" % self._include,
                                                   f"{title} {description}", re.IGNORECASE):
                    logger.info(f"{title} - {description} 不符合包含规则")
                    continue
                if self._exclude and re.search(r"%s" % self._exclude,
                                               f"{title} {description}", re.IGNORECASE):
                    logger.info(f"{title} - {description} 不符合排除规则")
                    continue
                if self._size_range:
                    sizes = [float(_size) * 1024 ** 3 for _size in self._size_range.split("-")]
                    if len(sizes) == 1 and float(size) < sizes[0]:
                        logger.info(f"{title} - 种子大小不符合条件")
                        continue
                    elif len(sizes) > 1 and not sizes[0] <= float(size) <= sizes[1]:
                        logger.info(f"{title} - 种子大小不在指定范围")
                        continue
                # 识别媒体信息
                # RSS 简介可能包含播出总集数、整季范围等，不能用于下载覆盖范围。
                # 标题没有明确集数时保持未知，由 skip_unknown_episodes 决定是否跳过。
                meta = MetaInfo(title=title)
                if not meta.name:
                    logger.warning(f"{title} 未识别到有效数据")
                    continue
                mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
                if not mediainfo:
                    logger.warning(f'未识别到媒体信息，标题：{title}')
                    continue
                if mediainfo.type != MediaType.TV:
                    logger.info(f"{title} - RSS剧集优选仅处理电视剧，跳过{mediainfo.type.value}")
                    continue
                # 种子
                torrentinfo = TorrentInfo(
                    title=title,
                    description=description,
                    enclosure=enclosure,
                    page_url=link,
                    size=size,
                    pubdate=pubdate.strftime("%Y-%m-%d %H:%M:%S") if pubdate else None,
                    site_proxy=self._proxy,
                )
                # 过滤种子
                if self._filter:
                    active_groups = rulehelper.get_rule_group_by_media(
                        media=mediainfo,
                        group_names=filter_groups
                    )
                    active_group_names = [group.name for group in active_groups]
                    media_category = mediainfo.category or "未分类"
                    if not active_group_names:
                        logger.info(
                            f"{title} - 未匹配到适用订阅规则组，"
                            f"类型：{mediainfo.type.value}，分类：{media_category}"
                        )
                        continue
                    logger.info(
                        f"{title} - 使用订阅规则组：{', '.join(active_group_names)}，"
                        f"类型：{mediainfo.type.value}，分类：{media_category}"
                    )
                    matched_torrents = self.chain.filter_torrents(
                        rule_groups=filter_groups,
                        torrent_list=[torrentinfo],
                        mediainfo=mediainfo
                    )
                    if not matched_torrents:
                        logger.info(f"{title} - 不匹配订阅规则组：{', '.join(active_group_names)}")
                        continue
                    torrentinfo = matched_torrents[0]
                    logger.info(
                        f"{title} - 已命中订阅规则组：{', '.join(active_group_names)}，"
                        f"优先级：{getattr(torrentinfo, 'pri_order', '-')}"
                    )
                context = Context(meta_info=meta, media_info=mediainfo, torrent_info=torrentinfo)
                smart = self._smart_quality and self._action == "download" and mediainfo.type == MediaType.TV
                episode_keys = self.__episode_keys(meta, mediainfo) if smart else []
                if smart and not episode_keys and self._skip_unknown_episodes:
                    logger.info(f"{title} - 剧集优选：无法识别明确集数，跳过以免误下载整季/更新包")
                    continue
                rank = self.__quality_rank(title, getattr(torrentinfo, "pri_order", 0))
                candidates.append((context, rss_key, episode_keys, rank, smart))
            except Exception as err:
                logger.error(f'解析RSS候选出错：{err} - {traceback.format_exc()}')
        # Higher MP rule scores first. Equal-grade variants are ordered by size only as a tie-breaker.
        # Size cannot itself cause an upgrade or a second download.
        if self._smart_quality and self._action == "download":
            sorted_tv = iter(sorted((item for item in candidates if item[4]),
                                    key=lambda item: (item[3], float(item[0].torrent_info.size or 0)),
                                    reverse=True))
            candidates = [next(sorted_tv) if item[4] else item for item in candidates]
        ledger = self.get_data("pending_downloads_v2") or {}
        if not isinstance(ledger, dict):
            raise ValueError("待完成下载记录格式异常，停止本轮")
        now = time.time()
        if any(not isinstance(value, dict) for value in ledger.values()):
            raise ValueError("待完成下载记录条目异常")
        download_services = DownloaderHelper().get_services()
        pending_cache = {}
        helper = MediaServerHelper()
        services = helper.get_services()
        if self._library_servers:
            names = {name.strip() for name in self._library_servers.split(",") if name.strip()}
            if not names.issubset(services):
                raise ValueError("指定查重媒体服务器未全部启用或不可用")
            services = {name: services[name] for name in names}
        enabled_names = set(helper.get_configs())
        if not self._library_servers and set(services) != enabled_names:
            raise ValueError("部分已启用媒体服务器没有运行实例，本轮停止")
        libraries = {}
        for context, rss_key, episode_keys, rank, smart in candidates:
            meta, mediainfo, torrentinfo = context.meta_info, context.media_info, context.torrent_info
            title = torrentinfo.title
            try:
                selected = set()
                try:
                    identity = (mediainfo.media_source.value, str(mediainfo.media_id))
                    if identity not in libraries:
                        libraries[identity] = self.__library(mediainfo, services)
                    library = libraries[identity]
                except UncertainLibrary as err:
                    libraries[identity] = None
                    self.__audit(mediainfo, title, "deferred", reason=str(err))
                    continue
                if library is None:
                    continue
                seasons = list(getattr(meta, "season_list", None) or [])
                requested = sorted(set(meta.episode_list or []))
                torrent_content = None
                chosen_season = meta.begin_season
                if not requested and not self._skip_unknown_episodes and len(seasons) <= 1:
                    try:
                        torrent_content, chosen_season, requested = self.__torrent_scope(context, downloadchain)
                        meta.begin_season = chosen_season
                        meta.end_season = None
                    except UnsafeSelection as err:
                        self.__audit(mediainfo, title, "deferred", reason=str(err))
                        continue
                if chosen_season is None or len(seasons) > 1 or not requested:
                    self.__audit(mediainfo, title, "deferred", reason="候选没有可信的单季集数")
                    continue
                for episode in requested:
                    pending_key = f"{mediainfo.media_source.value}:{mediainfo.media_id}:S{int(meta.begin_season)}:E{episode}"
                    if (int(meta.begin_season), episode) in library.episodes and pending_key in ledger:
                        record = ledger[pending_key]
                        if (time.time() - float(record.get("submitted_at", 0)) >= self._pending_hours * 3600
                                and not self.__pending_active(record, download_services, pending_cache)):
                            del ledger[pending_key]
                candidate = quality(title)
                axes = {"resolution": (0,), "balanced": (0, 1), "source": (0, 1, 2)}[self._upgrade_policy]
                plan = decide(requested, int(meta.begin_season), library, candidate,
                              lambda item: quality(str(item.get("Path") or item.get("Name") or ""), item),
                              mode=self._mode, upgrade_axes=axes)
                # Any in-flight download protects its episodes across RSS sources, even if
                # a better candidate arrives; reconsider upgrades after transfer or expiry.
                blocked = []
                for ep in list(plan.selected):
                    pending_key = f"{mediainfo.media_source.value}:{mediainfo.media_id}:S{int(meta.begin_season)}:E{ep}"
                    if pending_key in ledger:
                        if self.__pending_active(ledger[pending_key], download_services, pending_cache):
                            plan.selected.remove(ep)
                            plan.reasons[str(ep)] = "已有下载中或待入库的提交任务"
                            blocked.append(ep)
                        else:
                            del ledger[pending_key]
                selected = plan.selected
                details = {"season": int(meta.begin_season), "requested": requested,
                           "existing": library.coverage().get(str(int(meta.begin_season)), []),
                           "missing": sorted(plan.missing), "upgrades": sorted(plan.upgrades),
                           "selected": sorted(selected), "pending": sorted(blocked),
                           "candidate_quality": candidate, "reasons": plan.reasons}
                self.__audit(mediainfo, title, "planned" if selected else "skipped", **details)
                if not selected:
                    continue
                # 下载或订阅
                if self._action == "download":
                    # Every download goes through the strict qBittorrent adapter.
                    downloadchain.selection_season = int(meta.begin_season)
                    downloadchain.selection_upgrade = {"episodes": set(plan.upgrades) & selected,
                                                       "quality": candidate, "axes": axes}
                    # Save exact torrent identity before any downloader side effect. A crash
                    # after starting can then be reconciled against the real task on restart.
                    if torrent_content is None:
                        torrent_content, _, _ = downloadchain.download_torrent(torrentinfo)
                    if not isinstance(torrent_content, bytes):
                        raise UnsafeSelection("无法验证种子文件，拒绝磁力或整包回退")
                    from torrentool.api import Torrent
                    intent_hash = Torrent.from_string(torrent_content).info_hash
                    if not intent_hash:
                        raise UnsafeSelection("种子缺少可确认的任务Hash")
                    downloader_name = getattr(torrentinfo, "site_downloader", None)
                    if not downloader_name:
                        defaults = [name for name, config in DownloaderHelper().get_configs().items() if config.default]
                        if len(defaults) != 1:
                            raise UnsafeSelection("默认下载器不唯一或未设置")
                        downloader_name = defaults[0]
                    if downloader_name not in download_services or download_services[downloader_name].type != "qbittorrent":
                        raise UnsafeSelection("下载器不可用或不支持严格选集")
                    for ep in selected:
                        key = f"{mediainfo.media_source.value}:{mediainfo.media_id}:S{int(meta.begin_season)}:E{ep}"
                        ledger[key] = {"submitted_at": time.time(), "hash": intent_hash,
                                       "downloader": downloader_name, "quality": candidate,
                                       "title": title, "state": "reserved"}
                    self.save_data("pending_downloads_v2", ledger)
                    self.__audit(mediainfo, title, "reserved", torrent_hash=intent_hash, **details)
                    result = downloadchain.download_single(
                        context=Context(
                            meta_info=meta,
                            media_info=mediainfo,
                            torrent_info=torrentinfo,
                        ),
                        save_path=self._save_path or None,
                        episodes=set(selected),
                        torrent_content=torrent_content,
                        downloader=downloader_name,
                        username="RSS剧集优选"
                    )
                    if not result:
                        # Release an intent only after a successful empty downloader lookup.
                        # Paused tasks and unreachable downloaders retain duplicate protection.
                        tasks, error = download_services[downloader_name].instance.get_torrents(ids=intent_hash)
                        if not error and tasks is not None and not tasks:
                            for ep in selected:
                                key = f"{mediainfo.media_source.value}:{mediainfo.media_id}:S{int(meta.begin_season)}:E{ep}"
                                ledger.pop(key, None)
                            self.save_data("pending_downloads_v2", ledger)
                        self.__audit(mediainfo, title, "failed", reason=downloadchain.last_error or "下载链未接受资源", **details)
                        continue
                else:
                    # 检查是否在订阅中
                    subflag = subscribechain.exists(mediainfo=mediainfo, meta=meta)
                    if subflag:
                        logger.info(f'{mediainfo.title_year} {meta.season} 正在订阅中')
                        continue
                    # 添加订阅
                    subscribe_id, subscribe_message = subscribechain.add(
                        title=mediainfo.title,
                        year=mediainfo.year,
                        mtype=mediainfo.type,
                        media_source=mediainfo.media_source,
                        media_id=mediainfo.media_id,
                        season=meta.begin_season,
                        exist_ok=True,
                        message=False,
                        username="RSS剧集优选"
                    )
                    if not subscribe_id:
                        logger.error(
                            f"{mediainfo.title_year} {meta.season} 添加订阅失败："
                            f"{subscribe_message or '未知错误'}"
                        )
                        continue
                if self._action == "download":
                    for ep in selected:
                        key = f"{mediainfo.media_source.value}:{mediainfo.media_id}:S{int(meta.begin_season)}:E{ep}"
                        ledger[key] = {"submitted_at": time.time(), "hash": str(result),
                                       "downloader": downloadchain.last_downloader,
                                       "quality": candidate, "title": title, "state": "submitted"}
                    self.save_data("pending_downloads_v2", ledger)
                    self.__audit(mediainfo, title, "submitted", torrent_hash=str(result), **details)
                # 存储历史记录
                history_item = {
                    "title": f"{mediainfo.title} {meta.season}",
                    "key": rss_key,
                    "rss_title": title,
                    "rss_description": torrentinfo.description or "",
                    "rss_size": torrentinfo.size or 0,
                    "selected_episodes": sorted(selected),
                    "download_mode": self._mode,
                    "type": mediainfo.type.value,
                    "year": mediainfo.year,
                    "poster": mediainfo.get_poster_image(),
                    "overview": mediainfo.overview,
                    "media_source": mediainfo.media_source.value,
                    "media_id": str(mediainfo.media_id),
                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                history_item["record_id"] = self.__history_record_id(history_item)
                history = [item for item in history if item.get("key") != rss_key]
                history.append(history_item)
                self.__persist_history(history)
            except (UncertainLibrary, UnsafeSelection) as err:
                self.__audit(mediainfo, title, "deferred", reason=str(err))
            except Exception as err:
                logger.error(f'处理RSS候选出错：{err} - {traceback.format_exc()}')
        # Persist pending removals as well as successful additions.
        self.save_data("pending_downloads_v2", ledger)
        # 保存历史记录
        self.__persist_history(history)
        # 缓存只清理一次
        self._clearflag = False

    def __log_and_notify_error(self, message):
        """
        记录错误日志，不发送系统通知
        """
        logger.error(message)
        # no notification by design

    def __validate_and_fix_config(self, config: dict = None) -> bool:
        """
        检查并修正配置值
        """
        config = config if config is not None else {}
        for key, default, minimum, maximum in [("query_retries", 3, 1, 5),
                ("absence_minutes", 10, 1, 120), ("pending_hours", 6, 1, 72)]:
            try:
                value = int(config.get(key, default))
                config[key] = max(minimum, min(maximum, value))
            except (TypeError, ValueError):
                config[key] = default
        if config.get("mode") not in ("missing", "upgrade"):
            config["mode"] = "missing"
        if config.get("upgrade_policy") not in ("resolution", "balanced", "source"):
            config["upgrade_policy"] = "balanced"
        size_range = config.get("size_range")
        if size_range and not self.__is_number_or_range(str(size_range)):
            self.__log_and_notify_error(f"RSS剧集优选出错，种子大小设置错误：{size_range}")
            config["size_range"] = None
            return False
        return True

    @staticmethod
    def __is_number_or_range(value):
        """
        检查字符串是否表示单个数字或数字范围（如'5', '5.5', '5-10' 或 '5.5-10.2'）
        """
        return bool(re.match(r"^\d+(\.\d+)?(-\d+(\.\d+)?)?$", value))
