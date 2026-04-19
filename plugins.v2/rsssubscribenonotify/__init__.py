import datetime
import re
import traceback
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ExistMediaInfo
from app.schemas.types import MediaType, SystemConfigKey


class RssSubscribeNoNotify(_PluginBase):
    plugin_name = "自定义订阅无通知"
    plugin_desc = "基于官方 rsssubscribe 流程，定时刷新 RSS 识别内容后添加订阅或直接下载，但关闭插件触发的通知链。"
    plugin_icon = "rss.png"
    plugin_version = "1.0.0"
    plugin_author = "Codex"
    author_url = "https://github.com/openai"
    plugin_config_prefix = "rsssubscribenonotify_"
    plugin_order = 23
    auth_level = 2

    _scheduler: Optional[BackgroundScheduler] = None

    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _address: str = ""
    _include: str = ""
    _exclude: str = ""
    _proxy: bool = False
    _filter: bool = False
    _clear: bool = False
    _clearflag: bool = False
    _action: str = "download"
    _save_path: str = ""
    _size_range: str = ""

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self.__validate_and_fix_config(config=config)
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._address = config.get("address")
            self._include = config.get("include")
            self._exclude = config.get("exclude")
            self._proxy = config.get("proxy")
            self._filter = config.get("filter")
            self._clear = config.get("clear")
            self._action = config.get("action")
            self._save_path = config.get("save_path")
            self._size_range = config.get("size_range")

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("自定义订阅无通知服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.check,
                trigger="date",
                run_date=datetime.datetime.now(
                    tz=pytz.timezone(settings.TZ)
                ) + datetime.timedelta(seconds=3),
            )

            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

        if self._onlyonce or self._clear:
            self._onlyonce = False
            self._clearflag = self._clear
            self._clear = False
            self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除自定义订阅无通知历史记录",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [
                {
                    "id": "RssSubscribeNoNotify",
                    "name": "自定义订阅无通知服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.check,
                    "kwargs": {},
                }
            ]
        if self._enabled:
            return [
                {
                    "id": "RssSubscribeNoNotify",
                    "name": "自定义订阅无通知服务",
                    "trigger": "interval",
                    "func": self.check,
                    "kwargs": {"minutes": 30},
                }
            ]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            _col(4, _switch("enabled", "启用插件")),
                            _col(4, _switch("onlyonce", "立即运行一次")),
                            _col(4, _switch("clear", "清理历史记录")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _cronfield("cron", "执行周期", "5位cron表达式，留空自动")),
                            _col(
                                6,
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": "action",
                                        "label": "动作",
                                        "items": [
                                            {"title": "订阅", "value": "subscribe"},
                                            {"title": "下载", "value": "download"},
                                        ],
                                    },
                                },
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(
                                12,
                                _textarea("address", "RSS地址", "每行一个RSS地址", rows=3),
                            )
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _textfield("include", "包含", "支持正则表达式")),
                            _col(6, _textfield("exclude", "排除", "支持正则表达式")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _textfield("size_range", "种子大小(GB)", "如：3 或 3-5")),
                            _col(6, _textfield("save_path", "保存目录", "下载时有效，留空自动")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(4, _switch("proxy", "使用代理服务器")),
                            _col(4, _switch("filter", "使用订阅优先级规则")),
                            _col(
                                4,
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "warning",
                                        "variant": "tonal",
                                        "text": "本插件已关闭通知：不会调用插件系统通知，也会静音下载/订阅链里的同步消息发送。",
                                    },
                                },
                            ),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "*/30 * * * *",
            "address": "",
            "include": "",
            "exclude": "",
            "proxy": False,
            "clear": False,
            "filter": False,
            "action": "download",
            "save_path": "",
            "size_range": "",
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data("history")
        if not historys:
            return [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {"class": "text-center"},
                }
            ]

        historys = sorted(historys, key=lambda x: x.get("time"), reverse=True)
        contents = []
        for history in historys:
            title = history.get("title")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            contents.append(
                {
                    "component": "VCard",
                    "content": [
                        {
                            "component": "VDialogCloseBtn",
                            "props": {"innerClass": "absolute top-0 right-0"},
                            "events": {
                                "click": {
                                    "api": "plugin/RssSubscribeNoNotify/delete_history",
                                    "method": "get",
                                    "params": {
                                        "key": title,
                                        "apikey": settings.API_TOKEN,
                                    },
                                }
                            },
                        },
                        {
                            "component": "div",
                            "props": {
                                "class": "d-flex justify-space-start flex-nowrap flex-row",
                            },
                            "content": [
                                {
                                    "component": "div",
                                    "content": [
                                        {
                                            "component": "VImg",
                                            "props": {
                                                "src": poster,
                                                "height": 120,
                                                "width": 80,
                                                "aspect-ratio": "2/3",
                                                "class": "object-cover shadow ring-gray-500",
                                                "cover": True,
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "div",
                                    "content": [
                                        {
                                            "component": "VCardTitle",
                                            "props": {
                                                "class": "pa-1 pe-5 break-words whitespace-break-spaces"
                                            },
                                            "text": title,
                                        },
                                        {
                                            "component": "VCardText",
                                            "props": {"class": "pa-0 px-2"},
                                            "text": f"类型：{mtype}",
                                        },
                                        {
                                            "component": "VCardText",
                                            "props": {"class": "pa-0 px-2"},
                                            "text": f"时间：{time_str}",
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                }
            )

        return [
            {
                "component": "div",
                "props": {"class": "grid gap-3 grid-info-card"},
                "content": contents,
            }
        ]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.error("退出插件失败：%s", str(err))

    def delete_history(self, key: str, apikey: str):
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        historys = self.get_data("history")
        if not historys:
            return schemas.Response(success=False, message="未找到历史记录")
        historys = [item for item in historys if item.get("title") != key]
        self.save_data("history", historys)
        return schemas.Response(success=True, message="删除成功")

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
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
                "size_range": self._size_range,
            }
        )

    def check(self):
        if not self._address:
            return

        if self._clearflag:
            history = []
        else:
            history: List[dict] = self.get_data("history") or []

        downloadchain = DownloadChain()
        subscribechain = SubscribeChain()
        original_download_post_message = getattr(downloadchain, "post_message", None)
        original_subscribe_post_message = getattr(subscribechain, "post_message", None)

        try:
            self.__mute_chain_message(downloadchain)
            self.__mute_chain_message(subscribechain)
            self.__check_with_chains(
                history=history,
                downloadchain=downloadchain,
                subscribechain=subscribechain,
            )
        finally:
            if original_download_post_message:
                downloadchain.post_message = original_download_post_message
            if original_subscribe_post_message:
                subscribechain.post_message = original_subscribe_post_message

    def __check_with_chains(
        self,
        history: List[dict],
        downloadchain: DownloadChain,
        subscribechain: SubscribeChain,
    ):
        for url in self._address.split("\n"):
            if not url:
                continue
            logger.info("开始刷新RSS：%s ...", url)
            results = RssHelper().parse(url, proxy=self._proxy)
            if not results:
                logger.error("未获取到RSS数据：%s", url)
                return

            filter_groups = self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
            for result in results:
                try:
                    title = result.get("title")
                    description = result.get("description")
                    enclosure = result.get("enclosure")
                    link = result.get("link")
                    size = result.get("size")
                    pubdate: datetime.datetime = result.get("pubdate")

                    if not title or title in [item.get("key") for item in history]:
                        continue

                    if self._include and not re.search(r"%s" % self._include,
                                                       f"{title} {description}", re.IGNORECASE):
                        logger.info("%s - %s 不符合包含规则", title, description)
                        continue
                    if self._exclude and re.search(r"%s" % self._exclude,
                                                   f"{title} {description}", re.IGNORECASE):
                        logger.info("%s - %s 不符合排除规则", title, description)
                        continue
                    if self._size_range:
                        sizes = [float(_size) * 1024 ** 3 for _size in self._size_range.split("-")]
                        if len(sizes) == 1 and float(size) < sizes[0]:
                            logger.info("%s - 种子大小不符合条件", title)
                            continue
                        if len(sizes) > 1 and not sizes[0] <= float(size) <= sizes[1]:
                            logger.info("%s - 种子大小不在指定范围", title)
                            continue

                    meta = MetaInfo(title=title, subtitle=description)
                    if not meta.name:
                        logger.warning("%s 未识别到有效数据", title)
                        continue
                    mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
                    if not mediainfo:
                        logger.warning("未识别到媒体信息，标题：%s", title)
                        continue

                    torrentinfo = TorrentInfo(
                        title=title,
                        description=description,
                        enclosure=enclosure,
                        page_url=link,
                        size=size,
                        pubdate=pubdate.strftime("%Y-%m-%d %H:%M:%S") if pubdate else None,
                        site_proxy=self._proxy,
                    )
                    if self._filter:
                        filtered = self.chain.filter_torrents(
                            rule_groups=filter_groups,
                            torrent_list=[torrentinfo],
                            mediainfo=mediainfo,
                        )
                        if not filtered:
                            logger.info("%s %s 不匹配过滤规则", title, description)
                            continue

                    exist_info: Optional[ExistMediaInfo] = self.chain.media_exists(mediainfo=mediainfo)
                    if mediainfo.type == MediaType.TV:
                        if exist_info:
                            exist_season = exist_info.seasons
                            if exist_season:
                                exist_episodes = exist_season.get(meta.begin_season)
                                if exist_episodes and set(meta.episode_list).issubset(set(exist_episodes)):
                                    logger.info("%s %s 己存在", mediainfo.title_year, meta.season_episode)
                                    continue
                    elif exist_info:
                        logger.info("%s 己存在", mediainfo.title_year)
                        continue

                    if self._action == "download":
                        download_result = downloadchain.download_single(
                            context=Context(
                                meta_info=meta,
                                media_info=mediainfo,
                                torrent_info=torrentinfo,
                            ),
                            save_path=self._save_path,
                            username="RSS订阅无通知",
                        )
                        if not download_result:
                            logger.error("%s 下载失败", title)
                            continue
                    else:
                        subflag = subscribechain.exists(mediainfo=mediainfo, meta=meta)
                        if subflag:
                            logger.info("%s %s 正在订阅中", mediainfo.title_year, meta.season)
                            continue
                        subscribechain.add(
                            title=mediainfo.title,
                            year=mediainfo.year,
                            mtype=mediainfo.type,
                            tmdbid=mediainfo.tmdb_id,
                            season=meta.begin_season,
                            exist_ok=True,
                            username="RSS订阅无通知",
                        )

                    history.append(
                        {
                            "title": f"{mediainfo.title} {meta.season}",
                            "key": f"{title}",
                            "type": mediainfo.type.value,
                            "year": mediainfo.year,
                            "poster": mediainfo.get_poster_image(),
                            "overview": mediainfo.overview,
                            "tmdbid": mediainfo.tmdb_id,
                            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                except Exception as err:
                    logger.error("刷新RSS数据出错：%s - %s", str(err), traceback.format_exc())
            logger.info("RSS %s 刷新完成", url)

        self.save_data("history", history)
        self._clearflag = False

    def __log_and_notify_error(self, message: str):
        logger.error(message)

    def __validate_and_fix_config(self, config: dict = None) -> bool:
        size_range = config.get("size_range")
        if size_range and not self.__is_number_or_range(str(size_range)):
            self.__log_and_notify_error(f"自定义订阅无通知出错，种子大小设置错误：{size_range}")
            config["size_range"] = None
            return False
        return True

    @staticmethod
    def __mute_chain_message(chain: Any):
        if getattr(chain, "post_message", None):
            chain.post_message = RssSubscribeNoNotify.__suppress_post_message

    @staticmethod
    def __suppress_post_message(*args, **kwargs):
        return None

    @staticmethod
    def __is_number_or_range(value):
        return bool(re.match(r"^\d+(\.\d+)?(-\d+(\.\d+)?)?$", value))


def _col(md: int, *children) -> dict:
    return {
        "component": "VCol",
        "props": {"cols": 12, "md": md},
        "content": list(children),
    }


def _switch(model: str, label: str) -> dict:
    return {
        "component": "VSwitch",
        "props": {"model": model, "label": label},
    }


def _textfield(model: str, label: str, placeholder: str = "") -> dict:
    props = {"model": model, "label": label}
    if placeholder:
        props["placeholder"] = placeholder
    return {"component": "VTextField", "props": props}


def _textarea(model: str, label: str, placeholder: str = "", rows: int = 3) -> dict:
    props = {"model": model, "label": label, "rows": rows}
    if placeholder:
        props["placeholder"] = placeholder
    return {"component": "VTextarea", "props": props}


def _cronfield(model: str, label: str, placeholder: str = "") -> dict:
    props = {"model": model, "label": label}
    if placeholder:
        props["placeholder"] = placeholder
    return {"component": "VCronField", "props": props}
