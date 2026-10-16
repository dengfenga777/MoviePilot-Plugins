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
from app.schemas.types import MediaSource, SystemConfigKey, MediaType, MessageType

from .engine import (Library, UncertainLibrary, UnsafeSelection, decide, quality, rank, file_scope, VIDEO,
                     TARGETS, meets_target, site_score, is_special)
from .adapters import query_library, submit_selected, TORRENT_EXISTS

lock = Lock()

# 连续多少部剧的媒体库查询（重试用尽后）失败，就判定媒体库整体不可用并停止本轮查重
LIBRARY_BREAKER_THRESHOLD = 3
# “已确认无需下载”的 RSS 条目缓存时长（小时）；期间删集需等缓存过期才会补回
SETTLED_SKIP_HOURS = 6
# 这些理由只取决于媒体库已有版本与候选标题，不会因等待而改变
SETTLED_REASONS = ("已达到目标画质", "媒体库已有", "同档或降级，保留已有版本", "已有版本画质未知，保留已有版本",
                   "实际画质未知，保留已有版本", "候选未达到目标画质，等待更高画质", "已达每集最多下载次数",
                   "疑似标题虚标")
# 单部剧自身的数据问题：重试无用，也不代表媒体库整体不可用
LIBRARY_DATA_ERRORS = ("缺少可信季集编号", "缺少媒体ID")
# 认不出集号而直接下载整个种子后，同一 RSS 条目不再重复处理的时长（小时）
DIRECT_SKIP_HOURS = 7 * 24
# RSS 请求失败后的重试间隔（秒）
RSS_RETRY_SECONDS = 5
_SECRET_PARAM = re.compile(r"((?:passkey|apikey|api_key|token|authkey|rsskey|sign|uid|key)=)[^&\s]+", re.IGNORECASE)


def mask_url(url) -> str:
    """日志里隐藏 RSS 链接中的 passkey 等凭据。"""
    return _SECRET_PARAM.sub(r"\1***", str(url or ""))


