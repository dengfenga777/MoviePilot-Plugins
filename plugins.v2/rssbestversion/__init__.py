import datetime
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    plugin_version = "2.2.0"
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
    _skip_tv_without_episode: bool = True
    _max_downloads_per_run: int = 8
    _max_run_minutes: int = 20
    _rss_workers: int = 4
    _site_priority_rules: Optional[Dict[str, int]] = None
    _pending_run: bool = False

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
            self._skip_tv_without_episode = bool(config.get("skip_tv_without_episode", True))
            self._max_downloads_per_run = max(0, self.__safe_int(config.get("max_downloads_per_run") or 8))
            self._max_run_minutes = max(0, self.__safe_int(config.get("max_run_minutes") or 20))

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
                    "kwargs": {},
                }
            ]
        if self._enabled:
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
                            _col(4, _switch("skip_complete", "整季/完结包直接跳过")),
                            _col(4, _switch("skip_tv_without_episode", "电视剧无集号则跳过")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col(6, _textfield("max_downloads_per_run", "单轮最多推送数", "0 表示不限制，默认 8")),
                            _col(6, _textfield("max_run_minutes", "单轮最长执行(分钟)", "0 表示不限制，默认 20")),
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
                                                "本插件只做四件事：识别剧集、同集挑最优版本、"
                                                "整季/完结包过滤、以及更大体积版本的再次推送。"
                                                "同一集出现 4K 和 1080p 时，只会推送优先级更高的那个。"
                                                "为避免卡住下一轮调度，可限制单轮推送数量和最长执行时间。"
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
            "skip_tv_without_episode": True,
            "max_downloads_per_run": 8,
            "max_run_minutes": 20,
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
                "skip_tv_without_episode": self._skip_tv_without_episode,
                "max_downloads_per_run": self._max_downloads_per_run,
                "max_run_minutes": self._max_run_minutes,
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

        try:
            if not self._address:
                logger.info("未配置 RSS 地址，跳过本次执行")
                return

            filter_groups = self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
            candidates = self.__collect_candidates(filter_groups=filter_groups)
            if not candidates:
                logger.info("本轮 RSS 未产生可下载候选资源")
                return

            plans = self.__build_download_plans(candidates=candidates, history_lookup=history_lookup)
            if not plans:
                logger.info("本轮 RSS 无需下载的优选资源")
                return

            history_changed = self.__execute_download_plans(
                plans=plans,
                history_lookup=history_lookup,
                started_at=started_at,
            ) or history_changed
        finally:
            if history_changed:
                self.save_data("history", list(history_lookup.values()))
            elapsed = time.monotonic() - started_at
            logger.info("RSS优选下载本轮执行完成，用时 %.1f 秒", elapsed)
            self._clearflag = False

    def __load_history_lookup(self) -> Dict[str, dict]:
        if self._clearflag:
            return {}
        history = self.get_data("history") or []
        return {item.get("group_key"): item for item in history if item.get("group_key")}

    def __collect_candidates(self, filter_groups: Any) -> List[Candidate]:
        candidates: List[Candidate] = []
        urls = self.__rss_urls()
        if not urls:
            return candidates

        fetched_results = self.__fetch_rss_results(urls)

        for url in urls:
            results = fetched_results.get(url) or []
            if not results:
                logger.error("未获取到 RSS 数据：%s", url)
                continue

            candidate_count_before = len(candidates)
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
                "RSS %s 刷新完成，本次新增候选 %s 条，候选累计 %s 条",
                url,
                len(candidates) - candidate_count_before,
                len(candidates),
            )

        return candidates

    def __fetch_rss_results(self, urls: List[str]) -> Dict[str, List[dict]]:
        if len(urls) <= 1:
            url = urls[0]
            return {url: self.__fetch_single_rss(url)}

        max_workers = min(len(urls), self._rss_workers)
        logger.info("开始并发刷新 RSS，共 %s 个源，worker=%s", len(urls), max_workers)

        fetched_results: Dict[str, List[dict]] = {}
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rssbestversion") as executor:
            future_to_url = {
                executor.submit(self.__fetch_single_rss, url): url
                for url in urls
            }
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    fetched_results[url] = future.result() or []
                except Exception as err:
                    logger.error("刷新 RSS 失败：%s - %s", url, str(err))
                    fetched_results[url] = []

        return fetched_results

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
        quality_label, quality_score = self.__quality_rank(text)

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

        if candidate.mediainfo.type != MediaType.TV:
            if candidate.exist_info:
                return f"{title_year} 已存在"
            return None

        if candidate.is_complete_pack and self._skip_complete:
            season = candidate.meta.season or ""
            return f"{title_year} {season} 命中整季/完结包规则，已直接跳过".strip()

        if self._skip_tv_without_episode and not (candidate.meta.episode_list or []):
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
        chosen_map: Dict[str, Candidate] = {}

        for candidate in candidates:
            eligible_keys = self.__eligible_group_keys(
                candidate=candidate,
                history_lookup=history_lookup,
            )
            if not eligible_keys:
                logger.info("%s 已在下载历史中，且未发现更大体积版本", self.__candidate_label(candidate))
                continue

            for group_key in eligible_keys:
                current = chosen_map.get(group_key)
                if current and candidate.sort_tuple <= current.sort_tuple:
                    continue

                chosen_map[group_key] = candidate
                if current and current.raw_title != candidate.raw_title:
                    logger.info(
                        "同一剧集版本替换：%s -> %s | key=%s | 分值=%s > %s",
                        current.raw_title,
                        candidate.raw_title,
                        group_key,
                        candidate.sort_tuple,
                        current.sort_tuple,
                    )

        return self.__merge_download_plans(chosen_map)

    def __eligible_group_keys(
        self,
        candidate: Candidate,
        history_lookup: Dict[str, dict],
    ) -> List[str]:
        eligible_keys: List[str] = []
        upgraded_keys: List[str] = []

        for group_key in candidate.group_keys:
            previous = history_lookup.get(group_key)
            if not previous:
                eligible_keys.append(group_key)
                continue

            previous_size = self.__safe_int(previous.get("size"))
            if candidate.size_score > previous_size:
                eligible_keys.append(group_key)
                upgraded_keys.append(group_key)

        if upgraded_keys:
            logger.info(
                "%s 检测到更大版本，允许再次推送：%s | keys=%s",
                self.__candidate_label(candidate),
                candidate.size_score,
                upgraded_keys,
            )

        return eligible_keys

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
        started_at: float,
    ) -> bool:
        changed = False
        attempted = 0

        for plan in plans:
            if self.__should_stop_current_run(started_at=started_at, attempted=attempted):
                logger.warning(
                    "已达到单轮执行保护阈值，剩余 %s 个优选资源留待下个周期继续处理",
                    len(plans) - attempted,
                )
                break

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
                attempted += 1
                continue

            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            season_episode = self.__season_episode_text(candidate.meta)
            for group_key in plan.group_keys:
                history_lookup[group_key] = {
                    "title": candidate.mediainfo.title_year,
                    "season_episode": season_episode,
                    "group_key": group_key,
                    "quality": candidate.quality_label,
                    "site": candidate.site_name,
                    "raw_title": candidate.raw_title,
                    "poster": candidate.mediainfo.get_poster_image(),
                    "tmdbid": candidate.mediainfo.tmdb_id,
                    "size": candidate.size_score,
                    "time": now,
                }
                changed = True

            attempted += 1

        return changed

    def __should_stop_current_run(self, started_at: float, attempted: int) -> bool:
        if self._max_downloads_per_run and attempted >= self._max_downloads_per_run:
            return True
        if self._max_run_minutes and (time.monotonic() - started_at) >= self._max_run_minutes * 60:
            return True
        return False

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
        text = f"{title} {description or ''}"
        normalized = text.lower()
        if not COMPLETE_HINTS.search(normalized):
            return False

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
