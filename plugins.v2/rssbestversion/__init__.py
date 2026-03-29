import datetime
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.download import DownloadChain
from app.core.config import settings
from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ExistMediaInfo
from app.schemas.types import SystemConfigKey, MediaType

lock = Lock()
COMPLETE_HINTS = re.compile(
    r"(complete|全集|全季|season\s*\d+\s*complete|s\d+\s*complete|fin(al|ale)|完结|完結)",
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
class Candidate:
    raw_title: str
    description: str
    torrent: TorrentInfo
    meta: MetaInfo
    mediainfo: MediaInfo
    source_url: str
    site_name: str
    group_keys: List[str]
    quality_label: str
    quality_score: int
    site_score: int
    codec_score: int
    size_score: int
    pubdate_score: int

    @property
    def candidate_id(self) -> str:
        if self.torrent.enclosure:
            return self.torrent.enclosure
        if self.torrent.page_url:
            return self.torrent.page_url
        return self.raw_title

    @property
    def sort_tuple(self) -> Tuple[int, int, int, int, int]:
        return (
            self.quality_score,
            self.site_score,
            self.codec_score,
            self.size_score,
            self.pubdate_score,
        )


class RssBestVersion(_PluginBase):
    plugin_name = "RSS优选下载"
    plugin_desc = "识别同一剧集的多个版本，只保留优先级最高的资源下发下载。"
    plugin_icon = "rss.png"
    plugin_version = "1.4"
    plugin_author = "Codex"
    author_url = "https://github.com/openai"
    plugin_config_prefix = "rssbestversion_"
    plugin_order = 20
    auth_level = 2

    _scheduler: Optional[BackgroundScheduler] = None
    _cache_path: Optional[Path] = None

    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = "*/30 * * * *"
    _address: str = ""
    _include: str = ""
    _exclude: str = ""
    _proxy: bool = False
    _filter: bool = False
    _clear: bool = False
    _clearflag: bool = False
    _save_path: str = ""
    _size_range: str = ""
    _prefer_hevc: bool = True
    _quality_order: str = "2160p,1080p,720p,other"
    _skip_complete: bool = True
    _site_priority: str = ""

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self.__validate_and_fix_config(config=config)
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce"))
            self._cron = config.get("cron") or "*/30 * * * *"
            self._address = config.get("address") or ""
            self._include = config.get("include") or ""
            self._exclude = config.get("exclude") or ""
            self._proxy = bool(config.get("proxy"))
            self._filter = bool(config.get("filter"))
            self._clear = bool(config.get("clear"))
            self._save_path = config.get("save_path") or ""
            self._size_range = config.get("size_range") or ""
            self._prefer_hevc = bool(config.get("prefer_hevc", True))
            self._quality_order = config.get("quality_order") or "2160p,1080p,720p,other"
            self._skip_complete = bool(config.get("skip_complete", True))
            self._site_priority = config.get("site_priority") or ""

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("RSS优选下载服务启动，立即运行一次")
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
                "summary": "删除 RSS 优选下载历史记录",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [
                {
                    "id": "RssBestVersion",
                    "name": "RSS优选下载服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.check,
                    "kwargs": {},
                }
            ]
        elif self._enabled:
            return [
                {
                    "id": "RssBestVersion",
                    "name": "RSS优选下载服务",
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
                            _col(3, _switch("enabled", "启用插件")),
                            _col(3, _switch("notify", "发送通知")),
                            _col(3, _switch("onlyonce", "立即运行一次")),
                            _col(3, _switch("clear", "清理历史记录")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _cronfield("cron", "执行周期", "5位 cron 表达式，留空自动")),
                            _col(6, _textfield("save_path", "保存目录", "留空使用下载器默认目录")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(
                                12,
                                _textarea(
                                    "address",
                                    "RSS地址",
                                    "每行一个 RSS 地址，可直接填写站点 RSS 链接",
                                    rows=4,
                                ),
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
                            _col(4, _textfield("size_range", "种子大小(GB)", "如：3 或 3-20")),
                            _col(
                                4,
                                _textfield(
                                    "quality_order",
                                    "清晰度优先级",
                                    "默认 2160p,1080p,720p,other",
                                ),
                            ),
                            _col(2, _switch("proxy", "使用代理服务器")),
                            _col(2, _switch("filter", "使用订阅优先级规则")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(4, _switch("prefer_hevc", "同分辨率优先 HEVC/H265")),
                            _col(4, _switch("skip_complete", "过滤整季/完结包")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(
                                12,
                                _textarea(
                                    "site_priority",
                                    "站点优先级",
                                    "每行一个：pt1.com=100\\npt2.com=80\\n未配置的站点默认 0",
                                    rows=4,
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
                                                "本插件会先用 MoviePilot 识别 RSS 条目的 TMDB、季、集，"
                                                "再把同一剧集的多个版本放在一起比较，只下载优先级最高的一个。"
                                                "例如同一集同时出现 4K 和 1080p 时，只会推送 4K。"
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
            "cron": "*/30 * * * *",
            "address": "",
            "include": "",
            "exclude": "",
            "proxy": False,
            "filter": False,
            "save_path": "",
            "size_range": "",
            "prefer_hevc": True,
            "quality_order": "2160p,1080p,720p,other",
            "skip_complete": True,
            "site_priority": "",
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
        rows = []
        for item in historys:
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": item.get("title", "")},
                        {"component": "td", "text": item.get("season_episode", "")},
                        {"component": "td", "text": item.get("quality", "")},
                        {"component": "td", "text": item.get("site", "")},
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
                                    {"component": "th", "text": "季集"},
                                    {"component": "th", "text": "采用版本"},
                                    {"component": "th", "text": "来源"},
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

        historys = [h for h in historys if h.get("group_key") != key]
        self.save_data("history", historys)
        return schemas.Response(success=True, message="删除成功")

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "clear": self._clear,
                "cron": self._cron,
                "address": self._address,
                "include": self._include,
                "exclude": self._exclude,
                "proxy": self._proxy,
                "filter": self._filter,
                "save_path": self._save_path,
                "size_range": self._size_range,
                "prefer_hevc": self._prefer_hevc,
                "quality_order": self._quality_order,
                "skip_complete": self._skip_complete,
                "site_priority": self._site_priority,
            }
        )

    def check(self):
        if not self._address:
            return

        if self._clearflag:
            history = []
        else:
            history = self.get_data("history") or []

        history_lookup = {item.get("group_key"): item for item in history if item.get("group_key")}
        filter_groups = self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
        candidates: List[Candidate] = []

        for url in [line.strip() for line in self._address.splitlines() if line.strip()]:
            logger.info("开始刷新 RSS：%s ...", url)
            results = RssHelper().parse(url, proxy=self._proxy)
            if not results:
                logger.error("未获取到 RSS 数据：%s", url)
                continue

            for result in results:
                try:
                    candidate = self.__build_candidate(
                        result=result,
                        source_url=url,
                        filter_groups=filter_groups,
                        history_lookup=history_lookup,
                    )
                    if candidate:
                        candidates.append(candidate)
                except Exception as err:
                    logger.error("解析 RSS 条目出错：%s - %s", str(err), traceback.format_exc())

            logger.info("RSS %s 刷新完成，候选资源累计 %s 条", url, len(candidates))

        if not candidates:
            logger.info("本轮 RSS 未产生可下载候选资源")
            self._clearflag = False
            return

        chosen_map = self.__pick_best_candidates(candidates=candidates)
        chosen_candidates = self.__dedupe_candidates(chosen_map)
        if not chosen_candidates:
            logger.info("本轮 RSS 无需下载的优选资源")
            self._clearflag = False
            return

        for candidate in chosen_candidates:
            downloaded_keys = []
            for key in candidate.group_keys:
                previous = history_lookup.get(key)
                if not previous:
                    downloaded_keys.append(key)
                    continue
                previous_size = self.__safe_int(previous.get("size"))
                if candidate.size_score > previous_size:
                    downloaded_keys.append(key)
            if not downloaded_keys:
                continue

            logger.info(
                "优选资源：%s | 质量=%s | 组=%s",
                candidate.raw_title,
                candidate.quality_label,
                downloaded_keys,
            )
            success = self.__download_candidate(candidate)
            if not success:
                logger.error("下载失败：%s", candidate.raw_title)
                continue

            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            season_episode = self.__season_episode_text(candidate.meta)
            site = self.__site_name(candidate.source_url)

            for group_key in downloaded_keys:
                history_lookup[group_key] = {
                    "title": candidate.mediainfo.title_year,
                    "season_episode": season_episode,
                    "group_key": group_key,
                    "quality": candidate.quality_label,
                    "site": site,
                    "raw_title": candidate.raw_title,
                    "poster": candidate.mediainfo.get_poster_image(),
                    "tmdbid": candidate.mediainfo.tmdb_id,
                    "size": candidate.size_score,
                    "time": now,
                }

        self.save_data("history", list(history_lookup.values()))
        self._clearflag = False

    def __build_candidate(
        self,
        result: dict,
        source_url: str,
        filter_groups: Any,
        history_lookup: Dict[str, dict],
    ) -> Optional[Candidate]:
        title = result.get("title")
        description = result.get("description")
        enclosure = result.get("enclosure")
        link = result.get("link")
        size = result.get("size")
        pubdate = result.get("pubdate")

        if not title:
            return None
        if self._include and not re.search(self._include, f"{title} {description}", re.IGNORECASE):
            logger.info("%s 不符合包含规则", title)
            return None
        if self._exclude and re.search(self._exclude, f"{title} {description}", re.IGNORECASE):
            logger.info("%s 命中排除规则", title)
            return None
        if self._skip_complete and self.__should_skip_complete_pack(title=title, description=description):
            logger.info("%s 命中整季/完结包过滤规则，已跳过", title)
            return None
        if self._size_range and not self.__match_size_range(size):
            logger.info("%s - 种子大小不在指定范围", title)
            return None

        meta = MetaInfo(title=title, subtitle=description)
        if not meta.name:
            logger.warning("%s 未识别到有效数据", title)
            return None

        mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
        if not mediainfo:
            logger.warning("未识别到媒体信息，标题：%s", title)
            return None

        torrent = TorrentInfo(
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
                torrent_list=[torrent],
                mediainfo=mediainfo,
            )
            if not filtered:
                logger.info("%s 不匹配订阅优先级规则", title)
                return None

        exist_info: Optional[ExistMediaInfo] = self.chain.media_exists(mediainfo=mediainfo)
        group_keys = self.__build_group_keys(mediainfo=mediainfo, meta=meta)
        size_score = self.__safe_int(size)
        site_name = self.__site_name(source_url)
        if not group_keys:
            logger.info("%s 未识别到可比较的剧集键", title)
            return None

        if mediainfo.type == MediaType.TV:
            if exist_info and self.__all_episodes_exist(meta=meta, exist_info=exist_info):
                logger.info("%s %s 已存在", mediainfo.title_year, meta.season_episode)
                return None
            remaining_keys = []
            for key in group_keys:
                previous = history_lookup.get(key)
                if not previous:
                    remaining_keys.append(key)
                    continue
                previous_size = self.__safe_int(previous.get("size"))
                if size_score > previous_size:
                    logger.info(
                        "%s %s 检测到更大版本，允许再次推送：%s > %s",
                        mediainfo.title_year,
                        meta.season_episode,
                        size_score,
                        previous_size,
                    )
                    remaining_keys.append(key)
            if not remaining_keys:
                logger.info("%s %s 已在下载历史中，且未发现更大体积版本", mediainfo.title_year, meta.season_episode)
                return None
            group_keys = remaining_keys
        elif exist_info:
            logger.info("%s 已存在", mediainfo.title_year)
            return None

        quality_label, quality_score = self.__quality_rank(f"{title} {description or ''}")
        site_score = self.__site_priority_score(site_name)
        codec_score = self.__codec_rank(f"{title} {description or ''}")
        pubdate_score = int(pubdate.timestamp()) if pubdate else 0

        return Candidate(
            raw_title=title,
            description=description or "",
            torrent=torrent,
            meta=meta,
            mediainfo=mediainfo,
            source_url=source_url,
            site_name=site_name,
            group_keys=group_keys,
            quality_label=quality_label,
            quality_score=quality_score,
            site_score=site_score,
            codec_score=codec_score,
            size_score=size_score,
            pubdate_score=pubdate_score,
        )

    def __pick_best_candidates(self, candidates: List[Candidate]) -> Dict[str, Candidate]:
        best_for_key: Dict[str, Candidate] = {}

        for candidate in candidates:
            for group_key in candidate.group_keys:
                current = best_for_key.get(group_key)
                if not current or candidate.sort_tuple > current.sort_tuple:
                    best_for_key[group_key] = candidate
                    if current and current.raw_title != candidate.raw_title:
                        logger.info(
                            "同一剧集版本替换：%s -> %s | key=%s | 质量=%s>%s | 站点=%s(%s)",
                            current.raw_title,
                            candidate.raw_title,
                            group_key,
                            candidate.quality_label,
                            current.quality_label,
                            candidate.site_name,
                            candidate.site_score,
                        )
        return best_for_key

    @staticmethod
    def __dedupe_candidates(chosen_map: Dict[str, Candidate]) -> List[Candidate]:
        chosen: Dict[str, Candidate] = {}
        for candidate in chosen_map.values():
            chosen[candidate.candidate_id] = candidate
        return list(chosen.values())

    def __download_candidate(self, candidate: Candidate) -> bool:
        result = DownloadChain().download_single(
            context=Context(
                meta_info=candidate.meta,
                media_info=candidate.mediainfo,
                torrent_info=candidate.torrent,
            ),
            save_path=self._save_path,
            username="RSS优选下载",
        )
        return bool(result)

    def __build_group_keys(self, mediainfo: MediaInfo, meta: MetaInfo) -> List[str]:
        tmdbid = mediainfo.tmdb_id or f"{mediainfo.title}_{mediainfo.year}"
        if mediainfo.type == MediaType.TV:
            season = meta.begin_season or 1
            episodes = meta.episode_list or []
            if episodes:
                return [f"tv:{tmdbid}:s{season}:e{episode}" for episode in sorted(set(episodes))]
            return [f"tv:{tmdbid}:s{season}:{meta.season_episode or meta.name}"]
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

    def __quality_rank(self, text: str) -> Tuple[str, int]:
        normalized = text.lower()
        detected = "other"
        if re.search(r"2160p|4k|uhd", normalized):
            detected = "2160p"
        elif re.search(r"1080p|1080i", normalized):
            detected = "1080p"
        elif re.search(r"720p", normalized):
            detected = "720p"
        elif re.search(r"480p|576p", normalized):
            detected = "sd"

        order = [item.strip().lower() for item in self._quality_order.split(",") if item.strip()]
        normalized_order = []
        for item in order:
            if item in {"4k", "2160", "2160p", "uhd"}:
                normalized_order.append("2160p")
            elif item in {"1080", "1080p", "1080i"}:
                normalized_order.append("1080p")
            elif item in {"720", "720p"}:
                normalized_order.append("720p")
            elif item in {"480", "480p", "576", "576p", "sd"}:
                normalized_order.append("sd")
            else:
                normalized_order.append("other")

        if detected not in normalized_order:
            normalized_order.append(detected)
        if "other" not in normalized_order:
            normalized_order.append("other")

        score_map = {
            label: len(normalized_order) - index
            for index, label in enumerate(normalized_order)
        }
        return detected, score_map.get(detected, 0)

    def __codec_rank(self, text: str) -> int:
        if not self._prefer_hevc:
            return 0
        normalized = text.lower()
        if re.search(r"hevc|h\.?265|x265", normalized):
            return 2
        if re.search(r"h\.?264|x264|avc", normalized):
            return 1
        return 0

    @staticmethod
    def __safe_int(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def __site_priority_score(self, site_name: str) -> int:
        if not site_name:
            return 0
        rules = self.__parse_site_priority()
        site_name = site_name.lower()
        exact = rules.get(site_name)
        if exact is not None:
            return exact
        best_match = 0
        for domain, score in rules.items():
            if site_name == domain or site_name.endswith(f".{domain}"):
                best_match = max(best_match, score)
        return best_match

    def __parse_site_priority(self) -> Dict[str, int]:
        rules: Dict[str, int] = {}
        for raw_line in self._site_priority.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            domain, score = line.split("=", 1)
            domain = domain.strip().lower()
            if domain.startswith("http://") or domain.startswith("https://"):
                domain = self.__site_name(domain)
            parsed_score = self.__safe_int(score.strip())
            if domain:
                rules[domain] = parsed_score
        return rules

    def __should_skip_complete_pack(self, title: str, description: Optional[str]) -> bool:
        text = f"{title} {description or ''}"
        normalized = text.lower()
        if not COMPLETE_HINTS.search(normalized):
            return False

        # 两集或多集连发应保留，不应被误判为完结包。
        if MULTI_EPISODE_HINTS.search(normalized):
            return False

        return True

    def __match_size_range(self, size: Any) -> bool:
        sizes = [float(item) * 1024 ** 3 for item in self._size_range.split("-")]
        current = self.__safe_int(size)
        if not current:
            return False
        if len(sizes) == 1:
            return current >= sizes[0]
        return sizes[0] <= current <= sizes[1]

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

    @staticmethod
    def __site_name(url: str) -> str:
        match = re.search(r"https?://([^/]+)", url)
        if match:
            return match.group(1)
        return url

    def __log_and_notify_error(self, message: str):
        logger.error(message)
        if self._notify:
            self.systemmessage.put(message, title="RSS优选下载")

    def __validate_and_fix_config(self, config: dict = None) -> bool:
        size_range = config.get("size_range")
        if size_range and not self.__is_number_or_range(str(size_range)):
            self.__log_and_notify_error(f"RSS优选下载出错，种子大小设置错误：{size_range}")
            config["size_range"] = ""
            return False
        return True

    @staticmethod
    def __is_number_or_range(value: str) -> bool:
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
