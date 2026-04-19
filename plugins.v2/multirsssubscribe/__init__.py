import datetime
import json
import re
import traceback
from dataclasses import dataclass
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

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
    filter: bool = False


@dataclass
class Candidate:
    source: RssSource
    source_url: str
    raw_title: str
    description: str
    torrent: TorrentInfo
    meta: MetaInfo
    mediainfo: MediaInfo
    group_keys: List[str]
    exist_info: Optional[ExistMediaInfo]
    is_complete_pack: bool

    @property
    def candidate_id(self) -> str:
        if self.torrent.enclosure:
            return self.torrent.enclosure
        if self.torrent.page_url:
            return self.torrent.page_url
        return self.raw_title

    @property
    def title_label(self) -> str:
        return self.mediainfo.title_year or self.raw_title


class MultiRssSubscribe(_PluginBase):
    plugin_name = "多源RSS订阅"
    plugin_desc = "在一个插件中维护多条RSS源，稳定执行识别、过滤和直接下载，避免分身插件丢失或漏跑。"
    plugin_icon = "rss.png"
    plugin_version = "1.0.0"
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
    _cooldown_hours: int = 24
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
            self._cooldown_hours = self.__safe_int(config.get("cooldown_hours"), default=24)
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
                            _col(3, _textfield("cooldown_hours", "重复推送冷却(小时)", "默认 24")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _cronfield("cron", "执行周期", "5位 cron 表达式")),
                            _col(3, _switch("skip_complete", "整季/完结包直接跳过")),
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
                                                "这个插件就是为了解决官方 rsssubscribe 分身不稳定的问题："
                                                "所有 RSS 源都放在一个插件里统一调度，任何单个源报错都不会影响其它源。"
                                                "同一媒体在冷却期内不会重复推送，等媒体库补齐后会自动跳过。"
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
                                                '"save_path":"","include":"","exclude":"","proxy":false,"filter":false},'
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
            "cooldown_hours": 24,
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

        historys = [item for item in historys if item.get("group_key") != key]
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
        history_lookup = self.__load_history_lookup()
        history_changed = self._clearflag
        source_count = 0
        candidate_total = 0
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
                    source_candidate_total, source_download_total, changed = self.__run_source(
                        source=source,
                        filter_groups=filter_groups,
                        history_lookup=history_lookup,
                    )
                    candidate_total += source_candidate_total
                    download_total += source_download_total
                    history_changed = history_changed or changed
                except Exception:
                    logger.error("[%s] RSS 源处理出错：%s", source.name, traceback.format_exc())
        finally:
            if history_changed:
                self.save_data("history", list(history_lookup.values()))
            self._clearflag = False

            if not source_count:
                logger.info("没有启用的 RSS 源，跳过本次执行")
                return

            if not candidate_total:
                logger.info("多源RSS订阅本轮未产生可下载候选资源")
            else:
                logger.info("多源RSS订阅本轮完成，候选 %s 条，成功推送 %s 条", candidate_total, download_total)

    def __run_source(
        self,
        source: RssSource,
        filter_groups: Any,
        history_lookup: Dict[str, dict],
    ) -> Tuple[int, int, bool]:
        candidate_total = 0
        download_total = 0
        changed = False

        for url in source.urls:
            results = self.__fetch_single_rss(source=source, url=url)
            if not results:
                continue

            for result in results:
                try:
                    candidate = self.__build_candidate(
                        source=source,
                        source_url=url,
                        result=result,
                        filter_groups=filter_groups,
                    )
                    if not candidate:
                        continue

                    candidate_total += 1
                    skip_reason = self.__candidate_skip_reason(candidate=candidate, history_lookup=history_lookup)
                    if skip_reason:
                        logger.info("[%s] %s", source.name, skip_reason)
                        continue

                    if not self.__download_candidate(candidate):
                        logger.error("[%s] 下载失败：%s", source.name, candidate.raw_title)
                        continue

                    download_total += 1
                    for group_key in candidate.group_keys:
                        history_lookup[group_key] = self.__build_history_record(
                            candidate=candidate,
                            group_key=group_key,
                        )
                    changed = True
                except Exception:
                    logger.error("[%s] 解析 RSS 条目出错：%s", source.name, traceback.format_exc())

        logger.info("[%s] 本轮处理完成，候选 %s 条，成功推送 %s 条", source.name, candidate_total, download_total)
        return candidate_total, download_total, changed

    def __fetch_single_rss(self, source: RssSource, url: str) -> List[dict]:
        logger.info("[%s] 开始刷新 RSS：%s ...", source.name, url)
        results = RssHelper().parse(url, proxy=source.proxy)
        if results:
            logger.info("[%s] RSS %s 抓取完成，原始条目 %s 条", source.name, url, len(results))
            return results
        logger.warning("[%s] 未获取到 RSS 数据：%s", source.name, url)
        return []

    def __build_candidate(
        self,
        source: RssSource,
        source_url: str,
        result: dict,
        filter_groups: Any,
    ) -> Optional[Candidate]:
        title = result.get("title")
        description = result.get("description")
        enclosure = result.get("enclosure")
        link = result.get("link")
        size = result.get("size")
        pubdate = result.get("pubdate")

        if not title:
            return None
        if not self.__match_text_filters(source=source, title=title, description=description):
            return None

        is_complete_pack = self.__is_complete_pack(title=title)
        meta_title = self.__build_meta_title(title=title, description=description)
        meta = MetaInfo(title=meta_title, subtitle=description)
        if not meta.name:
            logger.warning("[%s] %s 未识别到有效数据", source.name, title)
            return None

        mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
        if not mediainfo:
            logger.warning("[%s] 未识别到媒体信息，标题：%s", source.name, title)
            return None

        torrent = TorrentInfo(
            title=title,
            description=description,
            enclosure=enclosure,
            page_url=link,
            size=size,
            pubdate=self.__pubdate_text(pubdate),
            site_proxy=source.proxy,
        )
        if not self.__match_subscribe_rules(
            source=source,
            torrent=torrent,
            mediainfo=mediainfo,
            filter_groups=filter_groups,
        ):
            logger.info("[%s] %s 不匹配订阅优先级规则", source.name, title)
            return None

        group_keys = self.__build_group_keys(mediainfo=mediainfo, meta=meta)
        if not group_keys:
            logger.info("[%s] %s 未识别到可去重的媒体键", source.name, title)
            return None

        return Candidate(
            source=source,
            source_url=source_url,
            raw_title=title,
            description=description or "",
            torrent=torrent,
            meta=meta,
            mediainfo=mediainfo,
            group_keys=group_keys,
            exist_info=self.chain.media_exists(mediainfo=mediainfo),
            is_complete_pack=is_complete_pack,
        )

    def __candidate_skip_reason(
        self,
        candidate: Candidate,
        history_lookup: Dict[str, dict],
    ) -> Optional[str]:
        title_year = candidate.title_label
        season_episode = self.__season_episode_text(candidate.meta)
        is_season_pack = self.__is_season_pack(candidate)
        is_pack_candidate = candidate.is_complete_pack or is_season_pack

        if candidate.mediainfo.type != MediaType.TV:
            if candidate.exist_info:
                return f"{title_year} 已存在"
            return self.__history_skip_reason(candidate, history_lookup)

        if candidate.is_complete_pack and self._skip_complete:
            if self.__library_missing(candidate):
                return self.__history_skip_reason(candidate, history_lookup)
            season = candidate.meta.season or ""
            return f"{title_year} {season} 命中整季/完结包规则，已直接跳过".strip()

        if self._skip_complete and is_season_pack:
            if self.__library_missing(candidate):
                return self.__history_skip_reason(candidate, history_lookup)
            season = candidate.meta.season or ""
            return f"{title_year} {season} 命中整季包规则，已直接跳过".strip()

        if self._skip_tv_without_episode and not (candidate.meta.episode_list or []) and not is_pack_candidate:
            return f"{candidate.raw_title} 未识别到集号，按配置跳过该电视剧资源"

        if (
            not candidate.is_complete_pack
            and candidate.exist_info
            and self.__all_episodes_exist(meta=candidate.meta, exist_info=candidate.exist_info)
        ):
            return f"{title_year} {season_episode} 已存在".strip()

        return self.__history_skip_reason(candidate, history_lookup)

    def __history_skip_reason(
        self,
        candidate: Candidate,
        history_lookup: Dict[str, dict],
    ) -> Optional[str]:
        if self._cooldown_hours <= 0:
            return None

        for group_key in candidate.group_keys:
            record = history_lookup.get(group_key)
            if not record:
                continue
            if not self.__history_active(record):
                continue

            season_episode = record.get("season_episode") or self.__season_episode_text(candidate.meta)
            return (
                f"{candidate.title_label} {season_episode} 仍在重复推送冷却期内，"
                f"已跳过（来源：{record.get('source', '')}）"
            ).strip()
        return None

    def __build_history_record(self, candidate: Candidate, group_key: str) -> dict:
        now = datetime.datetime.now()
        expire_at = int((now + datetime.timedelta(hours=self._cooldown_hours)).timestamp())
        return {
            "group_key": group_key,
            "source": candidate.source.name,
            "title": candidate.title_label,
            "season_episode": self.__season_episode_text(candidate.meta),
            "raw_title": candidate.raw_title,
            "candidate_id": candidate.candidate_id,
            "poster": candidate.mediainfo.get_poster_image(),
            "tmdbid": candidate.mediainfo.tmdb_id,
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "expire_at": expire_at,
        }

    def __download_candidate(self, candidate: Candidate) -> bool:
        downloadchain = DownloadChain()
        original_post_message = downloadchain.post_message

        try:
            downloadchain.post_message = self.__suppress_post_message
            result = downloadchain.download_single(
                context=Context(
                    meta_info=candidate.meta,
                    media_info=candidate.mediainfo,
                    torrent_info=candidate.torrent,
                ),
                save_path=candidate.source.save_path,
                username="多源RSS订阅",
            )
        except Exception:
            logger.error("[%s] 推送下载失败：%s - %s", candidate.source.name, candidate.raw_title, traceback.format_exc())
            return False
        finally:
            downloadchain.post_message = original_post_message

        return bool(result)

    def __match_text_filters(self, source: RssSource, title: str, description: Optional[str]) -> bool:
        text = f"{title} {description or ''}"
        if source.include and not re.search(source.include, text, re.IGNORECASE):
            return False
        if source.exclude and re.search(source.exclude, text, re.IGNORECASE):
            return False
        return True

    def __match_subscribe_rules(
        self,
        source: RssSource,
        torrent: TorrentInfo,
        mediainfo: MediaInfo,
        filter_groups: Any,
    ) -> bool:
        if not source.filter:
            return True
        filtered = self.chain.filter_torrents(
            rule_groups=filter_groups,
            torrent_list=[torrent],
            mediainfo=mediainfo,
        )
        return bool(filtered)

    def __build_group_keys(self, mediainfo: MediaInfo, meta: MetaInfo) -> List[str]:
        tmdbid = mediainfo.tmdb_id or f"{mediainfo.title}_{mediainfo.year}"
        if mediainfo.type == MediaType.TV:
            season = meta.begin_season or 1
            episodes = meta.episode_list or []
            if episodes:
                return [f"tv:{tmdbid}:s{season}:e{episode}" for episode in sorted(set(episodes))]
            if meta.season_episode:
                return [f"tv:{tmdbid}:s{season}:{meta.season_episode}"]
            return [f"tv:{tmdbid}:s{season}:{meta.name}"]
        return [f"movie:{tmdbid}"]

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
    def __library_missing(candidate: Candidate) -> bool:
        return not bool(candidate.exist_info)

    def __build_meta_title(self, title: str, description: Optional[str]) -> str:
        text = f"{title} {description or ''}"
        meta_title = title
        normalized = title.lower()

        if not re.search(r"s\d{1,2}", normalized):
            season = self.__extract_season_number(text)
            if season:
                meta_title = f"{meta_title} S{season:02d}"

        if not re.search(r"(s\d{1,2}e\d{1,3}|e\d{1,3})", normalized):
            episode_hint = self.__extract_episode_hint(text)
            if episode_hint:
                meta_title = f"{meta_title} {episode_hint}"

        return meta_title

    @staticmethod
    def __extract_season_number(text: str) -> Optional[int]:
        patterns = [
            r"第\s*([0-9]{1,2})\s*季",
            r"\[\s*第([一二三四五六七八九十]{1,3})季\s*\]",
            r"\bseason\s*([0-9]{1,2})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue

            value = match.group(1)
            if value.isdigit():
                return int(value)

            chinese = {
                "一": 1,
                "二": 2,
                "三": 3,
                "四": 4,
                "五": 5,
                "六": 6,
                "七": 7,
                "八": 8,
                "九": 9,
                "十": 10,
            }
            if value in chinese:
                return chinese[value]
        return None

    @staticmethod
    def __extract_episode_hint(text: str) -> Optional[str]:
        patterns = [
            r"第\s*([0-9]{1,3})\s*[-~到至]\s*([0-9]{1,3})\s*集",
            r"第\s*([0-9]{1,3})\s*集",
            r"\[\s*第([一二三四五六七八九十]{1,3})季\s*第([0-9]{1,3})集\s*\]",
            r"第([0-9]{1,3})季第([0-9]{1,3})集",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue

            groups = match.groups()
            if len(groups) == 2:
                if groups[0].isdigit() and groups[1].isdigit():
                    first = int(groups[0])
                    second = int(groups[1])
                    if "季" in match.group(0) and first <= 20 and second <= 999:
                        return f"S{first:02d}E{second:02d}"
                    if first <= 999 and second <= 999:
                        return f"E{first:02d}-E{second:02d}"

            if len(groups) == 1 and groups[0].isdigit():
                episode = int(groups[0])
                return f"E{episode:02d}"

        return None

    def __is_complete_pack(self, title: str) -> bool:
        normalized = title.lower()
        if not COMPLETE_HINTS.search(normalized):
            return False
        if MULTI_EPISODE_HINTS.search(normalized):
            return False
        return True

    def __is_season_pack(self, candidate: Candidate) -> bool:
        if candidate.mediainfo.type != MediaType.TV:
            return False
        if candidate.meta.episode_list:
            return False

        normalized = candidate.raw_title.lower()
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

        return bool(getattr(candidate.meta, "begin_season", None))

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

    def __history_active(self, record: dict) -> bool:
        if self._cooldown_hours <= 0:
            return False
        expire_at = self.__safe_int(record.get("expire_at"))
        return expire_at > int(datetime.datetime.now().timestamp())

    def __load_history_lookup(self) -> Dict[str, dict]:
        if self._clearflag:
            return {}

        history = self.get_data("history") or []
        lookup: Dict[str, dict] = {}
        for item in history:
            group_key = item.get("group_key")
            if not group_key:
                continue

            previous = lookup.get(group_key)
            if not previous:
                lookup[group_key] = item
                continue

            prev_expire = self.__safe_int(previous.get("expire_at"))
            curr_expire = self.__safe_int(item.get("expire_at"))
            if curr_expire >= prev_expire:
                lookup[group_key] = item
        return lookup

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
                    filter=bool(item.get("filter", False)),
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
                "cooldown_hours": self._cooldown_hours,
                "skip_complete": self._skip_complete,
                "skip_tv_without_episode": self._skip_tv_without_episode,
                "sources_json": self._sources_json,
            }
        )

    @staticmethod
    def __safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

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
