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
from app.sdk.services import RuleHelper, MediaServerHelper
from app.plugins import _PluginBase
from app.schemas import ExistMediaInfo
from app.schemas.types import MediaSource, SystemConfigKey, MediaType

lock = Lock()

# 每部电影最多下载几次：第一次 + 出现更高分辨率时升级一次
MAX_DOWNLOADS_PER_MOVIE = 2
# 已下载电影记录的保留条数
DOWNLOAD_RECORD_LIMIT = 3000
_SECRET_PARAM = re.compile(r"((?:passkey|apikey|api_key|token|authkey|rsskey|sign|uid|key|downhash)=)[^&\s]+", re.IGNORECASE)


def mask_url(url) -> str:
    """日志里隐藏 RSS 链接中的密钥。"""
    return _SECRET_PARAM.sub(r"\1***", str(url or ""))


def resolution(title) -> int:
    """从标题取分辨率，认不出返回 0。"""
    text = str(title or "")
    if re.search(r"2160[pi]|\b4k\b|\buhd\b", text, re.IGNORECASE):
        return 2160
    match = re.search(r"(?<!\d)(1440|1080|720|576|480)[pi]\b", text, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def source_resolution(source: dict) -> int:
    """媒体库单个版本的分辨率：优先视频流宽高，strm 等没有流信息时看文件名，认不出返回 0。"""
    for stream in source.get("MediaStreams") or []:
        if stream.get("Type") == "Video" and (stream.get("Width") or stream.get("Height")):
            width, height = int(stream.get("Width") or 0), int(stream.get("Height") or 0)
            if width >= 3200 or height >= 2000:
                return 2160
            if width >= 2400 or height >= 1400:
                return 1440
            if width >= 1800 or height >= 1000:
                return 1080
            if width >= 1200 or height >= 700:
                return 720
            return 480
    return resolution(f"{source.get('Name') or ''} {source.get('Path') or ''}")


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
        logger.info("电影订阅无通知：已屏蔽系统通知")
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
        logger.info("电影订阅无通知：已屏蔽下载链通知")
        return None

    async def async_post_message(self, *args, **kwargs):
        logger.info("电影订阅无通知：已屏蔽下载链异步通知")
        return None


class SilentSubscribeChain(SubscribeChain):
    """
    SubscribeChain variant that keeps subscription behavior but suppresses notifications.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.messagehelper = _SilentMessageHelper(self.messagehelper)

    def post_message(self, *args, **kwargs):
        logger.info("电影订阅无通知：已屏蔽订阅链通知")
        return None

    async def async_post_message(self, *args, **kwargs):
        logger.info("电影订阅无通知：已屏蔽订阅链异步通知")
        return None


class RssSubscribeMovieNoNotify(_PluginBase):
    # 插件名称
    plugin_name = "电影订阅无通知"
    # 插件描述
    plugin_desc = "定时刷新电影 RSS，仅处理电影：同一部电影只下载最好的一个版本（4K 优先、同分辨率码率高优先），媒体库已有低分辨率版本时也升级一次，不发送系统通知。"
    # 插件图标
    plugin_icon = "rss.png"
    # 插件版本
    plugin_version = "2.3.0"
    # 插件作者
    plugin_author = "jxxghp / misaya"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "rsssubscribemovienonotify_"
    # 加载顺序
    plugin_order = 21
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
            self._clear = config.get("clear")
            self._action = config.get("action")
            self._save_path = str(config.get("save_path") or "").strip()
            self._size_range = config.get("size_range")

        self.__migrate_history_identity()

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("电影订阅无通知服务启动，立即运行一次")
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
                "summary": "删除电影订阅无通知历史记录",
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
                "id": "RssSubscribeMovieNoNotify",
                "name": "电影订阅无通知服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.check,
                "kwargs": {}
            }]
        elif self._enabled:
            return [{
                "id": "RssSubscribeMovieNoNotify",
                "name": "电影订阅无通知服务",
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
                                    'api': 'plugin/RssSubscribeMovieNoNotify/delete_history',
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
                f"刷新 RSS 源发生未预期异常，已跳过：{mask_url(url)}，"
                f"错误：{mask_url(str(err))}"
            )
            return []

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
            logger.warning("电影订阅无通知正在运行，本次任务跳过")
            return
        try:
            self.__check()
        except Exception as err:
            logger.error(f'电影订阅无通知运行失败：{str(err)} - {traceback.format_exc()}')
        finally:
            lock.release()

    def __check(self):
        """
        读完所有 RSS 后按电影分组：每部电影只下载一个最好的版本，之后最多再升级一次分辨率。
        """
        if not self._address:
            return
        # 读取历史记录
        if self._clearflag:
            history = []
        else:
            history: List[dict] = self.get_data('history') or []
            if not isinstance(history, list):
                logger.warning("电影订阅无通知历史数据格式异常，将从空历史重新开始")
                history = []
        processed_keys = {
            str(item.get("key"))
            for item in history
            if isinstance(item, dict) and item.get("key")
        }
        downloads = self.get_data("movie_downloads_v1") or {}
        if not isinstance(downloads, dict):
            downloads = {}
        downloadchain = SilentDownloadChain()
        subscribechain = SilentSubscribeChain()
        rulehelper = RuleHelper()
        filter_groups = self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
        # 1. 读完所有 RSS 源，按电影分组；RSS 源的先后顺序就是同等画质下的站点优先级
        movies: Dict[str, dict] = {}
        order = 0
        for url in self._address.splitlines():
            url = url.strip()
            if not url:
                continue
            logger.info(f"开始刷新RSS：{mask_url(url)} ...")
            for result in self.__fetch_rss(url):
                order += 1
                try:
                    candidate = self.__candidate(result, processed_keys, rulehelper, filter_groups)
                except Exception as err:
                    logger.error(f'刷新RSS数据出错：{str(err)} - {traceback.format_exc()}')
                    continue
                if not candidate:
                    continue
                candidate["order"] = order
                movies.setdefault(candidate["media_key"], []).append(candidate)
            logger.info(f"RSS {mask_url(url)} 刷新完成")
        # 2. 每部电影只处理一次
        for media_key, candidates in movies.items():
            try:
                self.__handle_movie(media_key, candidates, downloads, history, processed_keys,
                                    downloadchain, subscribechain)
            except Exception as err:
                logger.error(f'处理电影出错：{str(err)} - {traceback.format_exc()}')
            self.__persist_history(history)
        # 保存历史记录
        self.__persist_history(history)
        # 缓存只清理一次
        self._clearflag = False

    def __candidate(self, result: dict, processed_keys: set, rulehelper, filter_groups) -> Optional[dict]:
        """过滤并识别单条 RSS，返回可下载的电影候选。"""
        title = result.get("title")
        description = result.get("description")
        enclosure = result.get("enclosure")
        link = result.get("link")
        size = result.get("size")
        pubdate: datetime.datetime = result.get("pubdate")
        # 检查是否处理过
        rss_key = self.__rss_key(result)
        if not title or not rss_key or rss_key in processed_keys or title in processed_keys:
            return None
        # 检查规则
        if self._include and not re.search(r"%s" % self._include,
                                           f"{title} {description}", re.IGNORECASE):
            logger.info(f"{title} - {description} 不符合包含规则")
            return None
        if self._exclude and re.search(r"%s" % self._exclude,
                                       f"{title} {description}", re.IGNORECASE):
            logger.info(f"{title} - {description} 不符合排除规则")
            return None
        if self._size_range:
            sizes = [float(_size) * 1024 ** 3 for _size in self._size_range.split("-")]
            if len(sizes) == 1 and float(size) < sizes[0]:
                logger.info(f"{title} - 种子大小不符合条件")
                return None
            elif len(sizes) > 1 and not sizes[0] <= float(size) <= sizes[1]:
                logger.info(f"{title} - 种子大小不在指定范围")
                return None
        # 识别媒体信息
        meta = MetaInfo(title=title, subtitle=description)
        if not meta.name:
            logger.warning(f"{title} 未识别到有效数据")
            return None
        mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
        if not mediainfo:
            logger.warning(f'未识别到媒体信息，标题：{title}')
            return None
        if mediainfo.type != MediaType.MOVIE:
            logger.info(f'{title} - 识别为{mediainfo.type.value}，电影订阅无通知跳过')
            return None
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
                return None
            matched_torrents = self.chain.filter_torrents(
                rule_groups=filter_groups,
                torrent_list=[torrentinfo],
                mediainfo=mediainfo
            )
            if not matched_torrents:
                logger.info(f"{title} - 不匹配订阅规则组：{', '.join(active_group_names)}")
                return None
            torrentinfo = matched_torrents[0]
        try:
            size_value = float(size or 0)
        except (TypeError, ValueError):
            size_value = 0
        return {
            "title": title, "rss_key": rss_key, "meta": meta, "mediainfo": mediainfo,
            "torrentinfo": torrentinfo, "res": resolution(title), "size": size_value,
            "media_key": f"{mediainfo.media_source.value}:{mediainfo.media_id}",
        }

    def __handle_movie(self, media_key: str, candidates: List[dict], downloads: dict, history: List[dict],
                       processed_keys: set, downloadchain, subscribechain) -> None:
        """同一部电影的多个版本只挑一个：分辨率优先，同分辨率体积大（码率高）优先，再按 RSS 源顺序。"""
        mediainfo: MediaInfo = candidates[0]["mediainfo"]
        if self._action != "download":
            first = candidates[0]
            if subscribechain.exists(mediainfo=mediainfo, meta=first["meta"]):
                logger.info(f'{mediainfo.title_year} 正在订阅中')
                return
            subscribe_id, subscribe_message = subscribechain.add(
                title=mediainfo.title,
                year=mediainfo.year,
                mtype=mediainfo.type,
                media_source=mediainfo.media_source,
                media_id=mediainfo.media_id,
                season=None,
                exist_ok=True,
                message=False,
                username="电影RSS订阅无通知"
            )
            if not subscribe_id:
                logger.error(f"{mediainfo.title_year} 添加订阅失败：{subscribe_message or '未知错误'}")
                return
            self.__add_history(history, processed_keys, mediainfo, first)
            return
        record = downloads.get(media_key)
        if isinstance(record, dict):
            have = int(record.get("res") or 0)
            if int(record.get("count") or 1) >= MAX_DOWNLOADS_PER_MOVIE:
                logger.info(f'{mediainfo.title_year} 已下载 {record.get("count")} 次，不再下载')
                return
            candidates = [c for c in candidates if c["res"] > have]
            if not candidates:
                logger.info(f'{mediainfo.title_year} 已下载 {have or "未知"}p，没有更高分辨率的版本')
                return
        else:
            record = None
            # 媒体库已存在
            exist_info: Optional[ExistMediaInfo] = self.chain.media_exists(mediainfo=mediainfo)
            if exist_info:
                best = max(c["res"] for c in candidates)
                have = self.__library_resolution(mediainfo) if best else None
                if not have or have >= best:
                    logger.info(f'{mediainfo.title_year} 己存在'
                                f'{f"（{have}p）" if have else "，画质未知不升级" if best else ""}')
                    return
                # 媒体库已有版本算作一次下载，只再升级一次
                record = {"res": have, "count": 1}
                candidates = [c for c in candidates if c["res"] > have]
        ranked = sorted(candidates, key=lambda c: (-c["res"], -c["size"], c["order"]))
        for candidate in ranked:
            result = downloadchain.download_single(
                context=Context(
                    meta_info=candidate["meta"],
                    media_info=mediainfo,
                    torrent_info=candidate["torrentinfo"],
                ),
                save_path=self._save_path or None,
                username="电影RSS订阅无通知"
            )
            if not result:
                logger.error(f'{candidate["title"]} 下载失败，尝试下一个版本')
                continue
            logger.info(f'{mediainfo.title_year} 共 {len(candidates)} 个版本，'
                        f'{"升级" if record else "下载"}：{candidate["title"]}')
            downloads[media_key] = {
                "res": candidate["res"], "count": (record or {}).get("count", 0) + 1,
                "title": candidate["title"], "t": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            if len(downloads) > DOWNLOAD_RECORD_LIMIT:
                for key in sorted(downloads, key=lambda k: str(downloads[k].get("t")))[:-DOWNLOAD_RECORD_LIMIT]:
                    downloads.pop(key, None)
            self.save_data("movie_downloads_v1", downloads)
            self.__add_history(history, processed_keys, mediainfo, candidate)
            return

    def __library_resolution(self, mediainfo: MediaInfo) -> Optional[int]:
        """Emby/Jellyfin 里这部电影最好版本的分辨率；有版本认不出分辨率或查询失败返回 None，不升级。"""
        import requests
        source = {"themoviedb": "tmdb", "tmdb": "tmdb"}.get(str(mediainfo.media_source.value).lower())
        if not source:
            return None
        found = []
        try:
            for service in (MediaServerHelper().get_services() or {}).values():
                server_type = str(service.type).lower()
                if server_type not in ("emby", "jellyfin"):
                    continue
                config = service.config.config or {}
                host = str(config.get("host") or "").rstrip("/")
                token = config.get("apikey")
                if not host.startswith(("http://", "https://")) or not token:
                    continue
                base = host + ("/" if host.endswith("/emby") else "/emby/" if server_type == "emby" else "/")
                response = requests.get(base + "Items", params={
                    "Recursive": "true", "IncludeItemTypes": "Movie", "IsVirtual": "false",
                    "AnyProviderIdEquals": f"{source}.{mediainfo.media_id}",
                    "Fields": "ProviderIds,Path,MediaSources"}, headers={"X-Emby-Token": token}, timeout=15)
                response.raise_for_status()
                for item in response.json().get("Items") or []:
                    ids = {str(k).lower(): str(v) for k, v in (item.get("ProviderIds") or {}).items()}
                    if ids.get(source) != str(mediainfo.media_id):
                        continue
                    for media_source in item.get("MediaSources") or [item]:
                        found.append(source_resolution(media_source))
        except Exception:
            # 不记录异常原文，避免把服务器地址或密钥写进日志
            logger.warning(f"{mediainfo.title_year} 查询媒体库画质失败，不升级")
            return None
        if not found or 0 in found:
            return None
        return max(found)

    def __add_history(self, history: List[dict], processed_keys: set, mediainfo: MediaInfo, candidate: dict):
        history_item = {
            "title": mediainfo.title_year,
            "key": candidate["rss_key"],
            "rss_title": candidate["title"],
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
        processed_keys.add(candidate["rss_key"])

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
            self.__log_and_notify_error(f"电影订阅无通知出错，种子大小设置错误：{size_range}")
            config["size_range"] = None
            return False
        return True

    @staticmethod
    def __is_number_or_range(value):
        """
        检查字符串是否表示单个数字或数字范围（如'5', '5.5', '5-10' 或 '5.5-10.2'）
        """
        return bool(re.match(r"^\d+(\.\d+)?(-\d+(\.\d+)?)?$", value))
