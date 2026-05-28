import copy
import datetime
import re
import traceback
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db.site_oper import SiteOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotExistMediaInfo
from app.schemas.types import MediaType, SystemConfigKey

lock = Lock()


@dataclass
class EpisodeTarget:
    history_id: int
    title: str
    year: Optional[str]
    tmdbid: Optional[int]
    doubanid: Optional[str]
    season: int
    episode: int
    baseline_text: str
    baseline_quality: str
    baseline_tuple: Tuple[int, int, int, int]
    date: str
    image: Optional[str]

    @property
    def key(self) -> str:
        media_key = self.tmdbid or self.doubanid or f"{self.title}_{self.year or ''}"
        return f"tv:{media_key}:s{self.season}:e{self.episode}"

    @property
    def episode_text(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


@dataclass
class UpgradeCandidate:
    context: Any
    title: str
    site: str
    quality: str
    rank_tuple: Tuple[int, int, int, int, int, int, int]


class SingleEpisodeUpgrade(_PluginBase):
    plugin_name = "整理记录单集洗版"
    plugin_desc = "读取 MoviePilot 整理记录，按已入库电视剧单集调用 MP 搜索，找到更优版本后下发下载。"
    plugin_icon = "search.png"
    plugin_version = "1.0.0"
    plugin_author = "Codex"
    author_url = "https://github.com/openai"
    plugin_config_prefix = "singleepisodeupgrade_"
    plugin_order = 22
    auth_level = 2

    _scheduler: Optional[BackgroundScheduler] = None

    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _clear: bool = False
    _clearflag: bool = False
    _cron: str = "15 4 * * *"
    _scan_days: int = 14
    _scan_limit: int = 30
    _cooldown_hours: int = 72
    _sites: List[int] = []
    _include: str = ""
    _exclude: str = ""
    _save_path: str = ""
    _use_subscribe_rules: bool = True
    _prefer_hevc: bool = True
    _allow_same_quality_better: bool = True
    _allow_unknown_baseline: bool = True
    _min_upgrade_score: int = 1
    _quality_order: str = "4k_hdr,2160p,1080p,720p,other"

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce"))
            self._clear = bool(config.get("clear"))
            self._cron = config.get("cron") or "15 4 * * *"
            self._scan_days = self.__safe_int(config.get("scan_days"), 14)
            self._scan_limit = self.__safe_int(config.get("scan_limit"), 30)
            self._cooldown_hours = self.__safe_int(config.get("cooldown_hours"), 72)
            self._sites = config.get("sites") or []
            self._include = config.get("include") or ""
            self._exclude = config.get("exclude") or ""
            self._save_path = config.get("save_path") or ""
            self._use_subscribe_rules = bool(config.get("use_subscribe_rules", True))
            self._prefer_hevc = bool(config.get("prefer_hevc", True))
            self._allow_same_quality_better = bool(config.get("allow_same_quality_better", True))
            self._allow_unknown_baseline = bool(config.get("allow_unknown_baseline", True))
            self._min_upgrade_score = self.__safe_int(config.get("min_upgrade_score"), 1)
            self._quality_order = config.get("quality_order") or "4k_hdr,2160p,1080p,720p,other"

        self._scan_days = max(self._scan_days, 1)
        self._scan_limit = max(self._scan_limit, 1)
        self._cooldown_hours = max(self._cooldown_hours, 0)
        self._min_upgrade_score = max(self._min_upgrade_score, 0)

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("整理记录单集洗版服务启动，立即运行一次")
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
        if not self._enabled:
            return []
        return [
            {
                "id": "SingleEpisodeUpgrade",
                "name": "整理记录单集洗版服务",
                "trigger": CronTrigger.from_crontab(self._cron or "15 4 * * *"),
                "func": self.check,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                            _col(3, _switch("notify", "发送汇总通知")),
                            _col(3, _switch("onlyonce", "立即运行一次")),
                            _col(3, _switch("clear", "清理插件历史")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(3, _cronfield("cron", "执行周期", "5 位 cron 表达式")),
                            _col(3, _textfield("scan_days", "整理记录天数", "默认 14")),
                            _col(3, _textfield("scan_limit", "每轮最多搜索", "默认 30 集")),
                            _col(3, _textfield("cooldown_hours", "单集冷却小时", "默认 72")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _textfield("save_path", "保存目录", "留空使用下载器默认目录")),
                            _col(6, _textfield("quality_order", "质量优先级", "4k_hdr,2160p,1080p,720p,other")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _textfield("include", "标题包含正则", "留空不过滤")),
                            _col(6, _textfield("exclude", "标题排除正则", "如 CAM|TS|720p")),
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
                                        "placeholder": "留空使用 MoviePilot RSS 站点范围",
                                    },
                                },
                            )
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(4, _switch("use_subscribe_rules", "使用订阅过滤规则")),
                            _col(4, _switch("prefer_hevc", "同等级优先 H.265/HEVC")),
                            _col(4, _switch("allow_same_quality_better", "允许同质量更优洗版")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _switch("allow_unknown_baseline", "原版本未知质量也尝试搜索")),
                            _col(6, _textfield("min_upgrade_score", "最低质量提升", "0=同级可洗，1=至少升一级")),
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
                                                "插件只读取最近成功的整理记录，不扫媒体库、不读 RSS。"
                                                "它会按 tmdb_id + SxxExx 去重，每个单集调用 MoviePilot 搜索，"
                                                "只接受精确匹配同一季同一集的资源。质量判断包含 4K HDR、臻彩、60fps、高帧率、HEVC 和体积。"
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
            "cron": "15 4 * * *",
            "scan_days": 14,
            "scan_limit": 30,
            "cooldown_hours": 72,
            "sites": [],
            "include": "",
            "exclude": "",
            "save_path": "",
            "use_subscribe_rules": True,
            "prefer_hevc": True,
            "allow_same_quality_better": True,
            "allow_unknown_baseline": True,
            "min_upgrade_score": 1,
            "quality_order": "4k_hdr,2160p,1080p,720p,other",
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data("history") or []
        if not historys:
            return [
                {
                    "component": "div",
                    "text": "暂无洗版记录",
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
                        {"component": "td", "text": item.get("episode", "")},
                        {"component": "td", "text": item.get("old_quality", "")},
                        {"component": "td", "text": item.get("new_quality", "")},
                        {"component": "td", "text": item.get("site", "")},
                        {"component": "td", "text": item.get("candidate", "")},
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
                                    {"component": "th", "text": "剧集"},
                                    {"component": "th", "text": "单集"},
                                    {"component": "th", "text": "原版本"},
                                    {"component": "th", "text": "新版本"},
                                    {"component": "th", "text": "站点"},
                                    {"component": "th", "text": "资源标题"},
                                    {"component": "th", "text": "状态"},
                                    {"component": "th", "text": "时间"},
                                ],
                            }
                        ],
                    },
                    {"component": "tbody", "content": rows[:200]},
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

    def check(self):
        with lock:
            old_history = [] if self._clearflag else (self.get_data("history") or [])
            recent_checks = {} if self._clearflag else (self.get_data("recent_checks") or {})
            recent_checks = self.__cleanup_recent_checks(recent_checks)

            targets = self.__load_targets(recent_checks=recent_checks)
            if not targets:
                self.save_data("recent_checks", recent_checks)
                if self._clearflag:
                    self.save_data("history", [])
                    self._clearflag = False
                logger.info("整理记录单集洗版未发现需要搜索的单集")
                return

            logger.info("整理记录单集洗版本轮准备搜索 %s 个单集", len(targets))
            searchchain = SearchChain()
            downloadchain = DownloadChain()
            rule_groups = self.systemconfig.get(
                SystemConfigKey.SubscribeFilterRuleGroups
                if self._use_subscribe_rules
                else SystemConfigKey.SearchFilterRuleGroups
            )
            search_sites = self.__get_search_sites()

            new_history: List[dict] = []
            searched_count = 0
            upgraded_count = 0

            for target in targets:
                searched_count += 1
                try:
                    record = self.__process_target(
                        target=target,
                        searchchain=searchchain,
                        downloadchain=downloadchain,
                        rule_groups=rule_groups,
                        search_sites=search_sites,
                    )
                except Exception:
                    logger.error(
                        "整理记录单集洗版处理失败：%s %s - %s",
                        target.title,
                        target.episode_text,
                        traceback.format_exc(),
                    )
                    record = self.__history_record(
                        target=target,
                        candidate=None,
                        status="处理失败",
                    )

                recent_checks[target.key] = {
                    "title": target.title,
                    "episode": target.episode_text,
                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                if record:
                    new_history.append(record)
                    if record.get("downloaded"):
                        upgraded_count += 1

            merged_history = (new_history + old_history)[:200]
            self.save_data("history", merged_history)
            self.save_data("recent_checks", recent_checks)
            self._clearflag = False

            logger.info(
                "整理记录单集洗版本轮完成：搜索 %s 集，推送洗版 %s 集",
                searched_count,
                upgraded_count,
            )
            if self._notify and new_history:
                self.__post_summary(
                    searched_count=searched_count,
                    upgraded_count=upgraded_count,
                    history_records=new_history,
                )

    def __process_target(
        self,
        target: EpisodeTarget,
        searchchain: SearchChain,
        downloadchain: DownloadChain,
        rule_groups: List[str],
        search_sites: List[int],
    ) -> Optional[dict]:
        mediainfo = self.__recognize_target(target)
        if not mediainfo:
            logger.warning("%s %s 未识别到媒体信息，跳过", target.title, target.episode_text)
            return self.__history_record(target=target, candidate=None, status="未识别")

        media_key = mediainfo.tmdb_id or mediainfo.douban_id
        if not media_key:
            return self.__history_record(target=target, candidate=None, status="缺少媒体ID")

        fresh_mediainfo = self.__fresh_mediainfo(mediainfo=mediainfo, season=target.season)
        no_exists = {
            media_key: {
                target.season: NotExistMediaInfo(
                    season=target.season,
                    episodes=[target.episode],
                    total_episode=1,
                    start_episode=target.episode,
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
            season=target.season,
            episode=target.episode,
        )
        candidates = [self.__build_candidate(context) for context in contexts]
        candidates = [candidate for candidate in candidates if candidate]
        candidates = sorted(candidates, key=lambda item: item.rank_tuple, reverse=True)

        if not candidates:
            logger.info("%s %s 未搜索到可用单集资源", target.title, target.episode_text)
            return self.__history_record(target=target, candidate=None, status="未找到资源")

        candidate = candidates[0]
        if not self.__is_upgrade(target=target, candidate=candidate):
            logger.info(
                "%s %s 当前最佳资源未超过原版本：原=%s/%s 新=%s/%s",
                target.title,
                target.episode_text,
                target.baseline_quality,
                target.baseline_tuple,
                candidate.quality,
                candidate.rank_tuple[:4],
            )
            return self.__history_record(target=target, candidate=candidate, status="未超过原版本")

        logger.info(
            "检测到更优单集：%s %s | 原=%s/%s 新=%s/%s | %s",
            target.title,
            target.episode_text,
            target.baseline_quality,
            target.baseline_tuple,
            candidate.quality,
            candidate.rank_tuple[:4],
            candidate.title,
        )
        if not self.__download_candidate(downloadchain=downloadchain, candidate=candidate):
            return self.__history_record(target=target, candidate=candidate, status="下载失败")

        return self.__history_record(target=target, candidate=candidate, status="已推送", downloaded=True)

    def __load_targets(self, recent_checks: Dict[str, dict]) -> List[EpisodeTarget]:
        since = (
            datetime.datetime.now() - datetime.timedelta(days=self._scan_days)
        ).strftime("%Y-%m-%d %H:%M:%S")

        try:
            histories = TransferHistoryOper().list_by_date(since) or []
        except Exception:
            logger.error("读取整理记录失败：%s", traceback.format_exc())
            return []

        targets: Dict[str, EpisodeTarget] = {}
        for history in histories:
            if len(targets) >= self._scan_limit:
                break
            target = self.__build_target(history)
            if not target:
                continue
            if target.key in targets:
                continue
            if not self.__match_target(target):
                continue
            if self.__in_cooldown(target.key, recent_checks):
                continue
            targets[target.key] = target
        return list(targets.values())

    def __build_target(self, history: Any) -> Optional[EpisodeTarget]:
        if not bool(getattr(history, "status", False)):
            return None
        if str(getattr(history, "type", "") or "") != MediaType.TV.value:
            return None

        seasons = self.__extract_numbers(getattr(history, "seasons", None))
        episodes = self.__extract_numbers(getattr(history, "episodes", None))
        if not seasons:
            seasons = [1]
        if len(seasons) != 1 or len(episodes) != 1:
            return None

        season = seasons[0]
        episode = episodes[0]
        if season <= 0 or episode <= 0:
            return None

        title = str(getattr(history, "title", "") or "").strip()
        if not title:
            return None

        baseline_text = self.__history_text(history)
        baseline_quality, baseline_tuple = self.__version_tuple(baseline_text)
        if baseline_tuple[0] <= 0 and not self._allow_unknown_baseline:
            logger.info("%s S%02dE%02d 原版本质量未知，按配置跳过", title, season, episode)
            return None

        return EpisodeTarget(
            history_id=self.__safe_int(getattr(history, "id", 0)),
            title=title,
            year=getattr(history, "year", None),
            tmdbid=getattr(history, "tmdbid", None),
            doubanid=getattr(history, "doubanid", None),
            season=season,
            episode=episode,
            baseline_text=baseline_text,
            baseline_quality=baseline_quality,
            baseline_tuple=baseline_tuple,
            date=str(getattr(history, "date", "") or ""),
            image=getattr(history, "image", None),
        )

    def __recognize_target(self, target: EpisodeTarget) -> Optional[MediaInfo]:
        mediainfo = None
        if target.tmdbid:
            mediainfo = self.chain.recognize_media(
                mtype=MediaType.TV,
                tmdbid=target.tmdbid,
                cache=True,
            )
        if not mediainfo and target.doubanid:
            mediainfo = self.chain.recognize_media(
                mtype=MediaType.TV,
                doubanid=target.doubanid,
                cache=True,
            )
        if not mediainfo:
            title = f"{target.title} {target.year or ''}".strip()
            meta = MetaInfo(title=title)
            meta.type = MediaType.TV
            mediainfo = self.chain.recognize_media(meta=meta, cache=True)
        if mediainfo:
            mediainfo.season = target.season
        return mediainfo

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
            meta = getattr(context, "meta_info", None)
            if not meta:
                continue

            seasons = list(getattr(meta, "season_list", None) or [])
            if not seasons:
                seasons = [getattr(meta, "begin_season", None) or 1]
            seasons = [self.__safe_int(item) for item in seasons if self.__safe_int(item) > 0]
            if len(seasons) != 1 or seasons[0] != season:
                continue

            episodes = sorted(
                {
                    self.__safe_int(ep)
                    for ep in (getattr(meta, "episode_list", None) or [])
                    if self.__safe_int(ep) > 0
                }
            )
            if episodes != [episode]:
                continue
            results.append(context)
        return results

    def __build_candidate(self, context: Any) -> Optional[UpgradeCandidate]:
        torrent = getattr(context, "torrent_info", None)
        if not torrent:
            return None
        title = str(getattr(torrent, "title", "") or "").strip()
        description = str(getattr(torrent, "description", "") or "").strip()
        if not title:
            return None
        quality, version_tuple = self.__version_tuple(f"{title} {description}")
        rank_tuple = (
            version_tuple[0],
            version_tuple[1],
            version_tuple[2],
            version_tuple[3],
            self.__safe_int(getattr(torrent, "seeders", 0)),
            self.__safe_int(getattr(torrent, "peers", 0)),
            self.__safe_int(getattr(torrent, "pri_order", 0)),
        )
        return UpgradeCandidate(
            context=context,
            title=title,
            site=str(getattr(torrent, "site_name", "") or ""),
            quality=quality,
            rank_tuple=rank_tuple,
        )

    def __is_upgrade(self, target: EpisodeTarget, candidate: UpgradeCandidate) -> bool:
        old_tuple = target.baseline_tuple
        new_tuple = candidate.rank_tuple[:4]
        old_quality_score = old_tuple[0]
        new_quality_score = new_tuple[0]

        if old_quality_score <= 0 and self._allow_unknown_baseline:
            return new_quality_score > 0

        if new_quality_score >= old_quality_score + self._min_upgrade_score:
            return True

        if self._allow_same_quality_better and new_quality_score == old_quality_score:
            return new_tuple > old_tuple

        return False

    def __download_candidate(self, downloadchain: DownloadChain, candidate: UpgradeCandidate) -> bool:
        original_post_message = downloadchain.post_message
        try:
            downloadchain.post_message = self.__suppress_post_message
            result = downloadchain.download_single(
                context=candidate.context,
                save_path=self._save_path,
                source="整理记录单集洗版",
                username="整理记录单集洗版",
            )
        except Exception:
            logger.error("推送洗版下载失败：%s - %s", candidate.title, traceback.format_exc())
            return False
        finally:
            downloadchain.post_message = original_post_message
        return bool(result)

    def __history_record(
        self,
        target: EpisodeTarget,
        candidate: Optional[UpgradeCandidate],
        status: str,
        downloaded: bool = False,
    ) -> dict:
        return {
            "title": f"{target.title} ({target.year})" if target.year else target.title,
            "episode": target.episode_text,
            "old_quality": target.baseline_quality,
            "old_scores": list(target.baseline_tuple),
            "new_quality": candidate.quality if candidate else "",
            "new_scores": list(candidate.rank_tuple[:4]) if candidate else [],
            "site": candidate.site if candidate else "",
            "candidate": candidate.title if candidate else "",
            "status": status,
            "downloaded": downloaded,
            "history_id": target.history_id,
            "target_key": target.key,
            "poster": target.image,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def __post_summary(
        self,
        searched_count: int,
        upgraded_count: int,
        history_records: List[dict],
    ):
        details = []
        for item in history_records[:8]:
            details.append(
                f"{item.get('title')} {item.get('episode')} | "
                f"{item.get('old_quality')} -> {item.get('new_quality') or '-'} | "
                f"{item.get('status')}"
            )
        text = "\n".join(
            [
                f"本轮搜索：{searched_count}",
                f"推送洗版：{upgraded_count}",
                "",
                *details,
            ]
        ).strip()
        self.post_message(
            mtype=schemas.NotificationType.Manual,
            title="【整理记录单集洗版】",
            text=text,
        )

    def __match_target(self, target: EpisodeTarget) -> bool:
        text = f"{target.title} {target.year or ''} {target.baseline_text}"
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

    def __history_text(self, history: Any) -> str:
        parts = [
            getattr(history, "src", None),
            getattr(history, "dest", None),
            getattr(history, "title", None),
            getattr(history, "year", None),
        ]
        files = getattr(history, "files", None) or []
        if isinstance(files, list):
            for item in files[:8]:
                if isinstance(item, dict):
                    parts.append(item.get("path") or item.get("name") or item.get("filename"))
                else:
                    parts.append(str(item))
        src_fileitem = getattr(history, "src_fileitem", None) or {}
        dest_fileitem = getattr(history, "dest_fileitem", None) or {}
        if isinstance(src_fileitem, dict):
            parts.append(src_fileitem.get("path") or src_fileitem.get("name"))
        if isinstance(dest_fileitem, dict):
            parts.append(dest_fileitem.get("path") or dest_fileitem.get("name"))
        return " ".join(str(part) for part in parts if part)

    def __version_tuple(self, text: str) -> Tuple[str, Tuple[int, int, int, int]]:
        quality_label, quality_score = self.__quality_rank(text)
        return quality_label, (
            quality_score,
            self.__fps_rank(text),
            self.__codec_rank(text),
            self.__size_rank(text),
        )

    def __quality_rank(self, text: str) -> Tuple[str, int]:
        detected = self.__normalize_quality_label(text)
        order = [item.strip().lower() for item in self._quality_order.split(",") if item.strip()]
        normalized_order = [self.__normalize_quality_label(item) for item in order]
        normalized_order = [item for item in normalized_order if item]

        if "4k_hdr" not in normalized_order:
            if "2160p" in normalized_order:
                normalized_order.insert(normalized_order.index("2160p"), "4k_hdr")
            else:
                normalized_order.insert(0, "4k_hdr")
        if detected not in normalized_order:
            normalized_order.append(detected)
        if "other" not in normalized_order:
            normalized_order.append("other")

        if detected == "other":
            return self.__display_quality_label(detected), 0

        score_map = {
            label: len(normalized_order) - index
            for index, label in enumerate(normalized_order)
        }
        return self.__display_quality_label(detected), score_map.get(detected, 0)

    @staticmethod
    def __normalize_quality_label(text: str) -> str:
        normalized = str(text or "").lower()
        is_2160 = bool(re.search(r"2160p|4k|uhd", normalized))
        has_hdr = bool(
            re.search(
                r"hdr10\+?|(?<![a-z0-9])hdr(?![a-z0-9])|dolby\s*vision|(?<![a-z0-9])dv(?![a-z0-9])|臻彩|真彩|高动态",
                normalized,
            )
        )
        if "4k_hdr" in normalized:
            return "4k_hdr"
        if is_2160 and has_hdr:
            return "4k_hdr"
        if is_2160:
            return "2160p"
        if re.search(r"1080p|1080i", normalized):
            return "1080p"
        if re.search(r"720p", normalized):
            return "720p"
        if re.search(r"480p|576p|\bsd\b", normalized):
            return "sd"
        return "other"

    @staticmethod
    def __display_quality_label(label: str) -> str:
        if label == "4k_hdr":
            return "4K HDR"
        if label == "2160p":
            return "4K"
        if label == "1080p":
            return "1080p"
        if label == "720p":
            return "720p"
        if label == "sd":
            return "SD"
        return "other"

    def __codec_rank(self, text: str) -> int:
        if not self._prefer_hevc:
            return 0
        normalized = str(text or "").lower()
        if re.search(r"hevc|h\.?265|x265", normalized):
            return 2
        if re.search(r"h\.?264|x264|avc", normalized):
            return 1
        return 0

    @staticmethod
    def __fps_rank(text: str) -> int:
        normalized = str(text or "").lower()
        if re.search(r"(?<!\d)(120|100)\s*(fps|帧)|(?<!\d)(120|100)p(?!\d)|高帧率", normalized):
            return 2
        if re.search(r"(?<!\d)60\s*(fps|帧)|(?<!\d)60p(?!\d)", normalized):
            return 1
        return 0

    @staticmethod
    def __size_rank(text: str) -> int:
        normalized = str(text or "")
        matches = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*(GB|GiB|G|MB|MiB|M)(?![a-zA-Z])", normalized, re.IGNORECASE)
        if not matches:
            return 0
        best = 0
        for number, unit in matches:
            size = float(number)
            if unit.lower().startswith("g"):
                size *= 1024
            best = max(best, int(size))
        return best

    def __get_search_sites(self) -> List[int]:
        if self._sites:
            return self._sites
        return self.systemconfig.get(SystemConfigKey.RssSites)

    def __in_cooldown(self, key: str, recent_checks: Dict[str, dict]) -> bool:
        if self._cooldown_hours <= 0:
            return False
        recent = recent_checks.get(key) or {}
        record_time = self.__parse_datetime(recent.get("time"))
        if not record_time:
            return False
        return (datetime.datetime.now() - record_time).total_seconds() < self._cooldown_hours * 3600

    def __cleanup_recent_checks(self, recent_checks: Dict[str, dict]) -> Dict[str, dict]:
        if self._cooldown_hours <= 0:
            return {}
        cleaned = {}
        now = datetime.datetime.now()
        for key, value in (recent_checks or {}).items():
            if not isinstance(value, dict):
                continue
            record_time = self.__parse_datetime(value.get("time"))
            if not record_time:
                continue
            if (now - record_time).total_seconds() > self._cooldown_hours * 3600:
                continue
            cleaned[key] = value
        return cleaned

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "clear": self._clear,
                "cron": self._cron,
                "scan_days": self._scan_days,
                "scan_limit": self._scan_limit,
                "cooldown_hours": self._cooldown_hours,
                "sites": self._sites,
                "include": self._include,
                "exclude": self._exclude,
                "save_path": self._save_path,
                "use_subscribe_rules": self._use_subscribe_rules,
                "prefer_hevc": self._prefer_hevc,
                "allow_same_quality_better": self._allow_same_quality_better,
                "allow_unknown_baseline": self._allow_unknown_baseline,
                "min_upgrade_score": self._min_upgrade_score,
                "quality_order": self._quality_order,
            }
        )

    @staticmethod
    def __extract_numbers(value: Any) -> List[int]:
        if value is None:
            return []
        return [int(item) for item in re.findall(r"\d+", str(value))]

    @staticmethod
    def __safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

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

    @staticmethod
    def __suppress_post_message(*args, **kwargs):
        return None


def _col(cols: int, child: dict) -> dict:
    return {
        "component": "VCol",
        "props": {"cols": cols},
        "content": [child],
    }


def _switch(model: str, label: str) -> dict:
    return {
        "component": "VSwitch",
        "props": {
            "model": model,
            "label": label,
        },
    }


def _textfield(model: str, label: str, placeholder: str = "") -> dict:
    return {
        "component": "VTextField",
        "props": {
            "model": model,
            "label": label,
            "placeholder": placeholder,
            "clearable": True,
        },
    }


def _cronfield(model: str, label: str, placeholder: str = "") -> dict:
    return {
        "component": "VCronField",
        "props": {
            "model": model,
            "label": label,
            "placeholder": placeholder,
        },
    }
