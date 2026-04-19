import datetime
import json
import re
import traceback
from dataclasses import dataclass
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple, Set

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.download import DownloadChain
from app.core.config import settings
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ExistMediaInfo
from app.schemas.types import MediaType, SystemConfigKey

lock = Lock()
COMPLETE_HINTS = re.compile(
    r"(complete|全集|全季|全\s*\d+\s*[集话]|season\s*\d+\s*complete|s\d+\s*complete|fin(al|ale)|完结|完結)",
    re.IGNORECASE,
)
MULTI_EPISODE_HINTS = re.compile(
    r"("
    r"s\d{1,2}e\d{1,3}\s*[-~]\s*e?\d{1,3}"
    r"|e\d{1,3}\s*[-~]\s*e?\d{1,3}"
    r"|第\s*\d+\s*[-~到至]\s*\d+\s*集"
    r"|\b\d{1,3}\s*[-~]\s*\d{1,3}\b"
    r")",
    re.IGNORECASE,
)


@dataclass
class RssSource:
    name: str
    urls: List[str]
    enabled: bool = True
    include: str = ""
    exclude: str = ""
    save_path: str = ""
    proxy: bool = False
    filter: Optional[bool] = None


class MultiRssSubscribe(_PluginBase):
    plugin_name = "多源RSS订阅"
    plugin_desc = "按官方 rsssubscribe 逻辑统一调度多条 RSS 源，并可静音下载通知，降低 TG 通知链卡住任务的概率。"
    plugin_icon = "rss.png"
    plugin_version = "1.1.0"
    plugin_author = "Codex"
    author_url = "https://github.com/openai"
    plugin_config_prefix = "multirsssubscribe_"
    plugin_order = 22
    auth_level = 2

    _scheduler: Optional[BackgroundScheduler] = None

    _enabled: bool = False
    _onlyonce: bool = False
    _clear: bool = False
    _clearflag: bool = False
    _cron: str = "*/30 * * * *"
    _mute_notify: bool = True
    _apply_filter_rules: bool = False
    _skip_complete: bool = True
    _skip_tv_without_episode: bool = True
    _sources_json: str = "[]"
    _sources: List[RssSource] = []
    _pending_run: bool = False

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled"))
            self._onlyonce = bool(config.get("onlyonce"))
            self._clear = bool(config.get("clear"))
            self._cron = config.get("cron") or "*/30 * * * *"
            self._mute_notify = bool(config.get("mute_notify", True))
            self._apply_filter_rules = bool(config.get("apply_filter_rules", False))
            self._skip_complete = bool(config.get("skip_complete", True))
            self._skip_tv_without_episode = bool(config.get("skip_tv_without_episode", True))
            self._sources_json = config.get("sources_json") or "[]"

        self._sources = self.__parse_sources_json(self._sources_json)

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("多源RSS订阅服务启动，立即运行一次")
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
                "summary": "删除多源RSS订阅历史记录",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [
                {
                    "id": "MultiRssSubscribe",
                    "name": "多源RSS订阅服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.check,
                    "kwargs": {"max_instances": 2},
                }
            ]
        if self._enabled:
            return [
                {
                    "id": "MultiRssSubscribe",
                    "name": "多源RSS订阅服务",
                    "trigger": "interval",
                    "func": self.check,
                    "kwargs": {"minutes": 30, "max_instances": 2},
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
                            _col(3, _switch("enabled", "启用插件")),
                            _col(3, _switch("onlyonce", "立即运行一次")),
                            _col(3, _switch("clear", "清理历史记录")),
                            _col(3, _switch("mute_notify", "静音下载通知")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _cronfield("cron", "执行周期", "5位 cron 表达式")),
                            _col(3, _switch("apply_filter_rules", "应用MoviePilot优先规则")),
                            _col(3, _switch("skip_complete", "整季/完结包直接跳过")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(3, _switch("skip_tv_without_episode", "电视剧无集号则跳过")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(
                                12,
                                _textarea(
                                    "sources_json",
                                    "RSS 源配置(JSON)",
                                    (
                                        "填写 JSON 数组，每个元素代表一个 RSS 源。"
                                        "支持字段：name、enabled、rss(字符串或数组)、save_path、include、exclude、proxy、filter"
                                    ),
                                    rows=16,
                                ),
                            )
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "这个插件按官方 rsssubscribe 的识别、查库、下载顺序来跑，"
                                                "但把多个 RSS 源统一放到一个插件里调度。"
                                                "任意单个 RSS 源报错，只会跳过当前源，不会把其它源一起带停。"
                                                "如果开启 MoviePilot 优先规则，就会复用系统里的订阅优先级筛选逻辑。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": (
                                                "建议保持“静音下载通知”开启：下载仍会正常推送到下载器，"
                                                "只是屏蔽 MoviePilot 默认的同步消息发送，"
                                                "用来降低 Telegram 等通知链阻塞当前 RSS 轮次的概率。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": (
                                                "配置示例："
                                                '[{"name":"观众剧集","enabled":true,"rss":["https://audiences.me/xxx"],'
                                                '"save_path":"","include":"","exclude":"","proxy":false,"filter":true},'
                                                '{"name":"馒头剧集","enabled":true,"rss":"https://rss.m-team.cc/xxx"}]'
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "clear": False,
            "cron": "*/30 * * * *",
            "mute_notify": True,
            "apply_filter_rules": False,
            "skip_complete": True,
            "skip_tv_without_episode": True,
            "sources_json": "[]",
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data("history") or []
        if not historys:
            return [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {"class": "text-center"},
                }
            ]

        historys = sorted(historys, key=lambda item: item.get("time"), reverse=True)
        rows = []
        for item in historys:
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": item.get("source", "")},
                        {"component": "td", "text": item.get("title", "")},
                        {"component": "td", "text": item.get("season_episode", "")},
                        {"component": "td", "text": item.get("raw_title", "")},
                        {"component": "td", "text": item.get("time", "")},
                    ],
                }
            )

        return [
            {
                "component": "VTable",
                "props": {"hover": True},
                "content": [
                    {
                        "component": "thead",
                        "content": [
                            {
                                "component": "tr",
                                "content": [
                                    {"component": "th", "text": "来源"},
                                    {"component": "th", "text": "标题"},
                                    {"component": "th", "text": "季集"},
                                    {"component": "th", "text": "种子标题"},
                                    {"component": "th", "text": "时间"},
                                ],
                            }
                        ],
                    },
                    {"component": "tbody", "content": rows},
                ],
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

    def delete_history(self, key: str = "", apikey: str = ""):
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")

        historys = self.get_data("history") or []
        if not key:
            self.save_data("history", [])
            return schemas.Response(success=True, message="历史记录已清空")

        historys = [item for item in historys if item.get("key") != key]
        self.save_data("history", historys)
        return schemas.Response(success=True, message="删除成功")

    def check(self):
        if not lock.acquire(blocking=False):
            self._pending_run = True
            logger.warning("多源RSS订阅任务仍在运行，已登记本轮补跑请求")
            return

        try:
            while True:
                self._pending_run = False
                self.__run_once()
                if not self._pending_run:
                    break
                logger.info("检测到调度期间有新一轮请求，当前执行结束后立即补跑一次")
        finally:
            lock.release()

    def __run_once(self):
        history = [] if self._clearflag else list(self.get_data("history") or [])
        history_keys = self.__history_keys(history)
        history_changed = self._clearflag
        source_count = 0
        download_total = 0

        try:
            if not self._sources:
                logger.info("未配置有效 RSS 源，跳过本次执行")
                return

            filter_groups = self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
            for source in self._sources:
                if not source.enabled or not source.urls:
                    continue

                source_count += 1
                logger.info("[%s] 开始处理，共 %s 条 RSS 地址", source.name, len(source.urls))
                try:
                    source_download_total, source_changed = self.__run_source(
                        source=source,
                        filter_groups=filter_groups,
                        history=history,
                        history_keys=history_keys,
                    )
                    download_total += source_download_total
                    history_changed = history_changed or source_changed
                except Exception:
                    logger.error("[%s] RSS 源处理出错：%s", source.name, traceback.format_exc())
        finally:
            if history_changed:
                self.save_data("history", history)
            self._clearflag = False

            if not source_count:
                logger.info("没有启用的 RSS 源，跳过本次执行")
            elif not download_total:
                logger.info("多源RSS订阅本轮未推送任何新资源")
            else:
                logger.info("多源RSS订阅本轮完成，成功推送 %s 条资源", download_total)

    def __run_source(
        self,
        source: RssSource,
        filter_groups: Any,
        history: List[dict],
        history_keys: Set[str],
    ) -> Tuple[int, bool]:
        download_total = 0
        changed = False
        downloadchain = DownloadChain()
        original_post_message = getattr(downloadchain, "post_message", None)

        try:
            if self._mute_notify and original_post_message:
                downloadchain.post_message = self.__suppress_post_message

            for url in source.urls:
                logger.info("[%s] 开始刷新 RSS：%s ...", source.name, url)
                results = RssHelper().parse(url, proxy=source.proxy)
                if not results:
                    logger.error("[%s] 未获取到 RSS 数据：%s", source.name, url)
                    continue

                for result in results:
                    try:
                        if self.__process_result(
                            source=source,
                            result=result,
                            filter_groups=filter_groups,
                            downloadchain=downloadchain,
                            history=history,
                            history_keys=history_keys,
                        ):
                            download_total += 1
                            changed = True
                    except Exception:
                        logger.error("[%s] 刷新 RSS 条目出错：%s", source.name, traceback.format_exc())

                logger.info("[%s] RSS %s 刷新完成", source.name, url)
        finally:
            if original_post_message:
                downloadchain.post_message = original_post_message

        logger.info("[%s] 本轮处理完成，成功推送 %s 条", source.name, download_total)
        return download_total, changed

    def __process_result(
        self,
        source: RssSource,
        result: dict,
        filter_groups: Any,
        downloadchain: DownloadChain,
        history: List[dict],
        history_keys: Set[str],
    ) -> bool:
        title = result.get("title")
        description = result.get("description")
        enclosure = result.get("enclosure")
        link = result.get("link")
        size = result.get("size")
        pubdate = result.get("pubdate")

        if not title or title in history_keys:
            return False

        if not self.__match_text_filters(source=source, title=title, description=description):
            logger.info("[%s] %s 不符合包含/排除规则", source.name, title)
            return False

        meta = MetaInfo(title=title, subtitle=description)
        if not meta.name:
            logger.warning("[%s] %s 未识别到有效数据", source.name, title)
            return False

        mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
        if not mediainfo:
            logger.warning("[%s] 未识别到媒体信息，标题：%s", source.name, title)
            return False

        torrentinfo = TorrentInfo(
            title=title,
            description=description,
            enclosure=enclosure,
            page_url=link,
            size=size,
            pubdate=self.__pubdate_text(pubdate),
            site_proxy=source.proxy,
        )
        if self.__use_filter_rules(source):
            filtered = self.chain.filter_torrents(
                rule_groups=filter_groups,
                torrent_list=[torrentinfo],
                mediainfo=mediainfo,
            )
            if not filtered:
                logger.info("[%s] %s 不匹配订阅优先级规则", source.name, title)
                return False

        exist_info = self.chain.media_exists(mediainfo=mediainfo)
        skip_reason = self.__library_skip_reason(
            title=title,
            meta=meta,
            mediainfo=mediainfo,
            exist_info=exist_info,
        )
        if skip_reason:
            logger.info("[%s] %s", source.name, skip_reason)
            return False

        download_result = downloadchain.download_single(
            context=Context(
                meta_info=meta,
                media_info=mediainfo,
                torrent_info=torrentinfo,
            ),
            save_path=source.save_path,
            username="多源RSS订阅",
        )
        if not download_result:
            logger.error("[%s] %s 下载失败", source.name, title)
            return False

        record = {
            "source": source.name,
            "title": f"{mediainfo.title} {meta.season}".strip(),
            "key": title,
            "type": mediainfo.type.value,
            "year": mediainfo.year,
            "poster": mediainfo.get_poster_image(),
            "overview": mediainfo.overview,
            "tmdbid": mediainfo.tmdb_id,
            "season_episode": self.__season_episode_text(meta),
            "raw_title": title,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        history.append(record)
        history_keys.add(title)
        return True

    def __library_skip_reason(
        self,
        title: str,
        meta: MetaInfo,
        mediainfo: MediaInfo,
        exist_info: Optional[ExistMediaInfo],
    ) -> Optional[str]:
        if mediainfo.type == MediaType.TV:
            if self._skip_complete and self.__is_complete_pack(title):
                return f"{title} 命中完结包规则，已跳过"

            if self._skip_complete and self.__is_season_pack(title=title, meta=meta):
                return f"{title} 命中整季包规则，已跳过"

            if self._skip_tv_without_episode and not (meta.episode_list or []):
                return f"{title} 未识别到集号，按配置跳过"

            if exist_info and self.__all_episodes_exist(meta=meta, exist_info=exist_info):
                season_episode = self.__season_episode_text(meta)
                return f"{mediainfo.title_year} {season_episode} 已存在".strip()
            return None

        if exist_info:
            return f"{mediainfo.title_year} 已存在"
        return None

    def __match_text_filters(self, source: RssSource, title: str, description: Optional[str]) -> bool:
        text = f"{title} {description or ''}"
        if source.include and not re.search(source.include, text, re.IGNORECASE):
            return False
        if source.exclude and re.search(source.exclude, text, re.IGNORECASE):
            return False
        return True

    def __use_filter_rules(self, source: RssSource) -> bool:
        if source.filter is None:
            return self._apply_filter_rules
        return bool(source.filter)

    @staticmethod
    def __all_episodes_exist(meta: MetaInfo, exist_info: ExistMediaInfo) -> bool:
        if not meta.begin_season or not meta.episode_list:
            return False
        exist_season = exist_info.seasons
        if not exist_season:
            return False
        exist_episodes = exist_season.get(meta.begin_season)
        if not exist_episodes:
            return False
        return set(meta.episode_list).issubset(set(exist_episodes))

    @staticmethod
    def __season_episode_text(meta: MetaInfo) -> str:
        season = getattr(meta, "begin_season", None)
        episodes = sorted(set(getattr(meta, "episode_list", []) or []))
        if season and episodes:
            if len(episodes) == 1:
                return f"S{int(season):02d}E{int(episodes[0]):02d}"
            return f"S{int(season):02d}E{int(episodes[0]):02d}-E{int(episodes[-1]):02d}"
        if meta.season_episode:
            return meta.season_episode
        if meta.season:
            return meta.season
        return ""

    def __is_complete_pack(self, title: str) -> bool:
        normalized = (title or "").lower()
        if not COMPLETE_HINTS.search(normalized):
            return False
        if MULTI_EPISODE_HINTS.search(normalized):
            return False
        return True

    def __is_season_pack(self, title: str, meta: MetaInfo) -> bool:
        if meta.episode_list:
            return False

        normalized = (title or "").lower()
        if MULTI_EPISODE_HINTS.search(normalized):
            return False

        season_markers = [
            r"\bseason\s*\d{1,2}\b",
            r"\bs\d{1,2}\b",
            r"第\s*[0-9一二三四五六七八九十]{1,3}\s*季",
            r"合集",
            r"全季",
            r"全集",
            r"complete",
            r"pack",
        ]
        if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in season_markers):
            return True

        return bool(getattr(meta, "begin_season", None))

    @staticmethod
    def __history_keys(history: List[dict]) -> Set[str]:
        return {str(item.get("key")).strip() for item in history if str(item.get("key") or "").strip()}

    def __parse_sources_json(self, raw_value: str) -> List[RssSource]:
        raw_value = (raw_value or "").strip() or "[]"
        try:
            data = json.loads(raw_value)
        except Exception as err:
            logger.error("RSS 源配置 JSON 解析失败：%s", str(err))
            return []

        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            logger.error("RSS 源配置必须是 JSON 数组")
            return []

        sources: List[RssSource] = []
        for index, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                logger.warning("第 %s 个 RSS 源配置不是对象，已跳过", index)
                continue

            urls = self.__source_urls(item.get("rss") or item.get("urls"))
            name = str(item.get("name") or "").strip()
            if not name:
                name = f"RSS源{index}"
            if not urls:
                logger.warning("[%s] 没有有效 RSS 地址，已跳过", name)
                continue

            sources.append(
                RssSource(
                    name=name,
                    urls=urls,
                    enabled=bool(item.get("enabled", True)),
                    include=str(item.get("include") or "").strip(),
                    exclude=str(item.get("exclude") or "").strip(),
                    save_path=str(item.get("save_path") or "").strip(),
                    proxy=bool(item.get("proxy", False)),
                    filter=self.__source_filter_value(item.get("filter")),
                )
            )

        return sources

    @staticmethod
    def __source_urls(value: Any) -> List[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        return []

    @staticmethod
    def __source_filter_value(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
            return None
        return bool(value)

    @staticmethod
    def __pubdate_text(value: Any) -> Optional[str]:
        if not value:
            return None
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value)

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "clear": self._clear,
                "cron": self._cron,
                "mute_notify": self._mute_notify,
                "apply_filter_rules": self._apply_filter_rules,
                "skip_complete": self._skip_complete,
                "skip_tv_without_episode": self._skip_tv_without_episode,
                "sources_json": self._sources_json,
            }
        )

    @staticmethod
    def __suppress_post_message(*args, **kwargs):
        return None


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
