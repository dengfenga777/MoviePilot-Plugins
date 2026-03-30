import copy
import datetime
import re
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.download import DownloadChain
from app.chain.mediaserver import MediaServerChain
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db.site_oper import SiteOper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotExistMediaInfo, ServiceInfo
from app.schemas.types import MediaType, SystemConfigKey

lock = Lock()
SERIES_TYPES = {"series", "show"}


class LibraryGapFill(_PluginBase):
    plugin_name = "媒体库缺集补全"
    plugin_desc = "只补媒体库缺失的单集，并优先选择下载人数高的资源。"
    plugin_icon = "search.png"
    plugin_version = "1.1"
    plugin_author = "Codex"
    author_url = "https://github.com/openai"
    plugin_config_prefix = "librarygapfill_"
    plugin_order = 21
    auth_level = 2

    _scheduler: Optional[BackgroundScheduler] = None

    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _clear: bool = False
    _clearflag: bool = False
    _cron: str = "0 4 * * *"
    _mediaservers: List[str] = []
    _sites: List[int] = []
    _include: str = ""
    _exclude: str = ""
    _save_path: str = ""
    _use_subscribe_rules: bool = True
    _skip_future_episodes: bool = True
    _cooldown_hours: int = 24

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce"))
            self._clear = bool(config.get("clear"))
            self._cron = config.get("cron") or "0 4 * * *"
            self._mediaservers = config.get("mediaservers") or []
            self._sites = config.get("sites") or []
            self._include = config.get("include") or ""
            self._exclude = config.get("exclude") or ""
            self._save_path = config.get("save_path") or ""
            self._use_subscribe_rules = bool(config.get("use_subscribe_rules", True))
            self._skip_future_episodes = bool(config.get("skip_future_episodes", True))
            self._cooldown_hours = self.__safe_int(config.get("cooldown_hours"), default=24)

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("媒体库缺集补全服务启动，立即运行一次")
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
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [
                {
                    "id": "LibraryGapFill",
                    "name": "媒体库缺集补全服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.check,
                    "kwargs": {},
                }
            ]
        elif self._enabled:
            return [
                {
                    "id": "LibraryGapFill",
                    "name": "媒体库缺集补全服务",
                    "trigger": CronTrigger.from_crontab("0 4 * * *"),
                    "func": self.check,
                    "kwargs": {},
                }
            ]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        mediaserver_items = [
            {"title": config.name, "value": config.name}
            for config in MediaServerHelper().get_configs().values()
        ]
        site_items = [
            {"title": site.name, "value": site.id}
            for site in SiteOper().list_order_by_pri()
        ]

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            _col(3, _switch("enabled", "启用插件")),
                            _col(3, _switch("notify", "发送通知")),
                            _col(3, _switch("onlyonce", "立即运行一次")),
                            _col(3, _switch("clear", "清理历史记录")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(4, _cronfield("cron", "执行周期", "5位 cron 表达式")),
                            _col(4, _textfield("cooldown_hours", "去重冷却(小时)", "默认 24")),
                            _col(4, _textfield("save_path", "保存目录", "留空使用下载器默认目录")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(
                                12,
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "model": "mediaservers",
                                        "label": "媒体服务器",
                                        "items": mediaserver_items,
                                    },
                                },
                            )
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(
                                12,
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "model": "sites",
                                        "label": "搜索站点",
                                        "items": site_items,
                                        "placeholder": "留空使用 MoviePilot 的订阅站点范围",
                                    },
                                },
                            )
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _textfield("include", "包含", "仅扫描匹配该正则的剧集标题")),
                            _col(6, _textfield("exclude", "排除", "扫描时跳过匹配该正则的剧集标题")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _switch("use_subscribe_rules", "使用订阅优先级规则")),
                            _col(6, _switch("skip_future_episodes", "只补已播出剧集")),
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
                                                "本插件不会看订阅缺失，而是直接扫描媒体库里的电视剧。"
                                                "它会按 TMDB 判断每一季实际缺了哪些已播出剧集，"
                                                "只按单集逐个搜索，并优先选择下载人数更高的资源。"
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
            "notify": True,
            "onlyonce": False,
            "clear": False,
            "cron": "0 4 * * *",
            "mediaservers": [],
            "sites": [],
            "include": "",
            "exclude": "",
            "save_path": "",
            "use_subscribe_rules": True,
            "skip_future_episodes": True,
            "cooldown_hours": 24,
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

        rows = []
        for item in sorted(historys, key=lambda x: x.get("time", ""), reverse=True):
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": item.get("title", "")},
                        {"component": "td", "text": item.get("server", "")},
                        {"component": "td", "text": item.get("missing", "")},
                        {"component": "td", "text": item.get("downloaded", "")},
                        {"component": "td", "text": item.get("remaining", "")},
                        {"component": "td", "text": item.get("status", "")},
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
                                    {"component": "th", "text": "标题"},
                                    {"component": "th", "text": "服务器"},
                                    {"component": "th", "text": "本轮缺失"},
                                    {"component": "th", "text": "已推送"},
                                    {"component": "th", "text": "剩余"},
                                    {"component": "th", "text": "状态"},
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

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "clear": self._clear,
                "cron": self._cron,
                "mediaservers": self._mediaservers,
                "sites": self._sites,
                "include": self._include,
                "exclude": self._exclude,
                "save_path": self._save_path,
                "use_subscribe_rules": self._use_subscribe_rules,
                "skip_future_episodes": self._skip_future_episodes,
                "cooldown_hours": self._cooldown_hours,
            }
        )

    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        if not self._mediaservers:
            logger.warning("尚未配置媒体服务器，请检查插件配置")
            return None

        services = MediaServerHelper().get_services(name_filters=self._mediaservers)
        if not services:
            logger.warning("获取媒体服务器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning("媒体服务器 %s 未连接，请检查配置", service_name)
                continue
            active_services[service_name] = service_info

        if not active_services:
            logger.warning("没有已连接的媒体服务器，请检查配置")
            return None
        return active_services

    def check(self):
        with lock:
            old_history = [] if self._clearflag else (self.get_data("history") or [])
            recent_requests = {} if self._clearflag else (self.get_data("recent_requests") or {})
            recent_requests = self.__cleanup_recent_requests(recent_requests)

            services = self.service_infos()
            if not services:
                if self._clearflag:
                    self.save_data("history", [])
                    self.save_data("recent_requests", {})
                    self._clearflag = False
                return

            rule_groups = self.systemconfig.get(
                SystemConfigKey.SubscribeFilterRuleGroups
                if self._use_subscribe_rules
                else SystemConfigKey.SearchFilterRuleGroups
            )
            search_sites = self.__get_search_sites()

            history_records = []
            scanned_series = 0
            missing_series = 0
            downloaded_episodes = 0
            mediaserverchain = MediaServerChain()
            searchchain = SearchChain()
            downloadchain = DownloadChain()
            season_cache: Dict[Tuple[Any, int], List[int]] = {}

            for server in sorted(services.keys()):
                libraries = mediaserverchain.librarys(server) or []
                logger.info("开始扫描媒体服务器 %s，共 %s 个媒体库", server, len(libraries))
                for library in libraries:
                    logger.info("开始扫描媒体库 %s/%s", server, library.name)
                    for item in mediaserverchain.items(server, library.id):
                        if not item or not item.item_id or not item.title:
                            continue
                        if str(item.item_type or "").lower() not in SERIES_TYPES:
                            continue
                        if not self.__match_item(item):
                            continue

                        scanned_series += 1
                        record = self.__process_series_item(
                            server=server,
                            library_name=library.name,
                            item=item,
                            mediaserverchain=mediaserverchain,
                            searchchain=searchchain,
                            downloadchain=downloadchain,
                            rule_groups=rule_groups,
                            search_sites=search_sites,
                            recent_requests=recent_requests,
                            season_cache=season_cache,
                        )
                        if not record:
                            continue

                        missing_series += 1
                        downloaded_episodes += record.get("downloaded_count", 0)
                        history_records.append(record)

            merged_history = (history_records + old_history)[:200]
            self.save_data("history", merged_history)
            self.save_data("recent_requests", recent_requests)

            logger.info(
                "媒体库缺集补全本轮完成：扫描剧集 %s 部，发现缺集 %s 部，已推送 %s 集",
                scanned_series,
                missing_series,
                downloaded_episodes,
            )

            if self._notify and history_records:
                self.__post_summary(
                    scanned_series=scanned_series,
                    missing_series=missing_series,
                    downloaded_episodes=downloaded_episodes,
                    history_records=history_records,
                )

            self._clearflag = False

    def __process_series_item(
        self,
        server: str,
        library_name: str,
        item,
        mediaserverchain: MediaServerChain,
        searchchain: SearchChain,
        downloadchain: DownloadChain,
        rule_groups: List[str],
        search_sites: List[int],
        recent_requests: Dict[str, dict],
        season_cache: Dict[Tuple[Any, int], List[int]],
    ) -> Optional[dict]:
        mediainfo = self.__recognize_series(item)
        if not mediainfo or mediainfo.type != MediaType.TV:
            return None

        targets = self.__collect_missing_targets(
            server=server,
            item=item,
            mediainfo=mediainfo,
            mediaserverchain=mediaserverchain,
            recent_requests=recent_requests,
            season_cache=season_cache,
        )
        if not targets:
            return None

        missing_map = {
            season: sorted(set(data.get("missing") or []))
            for season, data in targets.items()
            if data.get("missing")
        }
        if not missing_map:
            return None

        missing_text = self.__format_episode_map(missing_map)
        logger.info("%s %s 缺失：%s", server, mediainfo.title_year, missing_text)

        downloaded_map, remaining_map, matched_any = self.__download_single_episode_targets(
            mediainfo=mediainfo,
            missing_map=missing_map,
            searchchain=searchchain,
            downloadchain=downloadchain,
            rule_groups=rule_groups,
            search_sites=search_sites,
            recent_requests=recent_requests,
        )
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        downloaded_text = self.__format_episode_map(downloaded_map)
        remaining_text = self.__format_episode_map(remaining_map)
        downloaded_count = sum(len(episodes) for episodes in downloaded_map.values())

        if downloaded_map and not remaining_map:
            status = "已推送"
        elif downloaded_map:
            status = "部分推送"
        elif matched_any:
            status = "匹配到资源但未下载"
        else:
            status = "未找到资源"

        return {
            "title": mediainfo.title_year,
            "server": server,
            "library": library_name,
            "missing": missing_text,
            "downloaded": downloaded_text,
            "remaining": remaining_text,
            "status": status,
            "time": now,
            "downloaded_count": downloaded_count,
        }

    def __download_single_episode_targets(
        self,
        mediainfo: MediaInfo,
        missing_map: Dict[int, List[int]],
        searchchain: SearchChain,
        downloadchain: DownloadChain,
        rule_groups: List[str],
        search_sites: List[int],
        recent_requests: Dict[str, dict],
    ) -> Tuple[Dict[int, List[int]], Dict[int, List[int]], bool]:
        media_key = mediainfo.tmdb_id or mediainfo.douban_id
        downloaded_map: Dict[int, List[int]] = {}
        remaining_map: Dict[int, List[int]] = {
            season: list(sorted(set(episodes)))
            for season, episodes in missing_map.items()
            if episodes
        }
        matched_any = False

        if not media_key:
            return downloaded_map, remaining_map, matched_any

        for season in sorted(missing_map.keys()):
            for episode in sorted(set(missing_map.get(season) or [])):
                fresh_mediainfo = self.__fresh_mediainfo(mediainfo=mediainfo, season=season)
                no_exists = {
                    media_key: {
                        season: NotExistMediaInfo(
                            season=season,
                            episodes=[episode],
                            total_episode=1,
                            start_episode=episode,
                        )
                    }
                }
                contexts = searchchain.process(
                    mediainfo=fresh_mediainfo,
                    no_exists=no_exists,
                    sites=search_sites,
                    rule_groups=rule_groups,
                )
                contexts = self.__filter_single_episode_contexts(
                    contexts=contexts,
                    season=season,
                    episode=episode,
                )
                if not contexts:
                    continue

                matched_any = True
                contexts = self.__sort_contexts_by_people(contexts)

                for context in contexts:
                    torrent = context.torrent_info
                    logger.info(
                        "尝试下载单集 %s S%02dE%02d：%s | 下载=%s 做种=%s 完成=%s",
                        mediainfo.title_year,
                        season,
                        episode,
                        torrent.title,
                        self.__safe_int(getattr(torrent, "peers", 0)),
                        self.__safe_int(getattr(torrent, "seeders", 0)),
                        self.__safe_int(getattr(torrent, "grabs", 0)),
                    )
                    download_id = downloadchain.download_single(
                        context=context,
                        save_path=self._save_path,
                        source="媒体库缺集补全",
                        username="媒体库缺集补全",
                    )
                    if not download_id:
                        continue

                    downloaded_map.setdefault(season, []).append(episode)
                    if episode in remaining_map.get(season, []):
                        remaining_map[season].remove(episode)
                        if not remaining_map[season]:
                            remaining_map.pop(season, None)
                    self.__register_recent_requests(
                        recent_requests=recent_requests,
                        mediainfo=mediainfo,
                        downloaded_map={season: [episode]},
                    )
                    break

        return downloaded_map, remaining_map, matched_any

    def __fresh_mediainfo(self, mediainfo: MediaInfo, season: int) -> MediaInfo:
        fresh_mediainfo = None
        if mediainfo.tmdb_id:
            fresh_mediainfo = self.chain.recognize_media(
                mtype=mediainfo.type,
                tmdbid=mediainfo.tmdb_id,
                cache=True,
            )
        elif mediainfo.douban_id:
            fresh_mediainfo = self.chain.recognize_media(
                mtype=mediainfo.type,
                doubanid=mediainfo.douban_id,
                cache=True,
            )

        if not fresh_mediainfo:
            fresh_mediainfo = copy.deepcopy(mediainfo)
        fresh_mediainfo.season = season
        return fresh_mediainfo

    def __filter_single_episode_contexts(
        self,
        contexts: List[Any],
        season: int,
        episode: int,
    ) -> List[Any]:
        results = []
        for context in contexts or []:
            meta = context.meta_info
            if not meta:
                continue

            seasons = list(meta.season_list or [])
            if not seasons:
                seasons = [1]
            if len(seasons) != 1 or seasons[0] != season:
                continue

            episodes = sorted(
                {
                    self.__safe_int(ep)
                    for ep in (meta.episode_list or [])
                    if self.__safe_int(ep) > 0
                }
            )
            if episodes != [episode]:
                continue
            results.append(context)
        return results

    def __sort_contexts_by_people(self, contexts: List[Any]) -> List[Any]:
        return sorted(
            contexts,
            key=self.__context_people_sort_key,
            reverse=True,
        )

    def __context_people_sort_key(self, context: Any) -> Tuple[int, int, int, int, int]:
        torrent = context.torrent_info
        return (
            self.__safe_int(getattr(torrent, "peers", 0)),
            self.__safe_int(getattr(torrent, "seeders", 0)),
            self.__safe_int(getattr(torrent, "grabs", 0)),
            self.__safe_int(getattr(torrent, "pri_order", 0)),
            self.__safe_int(getattr(torrent, "size", 0)),
        )

    def __collect_missing_targets(
        self,
        server: str,
        item,
        mediainfo: MediaInfo,
        mediaserverchain: MediaServerChain,
        recent_requests: Dict[str, dict],
        season_cache: Dict[Tuple[Any, int], List[int]],
    ) -> Dict[int, dict]:
        existing_map = {}
        for season_info in mediaserverchain.episodes(server, item.item_id) or []:
            season = self.__safe_int(getattr(season_info, "season", None))
            if season <= 0:
                continue
            episodes = sorted(
                {
                    self.__safe_int(episode)
                    for episode in (getattr(season_info, "episodes", None) or [])
                    if self.__safe_int(episode) > 0
                }
            )
            existing_map[season] = episodes

        targets = {}
        for season in self.__candidate_seasons(mediainfo):
            expected = self.__get_expected_episodes(
                mediainfo=mediainfo,
                season=season,
                season_cache=season_cache,
            )
            if not expected:
                continue

            existing = existing_map.get(season, [])
            missing = [episode for episode in expected if episode not in set(existing)]
            if not missing:
                continue

            cooled_missing = self.__apply_recent_requests(
                mediainfo=mediainfo,
                season=season,
                missing=missing,
                recent_requests=recent_requests,
            )
            if not cooled_missing:
                logger.info(
                    "%s S%02d 缺集最近已经推送过，冷却中，先跳过",
                    mediainfo.title_year,
                    season,
                )
                continue

            targets[season] = {
                "expected": expected,
                "existing": existing,
                "missing": cooled_missing,
            }
        return targets

    def __candidate_seasons(self, mediainfo: MediaInfo) -> List[int]:
        seasons = {
            self.__safe_int(season)
            for season in (mediainfo.seasons or {}).keys()
            if self.__safe_int(season) > 0
        }
        if not seasons and mediainfo.number_of_seasons:
            seasons = set(range(1, int(mediainfo.number_of_seasons) + 1))
        return sorted(season for season in seasons if season > 0)

    def __get_expected_episodes(
        self,
        mediainfo: MediaInfo,
        season: int,
        season_cache: Dict[Tuple[Any, int], List[int]],
    ) -> List[int]:
        media_key = self.__media_key(mediainfo)
        cache_key = (media_key, season)
        if cache_key in season_cache:
            return season_cache[cache_key]

        fallback_episodes = sorted(
            {
                self.__safe_int(episode)
                for episode in (mediainfo.seasons.get(season) or [])
                if self.__safe_int(episode) > 0
            }
        )
        if not mediainfo.tmdb_id:
            result = fallback_episodes
            season_cache[cache_key] = result
            return result

        result: List[int] = []
        finished_show = self.__is_finished_show(mediainfo)
        today = datetime.date.today()
        try:
            season_info = self.chain.tmdb_info(
                tmdbid=mediainfo.tmdb_id,
                mtype=mediainfo.type,
                season=season,
            )
        except Exception as err:
            logger.warning(
                "%s 获取 TMDB 季信息失败：S%02d - %s",
                mediainfo.title_year,
                season,
                err,
            )
            season_info = None

        if season_info and isinstance(season_info, dict):
            episodes = season_info.get("episodes") or []
            for episode in episodes:
                episode_number = self.__safe_int(episode.get("episode_number"))
                if episode_number <= 0:
                    continue
                if not self._skip_future_episodes:
                    result.append(episode_number)
                    continue

                air_date = self.__parse_date(episode.get("air_date"))
                if air_date and air_date <= today:
                    result.append(episode_number)
                    continue
                if not air_date and finished_show:
                    result.append(episode_number)

        if not result:
            if not self._skip_future_episodes or finished_show:
                result = fallback_episodes
            else:
                season_meta = self.__find_season_meta(mediainfo, season)
                season_air_date = self.__parse_date(season_meta.get("air_date") if season_meta else None)
                if season_air_date and season_air_date > today:
                    result = []

        season_cache[cache_key] = sorted(set(result))
        return season_cache[cache_key]

    def __register_recent_requests(
        self,
        recent_requests: Dict[str, dict],
        mediainfo: MediaInfo,
        downloaded_map: Dict[int, List[int]],
    ):
        if self._cooldown_hours <= 0:
            return

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        media_key = self.__media_key(mediainfo)
        for season, episodes in downloaded_map.items():
            if not episodes:
                continue
            key = f"{media_key}:S{season:02d}"
            existing = recent_requests.get(key) or {}
            merged = {
                self.__safe_int(episode)
                for episode in (existing.get("episodes") or [])
                if self.__safe_int(episode) > 0
            }
            merged.update(
                self.__safe_int(episode)
                for episode in episodes
                if self.__safe_int(episode) > 0
            )
            recent_requests[key] = {
                "title": mediainfo.title_year,
                "season": season,
                "episodes": sorted(merged),
                "time": now,
            }

    def __cleanup_recent_requests(self, recent_requests: Dict[str, dict]) -> Dict[str, dict]:
        if self._cooldown_hours <= 0:
            return {}

        cleaned = {}
        now = datetime.datetime.now()
        for key, value in (recent_requests or {}).items():
            if not isinstance(value, dict):
                continue
            record_time = self.__parse_datetime(value.get("time"))
            if not record_time:
                continue
            if (now - record_time).total_seconds() > self._cooldown_hours * 3600:
                continue
            cleaned[key] = value
        return cleaned

    def __apply_recent_requests(
        self,
        mediainfo: MediaInfo,
        season: int,
        missing: List[int],
        recent_requests: Dict[str, dict],
    ) -> List[int]:
        if self._cooldown_hours <= 0:
            return missing

        key = f"{self.__media_key(mediainfo)}:S{season:02d}"
        recent = recent_requests.get(key) or {}
        recent_episodes = {
            self.__safe_int(episode)
            for episode in (recent.get("episodes") or [])
            if self.__safe_int(episode) > 0
        }
        if not recent_episodes:
            return missing
        return [episode for episode in missing if episode not in recent_episodes]

    def __recognize_series(self, item) -> Optional[MediaInfo]:
        mediainfo = None
        if getattr(item, "tmdbid", None):
            mediainfo = self.chain.recognize_media(
                mtype=MediaType.TV,
                tmdbid=item.tmdbid,
                cache=True,
            )

        if not mediainfo:
            meta = MetaInfo(title=f"{item.title} {item.year or ''}".strip())
            meta.type = MediaType.TV
            mediainfo = self.chain.recognize_media(meta=meta, cache=True)

        if not mediainfo:
            logger.warning("%s 未识别到媒体信息，已跳过", item.title)
            return None
        return mediainfo

    def __match_item(self, item) -> bool:
        text = " ".join(
            str(part).strip()
            for part in [item.title, getattr(item, "original_title", None), getattr(item, "year", None)]
            if part
        )
        if self._include:
            try:
                if not re.search(self._include, text, re.IGNORECASE):
                    return False
            except re.error as err:
                logger.error("包含正则配置错误：%s", err)
                return False
        if self._exclude:
            try:
                if re.search(self._exclude, text, re.IGNORECASE):
                    return False
            except re.error as err:
                logger.error("排除正则配置错误：%s", err)
                return False
        return True

    def __get_search_sites(self) -> List[int]:
        if self._sites:
            return self._sites
        return self.systemconfig.get(SystemConfigKey.RssSites)

    def __post_summary(
        self,
        scanned_series: int,
        missing_series: int,
        downloaded_episodes: int,
        history_records: List[dict],
    ):
        details = []
        for item in history_records[:8]:
            summary = item.get("downloaded") or item.get("remaining") or item.get("missing")
            details.append(f"{item.get('title')} | {item.get('status')} | {summary}")

        text = "\n".join(
            [
                f"扫描剧集：{scanned_series}",
                f"发现缺集：{missing_series}",
                f"已推送剧集：{downloaded_episodes}",
                "",
                *details,
            ]
        ).strip()
        self.post_message(
            mtype=schemas.NotificationType.Manual,
            title="【媒体库缺集补全】",
            text=text,
        )

    @staticmethod
    def __find_season_meta(mediainfo: MediaInfo, season: int) -> Optional[dict]:
        for season_info in mediainfo.season_info or []:
            if int(season_info.get("season_number") or 0) == int(season):
                return season_info
        return None

    @staticmethod
    def __is_finished_show(mediainfo: MediaInfo) -> bool:
        return str(mediainfo.status or "").lower() in {"ended", "canceled", "cancelled"}

    @staticmethod
    def __media_key(mediainfo: MediaInfo) -> str:
        return str(mediainfo.tmdb_id or mediainfo.douban_id or f"{mediainfo.title}_{mediainfo.year}")

    @staticmethod
    def __safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __parse_date(value: Optional[str]) -> Optional[datetime.date]:
        if not value:
            return None
        try:
            return datetime.datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def __parse_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
        if not value:
            return None
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.datetime.strptime(str(value), pattern)
            except ValueError:
                continue
        try:
            return datetime.datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def __format_episode_map(self, episode_map: Dict[int, List[int]]) -> str:
        if not episode_map:
            return ""
        parts = []
        for season in sorted(episode_map.keys()):
            episodes = sorted(
                {
                    self.__safe_int(episode)
                    for episode in episode_map.get(season) or []
                    if self.__safe_int(episode) > 0
                }
            )
            if not episodes:
                continue
            parts.append(f"S{season:02d}{self.__format_episode_ranges(episodes)}")
        return ", ".join(parts)

    @staticmethod
    def __format_episode_ranges(episodes: List[int]) -> str:
        if not episodes:
            return ""

        ranges = []
        start = episodes[0]
        end = episodes[0]
        for episode in episodes[1:]:
            if episode == end + 1:
                end = episode
                continue
            ranges.append((start, end))
            start = end = episode
        ranges.append((start, end))

        formatted = []
        for start, end in ranges:
            if start == end:
                formatted.append(f"E{start:02d}")
            else:
                formatted.append(f"E{start:02d}-E{end:02d}")
        return ",".join(formatted)


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


def _cronfield(model: str, label: str, placeholder: str = "") -> dict:
    props = {"model": model, "label": label}
    if placeholder:
        props["placeholder"] = placeholder
    return {"component": "VCronField", "props": props}
