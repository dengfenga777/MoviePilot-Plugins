import datetime
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple, Set

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
HISTORY_REF_LIMIT = 12
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
    upgrade_score: int
    site_score: int
    codec_score: int
    size_score: int
    pubdate_score: int
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
    def sort_tuple(self) -> Tuple[int, int, int, int, int]:
        return (
            self.quality_score,
            self.site_score,
            self.codec_score,
            self.size_score,
            self.pubdate_score,
        )


@dataclass
class DownloadPlan:
    candidate: Candidate
    group_keys: List[str]


class RssBestVersion(_PluginBase):
    plugin_name = "RSS优选下载"
    plugin_desc = "识别同一剧集的多个版本，只保留优先级最高的资源下发下载。"
    plugin_icon = "rss.png"
    plugin_version = "2.2.9"
    plugin_author = "Codex"
    author_url = "https://github.com/openai"
    plugin_config_prefix = "rssbestversion_"
    plugin_order = 20
    auth_level = 2

    _scheduler: Optional[BackgroundScheduler] = None
    _cache_path: Optional[Path] = None

    _enabled: bool = False
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
    _skip_tv_without_episode: bool = True
    _site_priority_rules: Optional[Dict[str, int]] = None
    _pending_run: bool = False

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self.__validate_and_fix_config(config=config)
            self._enabled = bool(config.get("enabled"))
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
            self._skip_tv_without_episode = bool(config.get("skip_tv_without_episode", True))

        self._site_priority_rules = self.__parse_site_priority()

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
                    # Allow one extra scheduler instance to enter `check()`,
                    # so overlapping triggers can register a pending rerun
                    # instead of being dropped by APScheduler directly.
                    "kwargs": {"max_instances": 2},
                }
            ]
        if self._enabled:
            return [
                {
                    "id": "RssBestVersion",
                    "name": "RSS优选下载服务",
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
                            _col(4, _switch("enabled", "启用插件")),
                            _col(4, _switch("onlyonce", "立即运行一次")),
                            _col(4, _switch("clear", "清理历史记录")),
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
                            _col(4, _switch("skip_complete", "整季/完结包直接跳过")),
                            _col(4, _switch("skip_tv_without_episode", "电视剧无集号则跳过")),
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
                                                "本插件会按单条 RSS 单独比较同一集资源，"
                                                "每轮只推当前 RSS 里最优的一个版本。"
                                                "后续只有在出现更高等级版本时才会继续推送。"
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
            "skip_tv_without_episode": True,
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

        historys = sorted(historys, key=lambda item: item.get("time"), reverse=True)
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

        historys = [item for item in historys if item.get("group_key") != key]
        self.save_data("history", historys)
        return schemas.Response(success=True, message="删除成功")

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
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
                "skip_tv_without_episode": self._skip_tv_without_episode,
            }
        )

    def check(self):
        if not lock.acquire(blocking=False):
            self._pending_run = True
            logger.warning("RSS优选下载任务仍在运行，已登记本轮补跑请求")
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
        started_at = time.monotonic()
        history_lookup: Dict[str, dict] = self.__load_history_lookup()
        history_changed = self._clearflag
        candidate_total = 0
        plan_total = 0

        try:
            if not self._address:
                logger.info("未配置 RSS 地址，跳过本次执行")
                return

            filter_groups = self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
            urls = self.__rss_urls()
            if not urls:
                logger.info("未配置有效 RSS 地址，跳过本次执行")
                return

            for url in urls:
                candidates = self.__collect_candidates_for_url(
                    url=url,
                    filter_groups=filter_groups,
                )
                if not candidates:
                    continue

                candidate_total += len(candidates)
                plans = self.__build_download_plans(
                    candidates=candidates,
                    history_lookup=history_lookup,
                )
                if not plans:
                    logger.info("RSS %s 无需下载的优选资源", url)
                    continue

                plan_total += len(plans)
                changed = self.__execute_download_plans(
                    plans=plans,
                    history_lookup=history_lookup,
                )
                history_changed = changed or history_changed
        finally:
            if history_changed:
                self.save_data("history", list(history_lookup.values()))
            elapsed = time.monotonic() - started_at
            if not candidate_total:
                logger.info("本轮 RSS 未产生可下载候选资源")
            elif not plan_total:
                logger.info("本轮 RSS 无需下载的优选资源")
            logger.info("RSS优选下载本轮执行完成，用时 %.1f 秒", elapsed)
            self._clearflag = False

    def __load_history_lookup(self) -> Dict[str, dict]:
        if self._clearflag:
            return {}
        history = self.get_data("history") or []
        return {item.get("group_key"): item for item in history if item.get("group_key")}

    def __collect_candidates_for_url(self, url: str, filter_groups: Any) -> List[Candidate]:
        candidates: List[Candidate] = []
        results = self.__fetch_single_rss(url)
        if not results:
            logger.error("未获取到 RSS 数据：%s", url)
            return candidates

        for result in results:
            try:
                candidate = self.__build_candidate(
                    result=result,
                    source_url=url,
                    filter_groups=filter_groups,
                )
                if not candidate:
                    continue

                skip_reason = self.__candidate_skip_reason(candidate)
                if skip_reason:
                    logger.info(skip_reason)
                    continue

                candidates.append(candidate)
            except Exception as err:
                logger.error("解析 RSS 条目出错：%s - %s", str(err), traceback.format_exc())

        logger.info(
            "RSS %s 刷新完成，本次新增候选 %s 条",
            url,
            len(candidates),
        )

        return candidates

    def __fetch_single_rss(self, url: str) -> List[dict]:
        logger.info("开始刷新 RSS：%s ...", url)
        results = RssHelper().parse(url, proxy=self._proxy)
        if results:
            logger.info("RSS %s 抓取完成，原始条目 %s 条", url, len(results))
            return results
        return []

    def __build_candidate(
        self,
        result: dict,
        source_url: str,
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
        if not self.__match_text_filters(title=title, description=description, size=size):
            return None

        is_complete_pack = self.__is_complete_pack(title=title, description=description)
        meta_title = self.__build_meta_title(title=title, description=description)
        meta = MetaInfo(title=meta_title, subtitle=description)
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
        if not self.__match_subscribe_rules(
            torrent=torrent,
            mediainfo=mediainfo,
            filter_groups=filter_groups,
        ):
            logger.info("%s 不匹配订阅优先级规则", title)
            return None

        group_keys = self.__build_group_keys(mediainfo=mediainfo, meta=meta)
        if not group_keys:
            logger.info("%s 未识别到可比较的剧集键", title)
            return None

        site_name = self.__site_name(source_url)
        text = f"{title} {description or ''}"
        quality_label, quality_score, upgrade_score = self.__quality_rank(text)

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
            upgrade_score=upgrade_score,
            site_score=self.__site_priority_score(site_name),
            codec_score=self.__codec_rank(text),
            size_score=self.__safe_int(size),
            pubdate_score=int(pubdate.timestamp()) if pubdate else 0,
            exist_info=self.chain.media_exists(mediainfo=mediainfo),
            is_complete_pack=is_complete_pack,
        )

    def __match_text_filters(self, title: str, description: Optional[str], size: Any) -> bool:
        text = f"{title} {description or ''}"
        if self._include and not re.search(self._include, text, re.IGNORECASE):
            logger.info("%s 不符合包含规则", title)
            return False
        if self._exclude and re.search(self._exclude, text, re.IGNORECASE):
            logger.info("%s 命中排除规则", title)
            return False
        if self._size_range and not self.__match_size_range(size):
            logger.info("%s - 种子大小不在指定范围", title)
            return False
        return True

    def __match_subscribe_rules(
        self,
        torrent: TorrentInfo,
        mediainfo: MediaInfo,
        filter_groups: Any,
    ) -> bool:
        if not self._filter:
            return True
        filtered = self.chain.filter_torrents(
            rule_groups=filter_groups,
            torrent_list=[torrent],
            mediainfo=mediainfo,
        )
        return bool(filtered)

    def __candidate_skip_reason(self, candidate: Candidate) -> Optional[str]:
        title_year = candidate.mediainfo.title_year or candidate.raw_title
        season_episode = self.__season_episode_text(candidate.meta)
        is_season_pack = self.__is_season_pack(candidate)
        is_pack_candidate = candidate.is_complete_pack or is_season_pack

        if candidate.mediainfo.type != MediaType.TV:
            if candidate.exist_info:
                return f"{title_year} 已存在"
            return None

        if candidate.is_complete_pack and self._skip_complete:
            if self.__library_missing(candidate):
                logger.info("%s 媒体库不存在，允许下载完结包", title_year)
                return None
            season = candidate.meta.season or ""
            return f"{title_year} {season} 命中整季/完结包规则，已直接跳过".strip()

        if self._skip_complete and is_season_pack:
            if self.__library_missing(candidate):
                logger.info("%s 媒体库不存在，允许下载整季包", title_year)
                return None
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

        return None

    def __build_download_plans(
        self,
        candidates: List[Candidate],
        history_lookup: Dict[str, dict],
    ) -> List[DownloadPlan]:
        grouped_candidates: Dict[str, Dict[str, Candidate]] = {}
        for candidate in candidates:
            for group_key in candidate.group_keys:
                candidate_map = grouped_candidates.setdefault(group_key, {})
                current = candidate_map.get(candidate.candidate_id)
                if not current or candidate.sort_tuple > current.sort_tuple:
                    candidate_map[candidate.candidate_id] = candidate

        selected_plans: Dict[str, DownloadPlan] = {}
        for group_key, candidate_map in grouped_candidates.items():
            ordered_candidates = sorted(
                candidate_map.values(),
                key=lambda item: item.sort_tuple,
                reverse=True,
            )
            selected_candidates = self.__select_candidates_for_group(
                group_key=group_key,
                candidates=ordered_candidates,
                history_record=history_lookup.get(group_key),
            )
            for candidate in selected_candidates:
                plan = selected_plans.get(candidate.candidate_id)
                if not plan:
                    plan = DownloadPlan(candidate=candidate, group_keys=[])
                    selected_plans[candidate.candidate_id] = plan
                plan.group_keys.append(group_key)

        for plan in selected_plans.values():
            plan.group_keys = sorted(set(plan.group_keys))

        return sorted(
            selected_plans.values(),
            key=lambda item: item.candidate.sort_tuple,
            reverse=True,
        )

    def __select_candidates_for_group(
        self,
        group_key: str,
        candidates: List[Candidate],
        history_record: Optional[dict],
    ) -> List[Candidate]:
        if not candidates:
            return []

        pushed_ids, pushed_titles = self.__history_pushed_refs(history_record)
        best_upgrade_score = self.__history_best_upgrade_score(history_record)

        for candidate in candidates:
            if candidate.candidate_id in pushed_ids:
                continue
            if candidate.raw_title in pushed_titles:
                continue

            if not history_record:
                return [candidate]

            if candidate.upgrade_score > best_upgrade_score:
                logger.info(
                    "%s 检测到更高等级版本，继续推送：%s > %s | key=%s",
                    self.__candidate_label(candidate),
                    candidate.upgrade_score,
                    best_upgrade_score,
                    group_key,
                )
                return [candidate]

        if history_record:
            logger.info("%s 已存在历史记录，当前 RSS 未出现更高等级新版本", self.__history_label(history_record) or group_key)

        return []

    def __history_pushed_refs(self, history_record: Optional[dict]) -> Tuple[Set[str], Set[str]]:
        if not history_record:
            return set(), set()

        pushed_ids = {
            str(item).strip()
            for item in history_record.get("pushed_ids") or []
            if str(item).strip()
        }
        pushed_titles = {
            str(item).strip()
            for item in history_record.get("pushed_titles") or []
            if str(item).strip()
        }

        candidate_id = history_record.get("candidate_id")
        if candidate_id:
            pushed_ids.add(str(candidate_id).strip())

        raw_title = history_record.get("raw_title")
        if raw_title:
            pushed_titles.add(str(raw_title).strip())

        return pushed_ids, pushed_titles

    def __history_best_upgrade_score(self, history_record: Optional[dict]) -> int:
        if not history_record:
            return 0

        upgrade_score = self.__safe_int(history_record.get("best_upgrade_score"))
        if upgrade_score > 0:
            return upgrade_score

        quality_text = history_record.get("best_quality") or history_record.get("quality") or ""
        return self.__quality_upgrade_score(str(quality_text))

    @staticmethod
    def __history_label(history_record: Optional[dict]) -> str:
        if not history_record:
            return ""
        title = history_record.get("title") or ""
        season_episode = history_record.get("season_episode") or ""
        return f"{title} {season_episode}".strip()

    @staticmethod
    def __merge_download_plans(chosen_map: Dict[str, Candidate]) -> List[DownloadPlan]:
        plan_map: Dict[str, DownloadPlan] = {}

        for group_key, candidate in chosen_map.items():
            plan = plan_map.get(candidate.candidate_id)
            if not plan:
                plan = DownloadPlan(candidate=candidate, group_keys=[])
                plan_map[candidate.candidate_id] = plan
            plan.group_keys.append(group_key)

        for plan in plan_map.values():
            plan.group_keys = sorted(set(plan.group_keys))

        return sorted(
            plan_map.values(),
            key=lambda item: item.candidate.sort_tuple,
            reverse=True,
        )

    def __execute_download_plans(
        self,
        plans: List[DownloadPlan],
        history_lookup: Dict[str, dict],
    ) -> bool:
        changed = False

        for plan in plans:
            candidate = plan.candidate
            logger.info(
                "优选资源：%s | 质量=%s | 站点=%s | 组=%s",
                candidate.raw_title,
                candidate.quality_label,
                candidate.site_name,
                plan.group_keys,
            )
            if not self.__download_candidate(candidate):
                logger.error("下载失败：%s", candidate.raw_title)
                continue

            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            season_episode = self.__season_episode_text(candidate.meta)
            for group_key in plan.group_keys:
                previous_record = history_lookup.get(group_key) or {}
                pushed_ids, pushed_titles = self.__history_pushed_refs(previous_record)
                pushed_ids_list = list(pushed_ids)
                pushed_titles_list = list(pushed_titles)
                if candidate.candidate_id not in pushed_ids_list:
                    pushed_ids_list.append(candidate.candidate_id)
                if candidate.raw_title not in pushed_titles_list:
                    pushed_titles_list.append(candidate.raw_title)
                pushed_ids_list = pushed_ids_list[-HISTORY_REF_LIMIT:]
                pushed_titles_list = pushed_titles_list[-HISTORY_REF_LIMIT:]

                history_lookup[group_key] = {
                    "title": candidate.mediainfo.title_year,
                    "season_episode": season_episode,
                    "group_key": group_key,
                    "quality": candidate.quality_label,
                    "best_quality": candidate.quality_label,
                    "best_upgrade_score": max(
                        self.__history_best_upgrade_score(previous_record),
                        candidate.upgrade_score,
                    ),
                    "site": candidate.site_name,
                    "raw_title": candidate.raw_title,
                    "candidate_id": candidate.candidate_id,
                    "pushed_ids": pushed_ids_list,
                    "pushed_titles": pushed_titles_list,
                    "poster": candidate.mediainfo.get_poster_image(),
                    "tmdbid": candidate.mediainfo.tmdb_id,
                    "size": candidate.size_score,
                    "time": now,
                }
                changed = True

        return changed

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
                save_path=self._save_path,
                username="RSS优选下载",
            )
        except Exception:
            logger.error("推送下载失败：%s - %s", candidate.raw_title, traceback.format_exc())
            return False
        finally:
            downloadchain.post_message = original_post_message

        return bool(result)

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

    def __quality_rank(self, text: str) -> Tuple[str, int, int]:
        detected = self.__normalize_quality_label(text)
        upgrade_score = self.__quality_upgrade_score(detected)

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

        score_map = {
            label: len(normalized_order) - index
            for index, label in enumerate(normalized_order)
        }
        return self.__display_quality_label(detected), score_map.get(detected, 0), upgrade_score

    @staticmethod
    def __normalize_quality_label(text: str) -> str:
        normalized = str(text or "").lower()
        is_2160 = bool(re.search(r"2160p|4k|uhd", normalized))
        has_hdr = bool(
            re.search(
                r"hdr10\+?|(?<![a-z0-9])hdr(?![a-z0-9])|dolby\s*vision|(?<![a-z0-9])dv(?![a-z0-9])",
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

    def __quality_upgrade_score(self, text: str) -> int:
        label = self.__normalize_quality_label(text)
        if label == "4k_hdr":
            return 4
        if label == "2160p":
            return 3
        if label == "1080p":
            return 2
        if label == "720p":
            return 1
        return 0

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

        site_name = site_name.lower()
        rules = self._site_priority_rules or {}
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
            if not line or line.startswith("#") or "=" not in line:
                continue

            domain, score = line.split("=", 1)
            domain = domain.strip().lower()
            if domain.startswith("http://") or domain.startswith("https://"):
                domain = self.__site_name(domain)

            if domain:
                rules[domain] = self.__safe_int(score.strip())

        return rules

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

    def __is_complete_pack(self, title: str, description: Optional[str]) -> bool:
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
    def __library_missing(candidate: Candidate) -> bool:
        return not bool(candidate.exist_info)

    def __match_size_range(self, size: Any) -> bool:
        sizes = [float(item) * 1024 ** 3 for item in self._size_range.split("-")]
        current = self.__safe_int(size)
        if not current:
            return False
        if len(sizes) == 1:
            return current >= sizes[0]
        return sizes[0] <= current <= sizes[1]

    def __candidate_label(self, candidate: Candidate) -> str:
        season_episode = self.__season_episode_text(candidate.meta)
        title = candidate.mediainfo.title_year or candidate.raw_title
        if season_episode:
            return f"{title} {season_episode}"
        return title

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

    def __rss_urls(self) -> List[str]:
        return [line.strip() for line in self._address.splitlines() if line.strip()]

    @staticmethod
    def __suppress_post_message(*args, **kwargs):
        return None

    def __log_error(self, message: str):
        logger.error(message)

    def __validate_and_fix_config(self, config: dict = None) -> bool:
        size_range = config.get("size_range")
        if size_range and not self.__is_number_or_range(str(size_range)):
            self.__log_error(f"RSS优选下载出错，种子大小设置错误：{size_range}")
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