class LibraryUnavailable(UncertainLibrary):
    """媒体库查询重试用尽仍失败（区别于“集数减少待复核”这类单剧状态）。"""


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
    # 认不出集号时整个种子交给 MoviePilot 原生下载，不做严格选集
    direct = False

    def download(self, content, download_dir, cookie=None, episodes=None, category=None,
                 label=None, downloader=None):
        if self.direct:
            return super().download(content, download_dir, cookie, episodes=episodes, category=category,
                                    label=label, downloader=downloader)
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
    plugin_desc = "追更优先：新集立即下载（多个版本同时出现时画质、码率优先，站点优先级最后），之后只升级一次到目标画质，每集下载次数可控；严格媒体库查重（支持多版本）、整包只选缺集，不发送系统通知。"
    # 插件图标
    plugin_icon = "rss.png"
    # 插件版本
    plugin_version = "1.5.5"
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
    _target_quality: str = "2160p"
    _max_downloads: int = 2
    _site_priority: str = ""
    # 详情页最多展示的历史卡片数（存储上限仍为 _history_limit）
    _history_page_limit: int = 100
    # 单轮运行内的决策缓冲与去重状态
    _audit_buffer: Optional[list] = None
    _audit_seen: Optional[dict] = None


    def init_plugin(self, config: dict = None):

        # 停止现有任务
        self.stop_service()

        # 配置
        migrate = False
        if config:
            migrate ="target_quality" not in config and (config.get("mode") or "missing") == "missing"
            if migrate:
                # 1.3.0 起默认“追更优先”；仅迁移仍停留在旧默认值“只补缺集”的配置
                config["mode"] = "chase"
                logger.info("RSS剧集优选：下载模式已从旧默认“只补缺集”切换为“追更优先”")
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
            self._target_quality = config.get("target_quality") or "2160p"
            self._max_downloads = int(config.get("max_downloads", 2))
            self._site_priority = str(config.get("site_priority") or "").strip()

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

        if config and migrate:
            self.__update_config()

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
                        ],
                    }
                ]
            }
        ] + [{
            'component': 'VCard',
            'content': [{'component': 'VCardText', 'content': [
                {'component': 'VTextField', 'props': {'model': 'library_servers', 'label': '查重媒体服务器名称（逗号分隔；留空使用全部启用服务）'}},
                {'component': 'VSelect', 'props': {'model': 'mode', 'label': '下载模式', 'items': [
                    {'title': '追更优先：新集立即下，之后只升级一次到目标画质', 'value': 'chase'},
                    {'title': '只补缺集', 'value': 'missing'},
                    {'title': '补缺集并允许逐档升级', 'value': 'upgrade'}]}},
                {'component': 'VSelect', 'props': {'model': 'target_quality', 'label': '目标画质（追更优先模式）', 'items': [
                    {'title': '2160p', 'value': '2160p'},
                    {'title': '2160p 且 HDR/杜比视界', 'value': '2160p_hdr'},
                    {'title': '1080p', 'value': '1080p'}]}},
                {'component': 'VTextField', 'props': {'model': 'max_downloads', 'label': '每集最多下载次数（追更优先模式，含首次，1–5）', 'type': 'number'}},
                {'component': 'VTextarea', 'props': {
                    'model': 'site_priority', 'label': '站点优先级（每行一个站点域名关键词，越靠上越优先；只在画质、体积都相同时才起作用）', 'rows': 3,
                    'placeholder': '例如：\nhdsky\nourbits\n留空则按上方RSS地址顺序，越靠上越优先',
                    'hint': '同一集同时出现在多个站时，先下优先级高的站；已有目标画质的资源仍优先，避免多下一次',
                    'persistent-hint': True}},
                {'component': 'VSelect', 'props': {'model': 'upgrade_policy', 'label': '升级判断（逐档升级模式）', 'items': [
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
            "library_servers": "", "mode": "chase", "upgrade_policy": "balanced",
            "target_quality": "2160p", "max_downloads": 2, "site_priority": "",
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
        # 数据按时间降序排序，只展示最近的部分，避免详情页一次渲染上千张卡片
        historys = sorted((item for item in historys if isinstance(item, dict)),
                          key=lambda x: str(x.get('time') or ''), reverse=True)[:self._history_page_limit]
        # 拼装页面
        contents = []
        for history in historys:
            title = history.get("title")
            record_id = history.get("record_id")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            episodes = history.get("selected_episodes") or []
            upgrades = history.get("upgrade_episodes") or []
            mode = {"missing": "补缺集", "upgrade": "补缺集+升级", "chase": "追更优先"}.get(history.get("download_mode"), "")
            episode_text = f"选集：{self.__format_episodes(episodes)}" if episodes else "选集：-"
            if upgrades:
                episode_text += f"（升级：{self.__format_episodes(upgrades)}）"
            if mode:
                episode_text += f" · {mode}"
            if history.get("site"):
                episode_text += f" · {history.get('site')}"
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
                                            'text': episode_text
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

    @staticmethod
    def __format_episodes(episodes) -> str:
        """[1,2,3,5] -> 'E01-E03, E05'"""
        values = sorted({int(ep) for ep in episodes if isinstance(ep, int) or str(ep).isdigit()})
        parts, start = [], None
        for index, value in enumerate(values):
            if start is None:
                start = value
            if index + 1 == len(values) or values[index + 1] != value + 1:
                parts.append(f"E{start:02d}" if start == value else f"E{start:02d}-E{value:02d}")
                start = None
        return ", ".join(parts)

    def __decision_page(self):
        records = self.get_data("decisions_v1") or []
        if not isinstance(records, list):
            return []
        records = [record for record in records if isinstance(record, dict)]
        labels = {"deferred": "暂缓", "skipped": "跳过", "planned": "待提交",
                  "failed": "失败", "submitted": "已提交", "reserved": "已记录提交计划"}
        # "跳过"多为媒体库已有的常规结果，默认不占用展示位
        important = [record for record in records if record.get("status") != "skipped"]
        skipped = len(records) - len(important)

        def line(record):
            selected = record.get('selected') or []
            reason = record.get('reason')
            if not reason and isinstance(record.get('reasons'), dict):
                counts = {}
                for value in record['reasons'].values():
                    counts[value] = counts.get(value, 0) + 1
                reason = "；".join(f"{value}×{count}" for value, count in counts.items())
            site = f"[{record.get('site')}] " if record.get('site') else ""
            text = (f"{record.get('time')} · {labels.get(record.get('status'), record.get('status'))} · "
                    f"{site}{record.get('title')} · {reason or '-'}")
            if selected:
                text += f" · 选择：{self.__format_episodes(selected)}"
            return {"component": "div", "text": text}

        content = [line(record) for record in reversed(important[-50:])]
        if skipped:
            content.append({"component": "div", "props": {"class": "text-medium-emphasis"},
                            "text": f"另有 {skipped} 条“跳过”（媒体库已有等）记录未展示；相同结果不会重复记录。"})
        if not content:
            content = [{"component": "div", "text": "暂无下载判断"}]
        return [{"component": "VCard", "content": [
            {"component": "VCardTitle", "text": "最近下载判断"},
            {"component": "VCardText", "content": content},
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
            "target_quality": self._target_quality,
            "max_downloads": self._max_downloads,
            "site_priority": self._site_priority,
        })

    def __audit(self, media, title, status, key=None, **details):
        """Bounded decision history contains no RSS URLs or service credentials.

        Within a run records are buffered and written once. A "skipped"/"deferred" outcome that is
        identical to the previous outcome for the same RSS item is not recorded again, so routine
        re-checks no longer push meaningful decisions out of the 500-entry window.
        """
        record = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
                  "media": f"{media.media_source.value}:{media.media_id}",
                  "title": title, "status": status, **details}
        if key:
            seen = self._audit_seen if self._audit_seen is not None else self.__load_audit_seen()
            fingerprint = self.__digest(json.dumps([status, details], ensure_ascii=False,
                                                   sort_keys=True, default=str))
            previous = seen.get(key) or {}
            seen[key] = {"fp": fingerprint, "t": time.time()}
            if self._audit_seen is None:
                self.save_data("decision_seen_v1", seen)
            if status in ("skipped", "deferred") and previous.get("fp") == fingerprint:
                logger.debug(f"RSS下载决策未变化，不重复记录：{title}")
                return
        logger.info(f"RSS下载决策：{json.dumps(record, ensure_ascii=False, default=str)}")
        if self._audit_buffer is not None:
            self._audit_buffer.append(record)
            return
        self.__write_audit([record])

    def __write_audit(self, new_records):
        records = self.get_data("decisions_v1") or []
        if not isinstance(records, list):
            raise ValueError("下载决策记录格式异常")
        self.save_data("decisions_v1", (records + list(new_records))[-500:])

    def __load_audit_seen(self) -> dict:
        seen = self.get_data("decision_seen_v1")
        if not isinstance(seen, dict):
            return {}
        cutoff = time.time() - 7 * 86400
        return {k: v for k, v in seen.items()
                if isinstance(v, dict) and float(v.get("t") or 0) >= cutoff}

    def __flush_audit(self):
        buffer, seen = self._audit_buffer, self._audit_seen
        self._audit_buffer, self._audit_seen = None, None
        if buffer:
            self.__write_audit(buffer)
        if seen is not None:
            self.save_data("decision_seen_v1", seen)

    def __config_fingerprint(self, filter_groups) -> str:
        """Cached negative results are only valid for the settings that produced them."""
        return self.__digest(json.dumps([self._include, self._exclude, self._filter, filter_groups,
                                         self._size_range, self._skip_unknown_episodes,
                                         self._smart_quality, self._action, self._mode,
                                         self._target_quality, self._upgrade_policy, self._max_downloads,
                                         self._library_servers], default=str))

    def __load_skip_cache(self, fingerprint) -> dict:
        cache = self.get_data("rss_skip_cache_v1")
        if not isinstance(cache, dict) or cache.get("cfg") != fingerprint:
            return {"cfg": fingerprint, "items": {}}
        now = time.time()
        items = cache.get("items") if isinstance(cache.get("items"), dict) else {}
        cache["items"] = {k: v for k, v in items.items()
                          if isinstance(v, dict) and float(v.get("until") or 0) > now}
        return cache

    @staticmethod
    def __remember_skip(cache, rss_key, reason, hours):
        cache["items"][rss_key] = {"until": time.time() + hours * 3600, "reason": reason}

    def __episode_key(self, mediainfo, season, episode) -> str:
        return f"{mediainfo.media_source.value}:{mediainfo.media_id}:S{int(season)}:E{int(episode)}"

    def __load_upgrade_attempts(self) -> dict:
        attempts = self.get_data("upgrade_attempts_v1")
        if not isinstance(attempts, dict):
            return {}
        cutoff = time.time() - 180 * 86400
        result = {}
        for key, values in attempts.items():
            if isinstance(values, list):
                kept = [v for v in values if isinstance(v, dict) and float(v.get("t") or 0) >= cutoff]
                if kept:
                    result[key] = kept[-10:]
        return result

    def __prune_ledger(self, ledger, services, cache) -> None:
        """Drop protection entries that can no longer matter.

        Entries normally disappear when their episode shows up in RSS again. Shows that stop
        appearing would otherwise keep their entries forever. Only entries older than 7 days are
        considered, and only removed when the downloader confirms the task is gone or finished.
        Entries whose downloader no longer exists are dropped after 30 days.
        """
        now = time.time()
        for key, record in list(ledger.items()):
            age = now - float(record.get("submitted_at") or 0)
            if age < 7 * 86400:
                continue
            name, torrent_hash = record.get("downloader"), record.get("hash")
            if not name or not torrent_hash or name not in services:
                if age >= 30 * 86400:
                    del ledger[key]
                continue
            identity = (name, torrent_hash)
            if identity not in cache:
                try:
                    tasks, error = services[name].instance.get_torrents(ids=torrent_hash)
                except Exception:
                    continue
                if error or tasks is None:
                    continue
                cache[identity] = tasks
            tasks = cache[identity]
            try:
                finished = all(float(task.get("progress", -1)) >= 1 for task in tasks)
            except (TypeError, ValueError):
                continue
            if not tasks or finished:
                del ledger[key]

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
                if any(text in str(err) for text in LIBRARY_DATA_ERRORS):
                    logger.warning(f"{media.title_year} 媒体库数据不完整，本轮暂缓：{err}")
                    raise
                logger.warning(f"{media.title_year} 媒体库查询 {attempt+1}/{self._query_retries}：{err}")
                if attempt + 1 == self._query_retries:
                    raise LibraryUnavailable(str(err)) from err
                time.sleep(1)
        if getattr(library, "unplaced", 0):
            logger.warning(f"{media.title_year} 媒体库有 {library.unplaced} 个条目无法确定季集，已忽略")
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
        """读取单个 RSS 源；请求或解析失败重试一次，任何异常只影响当前源。"""
        for attempt in range(2):
            try:
                results = RssHelper().parse(url, proxy=self._proxy, timeout=20)
            except Exception as err:
                if attempt == 0:
                    time.sleep(RSS_RETRY_SECONDS)
                    continue
                logger.error(
                    f"刷新 RSS 源发生未预期异常，已跳过：{mask_url(url)}，"
                    f"错误：{mask_url(err)} - {mask_url(traceback.format_exc())}"
                )
                return []
            if results is False and attempt == 0:
                time.sleep(RSS_RETRY_SECONDS)
                continue
            break

        if results is None:
            logger.warning(f"RSS 链接可能已经过期：{mask_url(url)}")
            return []
        if results is False:
            logger.error(f"RSS 请求或解析失败：{mask_url(url)}")
            return []
        if not isinstance(results, list):
            logger.error(f"RSS 返回了不支持的数据类型：{mask_url(url)}，类型：{type(results).__name__}")
            return []
        if not results:
            logger.info(f"RSS 没有可处理的数据：{mask_url(url)}")
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

    def __torrent_scope(self, context, downloader):
        from torrentool.api import Torrent
        content, _, _ = downloader.download_torrent(context.torrent_info)
        if not isinstance(content, bytes):
            err = UnsafeSelection("无法取得种子文件，暂缓")
            err.fetch_failed = True
            raise err
        torrent = Torrent.from_string(content)
        season_episodes = {}
        for file in torrent.files:
            if Path(file.name).suffix.lower() not in VIDEO or is_special(file.name):
                continue
            season, episodes = file_scope(file.name, None)
            if season is None or not episodes:
                raise UnsafeSelection("种子视频文件没有明确季集编号")
            season_episodes.setdefault(season, set()).update(episodes)
        if len(season_episodes) != 1:
            raise UnsafeSelection("种子不是明确单季资源")
        season, episodes = next(iter(season_episodes.items()))
        return content, season, sorted(episodes)

    def __task_downloading(self, record, services, cache) -> bool:
        """True while the submitted task still has data to fetch; raises when unknown."""
        identity = (record.get("downloader"), record.get("hash"))
        if not all(identity) or identity[0] not in services:
            raise UncertainLibrary("已提交记录对应下载器不可用")
        if identity not in cache:
            tasks, error = services[identity[0]].instance.get_torrents(ids=identity[1])
            if error or tasks is None:
                raise UncertainLibrary("无法查询已提交任务状态")
            cache[identity] = tasks
        progress = [float(task.get("progress", -1)) for task in cache[identity]]
        if any(not 0 <= value <= 1 for value in progress):
            raise UncertainLibrary("下载任务进度无效")
        return any(value < 1 for value in progress)

    def __released_by_import(self, record, library, season, episode, services, cache) -> bool:
        """入库即解除保护：库里已有不低于该次提交分辨率的版本，且任务不在下载中。

        这样升级版本不必等满“已提交任务重复保护”时长；查询失败时保持保护。
        """
        versions = library.episodes.get((season, episode), [])
        if not versions:
            return False
        expected = record.get("quality")
        if isinstance(expected, (list, tuple)) and expected and isinstance(expected[0], int) and expected[0] > 0:
            reached = False
            for item in versions:
                actual = quality(str(item.get("Path") or item.get("Name") or ""), item)
                if actual is not None and actual[0] >= expected[0]:
                    reached = True
                    break
            if not reached:
                return False
        try:
            return not self.__task_downloading(record, services, cache)
        except (UncertainLibrary, TypeError, ValueError):
            return False

    def __direct_download(self, context, rss_key, cause, downloadchain, download_services, skip_cache,
                          history, site_label):
        """认不出集号时不做严格选集，整个种子交给 MoviePilot 原生下载；同一条目只下一次。"""
        mediainfo, torrentinfo = context.media_info, context.torrent_info
        title = torrentinfo.title
        downloader_name = getattr(torrentinfo, "site_downloader", None)
        if not downloader_name:
            defaults = [name for name, config in DownloaderHelper().get_configs().items() if config.default]
            downloader_name = defaults[0] if len(defaults) == 1 else None
        if not downloader_name or downloader_name not in download_services:
            self.__audit(mediainfo, title, "deferred", key=rss_key, reason="下载器不可用，直接下载暂缓")
            return history
        downloadchain.direct = True
        try:
            result = downloadchain.download_single(context=context, save_path=self._save_path or None,
                                                   downloader=downloader_name, username="RSS剧集优选")
        finally:
            downloadchain.direct = False
        if not result:
            self.__audit(mediainfo, title, "failed", key=rss_key, site=site_label,
                         reason=getattr(downloadchain, "last_error", None) or "下载链未接受资源")
            return history
        self.__audit(mediainfo, title, "submitted", key=rss_key, torrent_hash=str(result), site=site_label,
                     reasons={"direct": f"认不出集号（{cause}），直接下载整个种子"})
        self.__remember_skip(skip_cache, rss_key, "已直接下载", DIRECT_SKIP_HOURS)
        item = {"title": f"{mediainfo.title} {context.meta_info.season}", "key": rss_key, "rss_title": title,
                "rss_description": torrentinfo.description or "", "rss_size": torrentinfo.size or 0,
                "selected_episodes": [], "upgrade_episodes": [], "site": site_label,
                "download_mode": "direct", "action": self._action, "type": mediainfo.type.value,
                "year": mediainfo.year, "poster": mediainfo.get_poster_image(), "overview": mediainfo.overview,
                "media_source": mediainfo.media_source.value, "media_id": str(mediainfo.media_id),
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        item["record_id"] = self.__history_record_id(item)
        history = [record for record in history if record.get("key") != rss_key] + [item]
        self.__persist_history(history)
        return history

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
        self._audit_buffer = []
        self._audit_seen = self.__load_audit_seen()
        try:
            self.__run_check()
        finally:
            self.__flush_audit()

    def __compile_patterns(self):
        """无效的包含/排除正则会让整轮停止，而不是放宽匹配。"""
        try:
            include = re.compile(self._include, re.IGNORECASE) if self._include else None
            exclude = re.compile(self._exclude, re.IGNORECASE) if self._exclude else None
        except re.error as err:
            logger.error(f"RSS剧集优选：包含/排除正则无效，本轮停止：{err}")
            return None
        return include, exclude

    def __run_check(self):
        patterns = self.__compile_patterns()
        if patterns is None:
            return
        include_re, exclude_re = patterns
        # 读取历史记录
        if self._clearflag:
            history = []
            self.save_data("rss_skip_cache_v1", {})
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
        skip_cache = self.__load_skip_cache(self.__config_fingerprint(filter_groups))
        cache_hits = 0
        candidates = []
        feed_results = []
        urls = [url.strip() for url in self._address.splitlines() if url.strip()]
        for feed_index, url in enumerate(urls):
            logger.info(f"剧集优选：读取第 {feed_index + 1} 个 RSS 源")
            feed_results.extend((feed_index, url, result) for result in self.__fetch_rss(url))
        site_keys = [line.strip() for line in self._site_priority.splitlines() if line.strip()]
        chase_target = TARGETS.get(self._target_quality, TARGETS["2160p"]) if self._mode == "chase" else None
        site_labels = {}
        seen_feed_keys = set()
        for feed_index, feed_url, result in feed_results:
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
                # 近期已确认不适用（非剧集、无法识别、不匹配规则组等）的条目不再重复识别
                if rss_key in skip_cache["items"]:
                    cache_hits += 1
                    continue
                # 检查规则
                if include_re and not include_re.search(f"{title} {description}"):
                    logger.info(f"{title} - {description} 不符合包含规则")
                    continue
                if exclude_re and exclude_re.search(f"{title} {description}"):
                    logger.info(f"{title} - {description} 不符合排除规则")
                    continue
                if self._size_range:
                    try:
                        size_value = float(size)
                    except (TypeError, ValueError):
                        logger.info(f"{title} - RSS 未提供有效种子大小，无法判断大小范围，跳过")
                        continue
                    sizes = [float(_size) * 1024 ** 3 for _size in self._size_range.split("-")]
                    if len(sizes) == 1 and size_value < sizes[0]:
                        logger.info(f"{title} - 种子大小不符合条件")
                        continue
                    elif len(sizes) > 1 and not sizes[0] <= size_value <= sizes[1]:
                        logger.info(f"{title} - 种子大小不在指定范围")
                        continue
                # 识别媒体信息
                # RSS 简介可能包含播出总集数、整季范围等，不能用于下载覆盖范围。
                # 标题没有明确集数时保持未知，查库阶段拆包读取种子文件名。
                meta = MetaInfo(title=title)
                if not meta.name:
                    logger.warning(f"{title} 未识别到有效数据")
                    self.__remember_skip(skip_cache, rss_key, "未识别到有效数据", 24)
                    continue
                mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
                if not mediainfo:
                    logger.warning(f'未识别到媒体信息，标题：{title}')
                    # 识别失败可能是临时网络问题，只短暂缓存
                    self.__remember_skip(skip_cache, rss_key, "未识别到媒体信息", 2)
                    continue
                if mediainfo.type != MediaType.TV:
                    logger.info(f"{title} - RSS剧集优选仅处理电视剧，跳过{mediainfo.type.value}")
                    self.__remember_skip(skip_cache, rss_key, "非电视剧", 24)
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
                        self.__remember_skip(skip_cache, rss_key, "未匹配到适用订阅规则组", 6)
                        continue
                    logger.debug(
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
                        self.__remember_skip(skip_cache, rss_key, "不匹配订阅规则组", 6)
                        continue
                    torrentinfo = matched_torrents[0]
                    logger.info(
                        f"{title} - 已命中订阅规则组：{', '.join(active_group_names)}，"
                        f"优先级：{getattr(torrentinfo, 'pri_order', '-')}"
                    )
                context = Context(meta_info=meta, media_info=mediainfo, torrent_info=torrentinfo)
                smart = self._smart_quality and self._action == "download" and mediainfo.type == MediaType.TV
                episode_keys = self.__episode_keys(meta, mediainfo) if smart else []
                # 排序：已达目标画质（追更优先）> 画质细节 > 体积（码率）> MP 规则优先级 > 站点优先级
                site, site_label = site_score([feed_url, link, enclosure], site_keys, feed_index)
                site_labels[rss_key] = site_label
                at_target = int(chase_target is not None and meets_target(quality(title), chase_target) is True)
                rule_priority, *details = rank(title, getattr(torrentinfo, "pri_order", 0))
                try:
                    size = float(getattr(torrentinfo, "size", 0) or 0)
                except (TypeError, ValueError):
                    size = 0.0
                order = (at_target, *details, size, rule_priority, site)
                candidates.append((context, rss_key, episode_keys, order, smart))
            except Exception as err:
                logger.error(f'解析RSS候选出错：{err} - {traceback.format_exc()}')
        self.save_data("rss_skip_cache_v1", skip_cache)
        if cache_hits:
            logger.info(f"剧集优选：{cache_hits} 条 RSS 近期已确认不适用或无需下载，本轮未重复识别")
        # Size only orders equal-grade variants; it cannot itself cause an upgrade or a second download.
        if self._smart_quality and self._action == "download":
            sorted_tv = iter(sorted((item for item in candidates if item[4]),
                                    key=lambda item: item[3], reverse=True))
            candidates = [next(sorted_tv) if item[4] else item for item in candidates]
        ledger = self.get_data("pending_downloads_v2") or {}
        if not isinstance(ledger, dict):
            raise ValueError("待完成下载记录格式异常，停止本轮")
        if any(not isinstance(value, dict) for value in ledger.values()):
            raise ValueError("待完成下载记录条目异常")
        upgrade_attempts = self.__load_upgrade_attempts()
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
        # 熔断：连续多部剧查询失败说明媒体库整体不可用，剩余候选直接暂缓，不再逐部重试
        library_failures = 0
        library_down = None
        library_ok = False
        for context, rss_key, episode_keys, _order, smart in candidates:
            meta, mediainfo, torrentinfo = context.meta_info, context.media_info, context.torrent_info
            title = torrentinfo.title
            try:
                selected = set()
                try:
                    identity = (mediainfo.media_source.value, str(mediainfo.media_id))
                    if identity not in libraries:
                        if library_down:
                            raise UncertainLibrary(f"媒体库连续查询失败，本轮暂停查重：{library_down}")
                        libraries[identity] = self.__library(mediainfo, services)
                        library_failures = 0
                        library_ok = True
                    library = libraries[identity]
                except LibraryUnavailable as err:
                    libraries[identity] = None
                    library_failures += 1
                    if library_failures >= LIBRARY_BREAKER_THRESHOLD and not library_down:
                        library_down = str(err)
                        logger.error(f"剧集优选：连续 {library_failures} 部剧媒体库查询失败，本轮剩余候选全部暂缓：{err}")
                    self.__audit(mediainfo, title, "deferred", key=rss_key, reason=str(err))
                    continue
                except UncertainLibrary as err:
                    libraries[identity] = None
                    self.__audit(mediainfo, title, "deferred", key=rss_key, reason=str(err))
                    continue
                if library is None:
                    continue
                seasons = list(getattr(meta, "season_list", None) or [])
                requested = sorted(set(meta.episode_list or []))
                torrent_content = None
                chosen_season = meta.begin_season
                # 标题没有集数（整季包/更新包）：拆包按文件名识别，再和媒体库对比只下缺的集；
                # 实在认不出就直接下载整个种子——宁愿多下，也不错过
                if not requested:
                    try:
                        if len(seasons) > 1:
                            raise UnsafeSelection("标题包含多季")
                        torrent_content, chosen_season, requested = self.__torrent_scope(context, downloadchain)
                        meta.begin_season = chosen_season
                        meta.end_season = None
                    except UnsafeSelection as err:
                        if getattr(err, "fetch_failed", False):
                            self.__audit(mediainfo, title, "deferred", key=rss_key, reason=str(err))
                        else:
                            history = self.__direct_download(context, rss_key, str(err), downloadchain,
                                                             download_services, skip_cache, history,
                                                             site_labels.get(rss_key, ""))
                        continue
                if chosen_season is None or len(seasons) > 1 or not requested:
                    self.__audit(mediainfo, title, "deferred", key=rss_key, reason="候选没有可信的单季集数")
                    continue
                season = int(meta.begin_season)
                for episode in requested:
                    pending_key = self.__episode_key(mediainfo, season, episode)
                    if pending_key in ledger and self.__released_by_import(
                            ledger[pending_key], library, season, episode, download_services, pending_cache):
                        del ledger[pending_key]
                        continue
                    if (season, episode) in library.episodes and pending_key in ledger:
                        record = ledger[pending_key]
                        if (time.time() - float(record.get("submitted_at", 0)) >= self._pending_hours * 3600
                                and not self.__pending_active(record, download_services, pending_cache)):
                            del ledger[pending_key]
                candidate = quality(title)
                if self._mode == "chase":
                    target = TARGETS.get(self._target_quality, TARGETS["2160p"])
                    axes = (0, 1) if target[1] else (0,)
                else:
                    target = None
                    axes = {"resolution": (0,), "balanced": (0, 1), "source": (0, 1, 2)}[self._upgrade_policy]
                plan = decide(requested, season, library, candidate,
                              lambda item: quality(str(item.get("Path") or item.get("Name") or ""), item),
                              mode=self._mode, upgrade_axes=axes, target=target)
                # Any in-flight download protects its episodes across RSS sources, even if
                # a better candidate arrives; reconsider upgrades after transfer or expiry.
                blocked = []
                for ep in list(plan.selected):
                    pending_key = self.__episode_key(mediainfo, season, ep)
                    if pending_key in ledger:
                        if self.__pending_active(ledger[pending_key], download_services, pending_cache):
                            plan.selected.remove(ep)
                            plan.reasons[str(ep)] = "已有下载中或待入库的提交任务"
                            blocked.append(ep)
                        else:
                            del ledger[pending_key]
                # 某集已经从这个 RSS 资源下载入库（无论当初是补缺还是升级），库内却仍判定它可升级，
                # 说明标题声明的画质与实际不符。不再用同一资源“升级”，避免下载→入库→再升级的循环。
                # 缺集不受影响：删集后仍可从同一资源补回。
                for ep in sorted(plan.upgrades & plan.selected):
                    tried = upgrade_attempts.get(self.__episode_key(mediainfo, season, ep), [])
                    if any(item.get("rss") == rss_key for item in tried):
                        plan.selected.discard(ep)
                        plan.reasons[str(ep)] = "已从该资源下载过但库内画质仍低于其标题，疑似标题虚标，不再重复升级"
                # 追更优先：每集下载次数（含首次）达到上限后不再升级；缺集补回不受限制
                if self._mode == "chase":
                    for ep in sorted(plan.upgrades & plan.selected):
                        count = len(upgrade_attempts.get(self.__episode_key(mediainfo, season, ep), []))
                        if count >= self._max_downloads:
                            plan.selected.discard(ep)
                            plan.reasons[str(ep)] = f"已达每集最多下载次数（{count}/{self._max_downloads}）"
                selected = plan.selected

                def plan_details():
                    return {"site": site_labels.get(rss_key, ""), "season": season, "requested": requested,
                            "existing": library.coverage().get(str(season), []),
                            "missing": sorted(plan.missing), "upgrades": sorted(plan.upgrades & selected),
                            "selected": sorted(selected), "pending": sorted(blocked),
                            "candidate_quality": candidate, "reasons": plan.reasons}
                details = plan_details()
                self.__audit(mediainfo, title, "planned" if selected else "skipped", key=rss_key, **details)
                if not selected:
                    # 结论只取决于库内版本与候选标题时，缓存一段时间，免得每轮重复识别和查库
                    if not blocked and plan.reasons and all(
                            any(reason in str(text) for reason in SETTLED_REASONS)
                            for text in plan.reasons.values()):
                        self.__remember_skip(skip_cache, rss_key, "无需下载", SETTLED_SKIP_HOURS)
                    continue
                # 下载或订阅
                if self._action == "download":
                    # Every download goes through the strict qBittorrent adapter.
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
                    # 同一种子（其他站点的不同 RSS 条目）也适用升级防循环
                    repeated = {ep for ep in plan.upgrades & selected
                                if any(item.get("hash") == intent_hash for item in
                                       upgrade_attempts.get(self.__episode_key(mediainfo, season, ep), []))}
                    if repeated:
                        for ep in repeated:
                            selected.discard(ep)
                            plan.reasons[str(ep)] = "已从该种子下载过但库内画质仍低于其标题，疑似标题虚标，不再重复升级"
                        details = plan_details()
                        if not selected:
                            self.__audit(mediainfo, title, "skipped", key=rss_key, **details)
                            continue
                    downloadchain.selection_season = season
                    downloadchain.selection_upgrade = {"episodes": set(plan.upgrades) & selected,
                                                       "quality": candidate, "axes": axes}
                    downloader_name = getattr(torrentinfo, "site_downloader", None)
                    if not downloader_name:
                        defaults = [name for name, config in DownloaderHelper().get_configs().items() if config.default]
                        if len(defaults) != 1:
                            raise UnsafeSelection("默认下载器不唯一或未设置")
                        downloader_name = defaults[0]
                    if (downloader_name not in download_services
                            or str(download_services[downloader_name].type).lower() != "qbittorrent"):
                        raise UnsafeSelection("下载器不可用或不支持严格选集")
                    for ep in selected:
                        ledger[self.__episode_key(mediainfo, season, ep)] = {
                            "submitted_at": time.time(), "hash": intent_hash,
                            "downloader": downloader_name, "quality": candidate,
                            "title": title, "state": "reserved"}
                    self.save_data("pending_downloads_v2", ledger)
                    self.__audit(mediainfo, title, "reserved", key=rss_key, torrent_hash=intent_hash, **details)
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
                                ledger.pop(self.__episode_key(mediainfo, season, ep), None)
                            self.save_data("pending_downloads_v2", ledger)
                        if downloadchain.last_error == TORRENT_EXISTS:
                            # 下载器里已有同一个种子（别的订阅或上次提交），等它入库即可，不再反复提交
                            self.__remember_skip(skip_cache, rss_key, "下载器已有该种子", DIRECT_SKIP_HOURS)
                            self.__audit(mediainfo, title, "skipped", key=rss_key,
                                         reason="下载器已有该种子", **details)
                            continue
                        self.__audit(mediainfo, title, "failed", key=rss_key,
                                     reason=downloadchain.last_error or "下载链未接受资源", **details)
                        continue
                    # 记录每集的下载来源，供上面的升级防循环判断
                    for ep in selected:
                        attempt_key = self.__episode_key(mediainfo, season, ep)
                        upgrade_attempts.setdefault(attempt_key, []).append(
                            {"rss": rss_key, "hash": intent_hash, "t": time.time(), "quality": candidate,
                             "kind": "upgrade" if ep in plan.upgrades else "missing"})
                        upgrade_attempts[attempt_key] = upgrade_attempts[attempt_key][-10:]
                    self.save_data("upgrade_attempts_v1", upgrade_attempts)
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
                        ledger[self.__episode_key(mediainfo, season, ep)] = {
                            "submitted_at": time.time(), "hash": str(result),
                            "downloader": downloadchain.last_downloader,
                            "quality": candidate, "title": title, "state": "submitted"}
                    self.save_data("pending_downloads_v2", ledger)
                    self.__audit(mediainfo, title, "submitted", key=rss_key, torrent_hash=str(result), **details)
                # 存储历史记录
                history_item = {
                    "title": f"{mediainfo.title} {meta.season}",
                    "key": rss_key,
                    "rss_title": title,
                    "rss_description": torrentinfo.description or "",
                    "rss_size": torrentinfo.size or 0,
                    "selected_episodes": sorted(selected),
                    "upgrade_episodes": sorted(set(plan.upgrades) & selected),
                    "site": site_labels.get(rss_key, ""),
                    "download_mode": self._mode,
                    "action": self._action,
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
                self.__audit(mediainfo, title, "deferred", key=rss_key, reason=str(err))
            except Exception as err:
                logger.error(f'处理RSS候选出错：{err} - {traceback.format_exc()}')
        self.save_data("rss_skip_cache_v1", skip_cache)
        self.__report_library_health(library_down, library_ok)
        # 清理长期不再出现的待完成记录
        self.__prune_ledger(ledger, download_services, pending_cache)
        # Persist pending removals as well as successful additions.
        self.save_data("pending_downloads_v2", ledger)
        # 保存历史记录
        self.__persist_history(history)
        # 缓存只清理一次
        self._clearflag = False

    def __report_library_health(self, library_down, library_ok):
        """媒体库不可用/恢复时各发一条通知；持续故障不重复提醒。

        插件平时不发系统通知，这是唯一例外：查重失败意味着所有下载都会停下。
        """
        state = self.get_data("library_health_v1")
        was_down = isinstance(state, dict) and bool(state.get("down"))
        if library_down and not was_down:
            self.save_data("library_health_v1", {"down": True, "since": time.time(), "error": library_down})
            self.post_message(mtype=MessageType.Plugin, title="【RSS剧集优选】媒体库查询失败",
                              text=f"连续多部剧查询媒体库失败，查重已暂停，所有下载暂缓。\n错误：{library_down}")
        elif was_down and not library_down and library_ok:
            self.save_data("library_health_v1", {"down": False, "since": time.time()})
            self.post_message(mtype=MessageType.Plugin, title="【RSS剧集优选】媒体库查询已恢复",
                              text="媒体库查询恢复正常，剧集优选已继续工作。")

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
                ("absence_minutes", 10, 1, 120), ("pending_hours", 6, 1, 72),
                ("max_downloads", 2, 1, 5)]:
            try:
                value = int(config.get(key, default))
                config[key] = max(minimum, min(maximum, value))
            except (TypeError, ValueError):
                config[key] = default
        if config.get("mode") not in ("missing", "upgrade", "chase"):
            config["mode"] = "missing"
        if config.get("upgrade_policy") not in ("resolution", "balanced", "source"):
            config["upgrade_policy"] = "balanced"
        if config.get("target_quality") not in TARGETS:
            config["target_quality"] = "2160p"
        for key in ("include", "exclude"):
            pattern = config.get(key)
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as err:
                    # 保留原值让用户修改；运行时会因正则无效而整轮停止，而不是放宽匹配
                    self.__log_and_notify_error(f"RSS剧集优选：{'包含' if key == 'include' else '排除'}正则无效：{err}")
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
