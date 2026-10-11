import datetime
import hashlib
import re
import traceback
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
from app.sdk.services import RuleHelper
from app.plugins import _PluginBase
from app.schemas import ExistMediaInfo
from app.schemas.types import MediaSource, SystemConfigKey, MediaType

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
    plugin_desc = "跨 RSS 源优选剧集，同集去重、拦截降级，允许质量升级，不发送系统通知。"
    # 插件图标
    plugin_icon = "rss.png"
    # 插件版本
    plugin_version = "1.0.0"
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
            self._action = config.get("action")
            self._save_path = str(config.get("save_path") or "").strip()
            self._size_range = config.get("size_range")

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
                                        'label': '剧集优选：同集去重，允许质量升级',
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
                                        'label': '剧集优选时跳过无法识别集数的资源',
                                    },
                                }],
                            },
                        ],
                    }
                ]
            }
        ], {
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
            "size_range": ""
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]
        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
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

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]

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
            "size_range": self._size_range
        })

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

    def __seed_quality(self, history, mediainfo, filter_groups, ledger) -> None:
        """Migrate retained successful downloads without fetching RSS or downloading again."""
        for item in history:
            if (not isinstance(item, dict)
                    or str(item.get("media_id")) != str(mediainfo.media_id)
                    or item.get("media_source") != mediainfo.media_source.value
                    or item.get("type") != MediaType.TV.value):
                continue
            title = item.get("rss_title")
            if not title:
                continue
            try:
                old_meta = MetaInfo(title=title, subtitle=item.get("rss_description") or "")
                keys = self.__episode_keys(old_meta, mediainfo)
                if not keys:
                    continue
                old_torrent = TorrentInfo(title=title, description=item.get("rss_description") or "",
                                          size=item.get("rss_size") or 0)
                if self._filter:
                    matched = self.chain.filter_torrents(
                        rule_groups=filter_groups, torrent_list=[old_torrent], mediainfo=mediainfo)
                    # An old record rejected by today's rules cannot safely receive a rule score.
                    if not matched:
                        continue
                    old_torrent = matched[0]
                rank = self.__quality_rank(title, getattr(old_torrent, "pri_order", 0))
                for key in keys:
                    previous = ledger.get(key)
                    if not previous or tuple(previous["rank"]) < rank:
                        ledger[key] = {"rank": list(rank), "title": title,
                                       "time": item.get("time"), "migrated": True}
            except Exception as err:
                logger.warning(f"历史质量记录迁移失败，保留原历史：{title}，{err}")

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
        processed_keys = {
            str(item.get("key"))
            for item in history
            if isinstance(item, dict) and item.get("key")
        }
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
                if not title or not rss_key or rss_key in processed_keys or title in processed_keys:
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
                meta = MetaInfo(title=title, subtitle=description)
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
        ledger = (self.get_data("episode_quality_v1") or {}) if self._smart_quality and self._action == "download" and not self._clearflag else {}
        if not isinstance(ledger, dict):
            raise ValueError("剧集质量记录格式异常；停止本轮，避免重复下载")
        if self._clearflag and self._smart_quality and self._action == "download":
            self.save_data("episode_quality_v1", {})
        seeded_media = set()
        logger.info(f"剧集优选：{len(feed_results)} 条RSS记录，{len(candidates)} 条有效候选")
        for context, rss_key, episode_keys, rank, smart in candidates:
            meta, mediainfo, torrentinfo = context.meta_info, context.media_info, context.torrent_info
            title = torrentinfo.title
            try:
                if rss_key in processed_keys:
                    continue
                if smart and episode_keys:
                    identity = (mediainfo.media_source.value, str(mediainfo.media_id), mediainfo.category)
                    if identity not in seeded_media:
                        self.__seed_quality(history, mediainfo, filter_groups, ledger)
                        self.save_data("episode_quality_v1", ledger)
                        seeded_media.add(identity)
                    covered = [key for key in episode_keys if key in ledger and tuple(ledger[key]["rank"]) >= rank]
                    if len(covered) == len(episode_keys):
                        logger.info(f"{title} - 剧集优选：所有集数已有同档或更好下载，拦截重复/降级")
                        continue
                    if covered:
                        logger.info(f"{title} - 剧集优选：更新包含 {len(episode_keys)-len(covered)} 集新增或升级，允许下载")
                # 媒体库已存在的剧集
                exist_info: Optional[ExistMediaInfo] = self.chain.media_exists(mediainfo=mediainfo)
                if mediainfo.type == MediaType.TV:
                    if exist_info:
                        exist_season = exist_info.seasons
                        if exist_season:
                            exist_episodes = exist_season.get(meta.begin_season)
                            if exist_episodes and set(meta.episode_list).issubset(set(exist_episodes)):
                                logger.info(f'{mediainfo.title_year} {meta.season_episode} 己存在')
                                continue
                elif exist_info:
                    # 电影已存在
                    logger.info(f'{mediainfo.title_year} 己存在')
                    continue
                # 下载或订阅
                if self._action == "download":
                    # 添加下载
                    result = downloadchain.download_single(
                        context=Context(
                            meta_info=meta,
                            media_info=mediainfo,
                            torrent_info=torrentinfo,
                        ),
                        save_path=self._save_path or None,
                        username="RSS剧集优选"
                    )
                    if not result:
                        logger.error(f'{title} 下载失败')
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
                if smart and episode_keys:
                    for key in episode_keys:
                        previous = ledger.get(key)
                        if not previous or tuple(previous["rank"]) < rank:
                            ledger[key] = {"rank": list(rank), "title": title,
                                           "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    self.save_data("episode_quality_v1", ledger)
                # 存储历史记录
                history_item = {
                    "title": f"{mediainfo.title} {meta.season}",
                    "key": rss_key,
                    "rss_title": title,
                    "rss_description": torrentinfo.description or "",
                    "rss_size": torrentinfo.size or 0,
                    "type": mediainfo.type.value,
                    "year": mediainfo.year,
                    "poster": mediainfo.get_poster_image(),
                    "overview": mediainfo.overview,
                    "media_source": mediainfo.media_source.value,
                    "media_id": str(mediainfo.media_id),
                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                history_item["record_id"] = self.__history_record_id(history_item)
                history.append(history_item)
                processed_keys.add(rss_key)
                self.__persist_history(history)
            except Exception as err:
                logger.error(f'处理RSS候选出错：{err} - {traceback.format_exc()}')
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
