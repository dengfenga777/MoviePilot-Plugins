import html
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from app.chain.download import DownloadChain
from app.chain.search.facade import SearchChain
from app.chain.tmdb import TmdbChain
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaSource, MediaType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger

from .billing import PmsClient, PmsError, PmsInsufficient, PmsUserNotFound, Usage, charge_key
from .picker import brief, clean_title, episode_size, episodes_of, files_to_enable, pick
from .telegram import TelegramBot, TelegramConflict, button
from .tgparse import Request, is_chatter, parse


@dataclass
class Job:
    req: Request
    user_id: int
    user_name: str
    chat_id: int
    message_id: int = 0
    reply_to: int = 0  # 群友发的原始求片消息
    title: str = ""
    # 集数（0 = 整季 / 电影）-> 候选 Context
    choices: Dict[int, list] = field(default_factory=dict)
    # 集数 -> 已推送的种子标题
    done: Dict[int, str] = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    # 一次求多季时几季共用：本批已占用的免费额度和积分，避免每季单独检查都放行
    batch: Optional[Dict[str, Any]] = None


class TgEpisodeRequest(_PluginBase):
    # 插件名称
    plugin_name = "TG群组智能补集"
    # 插件描述
    plugin_desc = "群成员发 TMDB ID + 缺哪集 / 哪集有问题，自动识别后搜索并下载对应集数。"
    # 插件图标
    plugin_icon = "Telegram_A.png"
    # 插件版本
    plugin_version = "1.6.1"
    # 插件作者
    plugin_author = "misaya"
    # 作者主页
    author_url = "https://github.com/dengfenga777"
    # 插件配置项ID前缀
    plugin_config_prefix = "tgepisoderequest_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 2

    JOB_TTL = 3600
    HISTORY_LIMIT = 200

    # 运行状态
    _bot: Optional[TelegramBot] = None
    _stop: Optional[threading.Event] = None
    _thread: Optional[threading.Thread] = None
    _executor: Optional[ThreadPoolExecutor] = None
    _search_lock: Optional[threading.Semaphore] = None
    _jobs: Dict[str, Job] = {}
    # 正在逐季处理的多季请求，停止口令靠它通知还没轮到的季不要再处理
    _batches: List[Dict[str, Any]] = []
    _jobs_lock = threading.Lock()
    _bot_username: str = ""
    _data_lock = threading.Lock()

    # 配置
    _enabled: bool = False
    _token: str = ""
    _proxy: bool = True
    _allowed_chats: Set[int] = set()
    _admin_ids: Set[int] = set()
    _trigger: str = "auto"
    _auto_download: bool = True
    _check_library: bool = True
    _allow_bare_id: bool = True
    _allow_pack: bool = True
    _min_seeders: int = 1
    _low_seeders: int = 3
    _candidates: int = 3
    _max_episodes: int = 12
    _max_seasons: int = 10
    _downloader: str = ""
    _save_path: str = ""
    _notify_arrival: bool = True
    _wait_library: bool = True
    _pending_days: int = 7
    _pms_enabled: bool = False
    _pms_url: str = ""
    _pms_token: str = ""
    _movie_price: float = 10
    _season_price: float = 20
    _free_movies: int = 3
    _free_seasons: int = 2
    _admin_free: bool = True
    _auto_delete: int = 3
    _delete_request: bool = False
    _notice_delete: int = 0

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config and config.get("config_version", 0) < 2:
            # 1.1.0 起默认自动下载：旧配置里的“手动选择”是当时的默认值，统一切换一次
            config["auto_download"] = True
            config["config_version"] = 2
            self.update_config(config)
            logger.info("TG群组智能补集：已切换为默认自动下载最优资源")
        if config:
            self._enabled = bool(config.get("enabled"))
            self._token = str(config.get("token") or "").strip()
            self._proxy = config.get("proxy", True)
            self._allowed_chats = self.__ids(config.get("allowed_chats"))
            self._admin_ids = self.__ids(config.get("admin_ids"))
            self._trigger = config.get("trigger") or "auto"
            self._auto_download = bool(config.get("auto_download", True))
            self._check_library = bool(config.get("check_library", True))
            self._allow_bare_id = config.get("allow_bare_id", True)
            self._allow_pack = config.get("allow_pack", True)
            self._min_seeders = self.__int(config.get("min_seeders"), 1)
            self._low_seeders = self.__int(config.get("low_seeders"), 3)
            self._candidates = max(1, min(self.__int(config.get("candidates"), 3), 8))
            self._max_episodes = self.__int(config.get("max_episodes"), 12)
            self._max_seasons = max(1, self.__int(config.get("max_seasons"), 10))
            self._downloader = str(config.get("downloader") or "").strip()
            self._save_path = str(config.get("save_path") or "").strip()
            self._notify_arrival = config.get("notify_arrival", True)
            self._wait_library = bool(config.get("wait_library", True))
            self._pending_days = max(1, self.__int(config.get("pending_days"), 7))
            self._pms_enabled = bool(config.get("pms_enabled"))
            self._pms_url = str(config.get("pms_url") or "").strip()
            self._pms_token = str(config.get("pms_token") or "").strip()
            self._movie_price = self.__num(config.get("movie_price"), 10)
            self._season_price = self.__num(config.get("season_price"), 20)
            self._free_movies = self.__int(config.get("free_movies"), 3)
            self._free_seasons = self.__int(config.get("free_seasons"), 2)
            self._admin_free = config.get("admin_free", True)
            self._auto_delete = max(0, self.__int(config.get("auto_delete"), 3))
            self._delete_request = bool(config.get("delete_request"))
            self._notice_delete = max(0, self.__int(config.get("notice_delete"), 0))

        self._jobs = {}
        self._batches = []
        self._search_lock = threading.Semaphore(2)
        if not self._enabled:
            return
        if not self._token:
            logger.warning("TG群组智能补集：未填写机器人 Token，插件不会启动")
            return

        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tgepisoderequest")
        self._bot = TelegramBot(self._token, proxy=settings.PROXY_HOST if self._proxy else None)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self.__run, args=(self._bot, self._stop),
                                        name="tgepisoderequest-poll", daemon=True)
        self._thread.start()
        threading.Thread(target=self.__cleaner, args=(self._bot, self._stop),
                         name="tgepisoderequest-clean", daemon=True).start()

    def get_state(self) -> bool:
        return self._enabled and bool(self._token)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        if self._stop:
            self._stop.set()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        if self._bot:
            # 轮询线程可能还挂在 getUpdates 上，等它自己超时退出后再关闭连接
            bot, thread = self._bot, self._thread
            threading.Thread(target=lambda: (thread and thread.join(45), bot.close()), daemon=True).start()
        self._bot = self._stop = self._thread = self._executor = None

    # ---------------- 配置 ----------------

    @staticmethod
    def __ids(value) -> Set[int]:
        out = set()
        for part in str(value or "").replace("，", ",").replace("\n", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                out.add(int(part))
        return out

    @staticmethod
    def __int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __num(value, default: float) -> float:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return default

    # ---------------- Telegram 轮询 ----------------

    def __run(self, bot: TelegramBot, stop: threading.Event):
        # 插件更新 / 重载时旧实例的轮询线程可能还挂在 getUpdates 上（最多 30 秒），
        # 同一个 Token 同时拉消息会 409，先等它退出
        me_thread = threading.current_thread()
        for t in threading.enumerate():
            if t is not me_thread and t.name == me_thread.name and t.is_alive():
                t.join(40)
        if stop.is_set():
            return
        started = time.time()
        try:
            me = bot.get_me()
            self._bot_username = me.get("username") or ""
            logger.info(f"TG群组智能补集：机器人 @{self._bot_username} 已启动")
        except Exception as e:
            logger.error(f"TG群组智能补集：连接 Telegram 失败，请检查 Token 和代理：{e}")
        last_error = [""]

        def on_error(e: Exception):
            if isinstance(e, TelegramConflict) and time.time() - started < 90:
                # 刚启动时多半是旧连接还没断开，重试几次就好，不算错误
                last_error[0] = last_error[0] or "conflict-startup"
                return
            msg = str(e)
            if msg == last_error[0]:
                return
            last_error[0] = msg
            if isinstance(e, TelegramConflict):
                logger.error("TG群组智能补集：这个 Token 正被其他程序使用（例如 MoviePilot 自带的 Telegram 通知）"
                             "或设置了 Webhook，请给本插件单独创建一个机器人")
            else:
                logger.warning(f"TG群组智能补集：获取消息失败：{e}")

        def on_ok():
            if last_error[0]:
                if last_error[0] != "conflict-startup":
                    logger.info("TG群组智能补集：已恢复接收群消息")
                last_error[0] = ""

        def on_update(update: Dict):
            executor = self._executor
            if executor:
                executor.submit(self.__safe, self.__handle_update, bot, update)

        bot.poll(stop, on_update, on_error, on_ok)

    @staticmethod
    def __safe(fn, *args):
        try:
            fn(*args)
        except Exception as e:
            logger.error(f"TG群组智能补集：处理消息出错：{e}", exc_info=True)

    def __handle_update(self, bot: TelegramBot, update: Dict):
        if "callback_query" in update:
            self.__on_callback(bot, update["callback_query"])
            return
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        if not text or chat_id is None:
            return
        if self._allowed_chats and chat_id not in self._allowed_chats:
            return

        command, _, rest = text.partition(" ")
        command = command.split("@", 1)[0].lower() if command.startswith("/") else ""
        if command in ("/help", "/start"):
            self.__reply(bot, chat_id, self.__help_text(), msg.get("message_id"))
            return
        if command == "/stop" or (not command and text in self.STOP_WORDS):
            self.__stop(bot, msg, quiet=command != "/stop")
            return
        if command == "/mp":
            req = parse(rest, allow_bare_id=True)
            if not req:
                self.__reply(bot, chat_id, "没识别出来 😵 例：/mp tmdb 12345 S01E05 缺集", msg.get("message_id"))
                return
        elif command or self._trigger != "auto":
            return
        else:
            req = None if is_chatter(text) else parse(text, allow_bare_id=self._allow_bare_id)
            if not req:
                return
        self.__handle_request(bot, msg, req)

    @staticmethod
    def __help_text() -> str:
        return ("在群里直接说就行，例如：\n"
                "• tmdb 12345 缺第5集\n"
                "• tmdb 12345 S02E05 资源有问题\n"
                "• https://www.themoviedb.org/tv/12345 第二季 3-5集 花屏\n"
                "• /mp 12345 第3、7集\n"
                "• tmdb 550 电影 资源有问题（或 tmdb movie 550）\n"
                "• tmdb 12345 第1-3季 / tmdb 12345 全部季（一次求多季）\n"
                "• /stop 或「停止求片」：停掉还没处理的季\n\n"
                "没写季的话，单季剧自动识别，多季剧会让你选；没写集数又查不到剧集会按电影搜。")

    # ---------------- 求片流程 ----------------

    def __gc(self):
        now = time.time()
        with self._jobs_lock:
            for k in [k for k, j in self._jobs.items() if now - j.created > self.JOB_TTL]:
                self._jobs.pop(k, None)

    def __handle_request(self, bot: TelegramBot, msg: Dict, req: Request):
        self.__gc()
        chat_id = msg["chat"]["id"]
        reply_to = msg.get("message_id")
        if len(req.episodes) > self._max_episodes:
            self.__reply(bot, chat_id, f"一次最多处理 {self._max_episodes} 集，拆开发吧～", reply_to)
            return
        if req.multi_season:
            self.__handle_multi_season(bot, msg, req)
            return
        user = msg.get("from") or {}
        job = Job(req=req, user_id=user.get("id", 0),
                  user_name=user.get("first_name") or user.get("username") or str(user.get("id", "")),
                  chat_id=chat_id, reply_to=reply_to or 0)
        jid = secrets.token_hex(4)
        with self._jobs_lock:
            self._jobs[jid] = job

        if not req.is_movie and req.season is None:
            try:
                seasons = [s.season_number for s in (TmdbChain().tmdb_seasons(tmdbid=req.tmdbid) or [])
                           if (s.season_number or 0) > 0]
            except Exception as e:
                if req.episodes:
                    self.__reply(bot, chat_id, f"❌ 查询 TMDB 季信息失败：{html.escape(str(e))}", reply_to)
                    return
                logger.warning(f"TG群组智能补集：查询 TMDB {req.tmdbid} 季信息失败，按电影处理：{e}")
                seasons = []
            if not seasons and not req.episodes:
                # 没写集数又查不到剧集，多半是电影
                req.is_movie = True
            elif not seasons:
                self.__reply(bot, chat_id, f"❌ TMDB {req.tmdbid} 没查到剧集信息，ID 是不是错了？"
                                           f"电影的话不用写集数，或者发 tmdb movie {req.tmdbid}", reply_to)
                return
            elif len(seasons) == 1:
                req.season = seasons[0]
            else:
                rows = [[button(f"S{n:02d}", f"s|{jid}|{n}") for n in seasons[i:i + 5]]
                        for i in range(0, len(seasons), 5)]
                sent = bot.send(chat_id, f"这部剧有 {len(seasons)} 季，是哪一季？", reply_to=reply_to, buttons=rows)
                job.message_id = sent["message_id"]
                self.__expire_job(job, pending=True)
                return

        sent = bot.send(chat_id, f"🔍 识别到：{req.describe()}，正在搜索…", reply_to=reply_to)
        job.message_id = sent["message_id"]
        self.__search(bot, jid)

    STOP_WORDS = {"停止", "停止求片", "取消求片", "别下了", "停止下载"}

    def __stop(self, bot: TelegramBot, msg: Dict, quiet: bool = False):
        """停止口令：停掉自己还没处理的季和还在等点按钮的求片；管理员停掉本群所有人的。已推送的下载不动。"""
        chat_id, reply_to = msg["chat"]["id"], msg.get("message_id")
        uid = (msg.get("from") or {}).get("id", 0)
        is_admin = uid in self._admin_ids

        def mine(owner: int, chat: int) -> bool:
            return chat == chat_id and (is_admin or owner == uid)

        left = 0
        for batch in list(self._batches):
            if not batch.get("stopped") and mine(batch["user_id"], batch["chat_id"]):
                batch["stopped"] = True
                left += len(batch.get("pending") or [])
        with self._jobs_lock:
            waiting = [(jid, job) for jid, job in self._jobs.items()
                       if mine(job.user_id, job.chat_id) and job.message_id
                       and any(c and ep not in job.done for ep, c in job.choices.items())]
            for jid, _ in waiting:
                self._jobs.pop(jid, None)
        for _, job in waiting:
            bot.edit(job.chat_id, job.message_id, f"⏹ 已停止：{job.req.describe()}")
            self.__expire_job(job)
            self.__record(job, "已停止")
        if not left and not waiting:
            if not quiet:  # 群里随口说「停止」时没东西可停就不打扰
                self.__reply(bot, chat_id, "现在没有可以停止的求片（已推送的下载不会撤回）", reply_to)
            return
        parts = []
        if left:
            parts.append(f"还没处理的 {left} 季不再搜索")
        if waiting:
            parts.append(f"{len(waiting)} 个等待选择的求片已取消")
        self.__reply(bot, chat_id, f"⏹ 已停止：{'，'.join(parts)}。已推送的下载不会撤回。", reply_to)

    def __handle_multi_season(self, bot: TelegramBot, msg: Dict, req: Request):
        """一次求多季：逐季按整季请求处理，每季各自查库、检查积分、搜索下载、入库提醒和结算。"""
        chat_id, reply_to = msg["chat"]["id"], msg.get("message_id")
        try:
            tmdb_seasons = [s.season_number for s in (TmdbChain().tmdb_seasons(tmdbid=req.tmdbid) or [])
                            if (s.season_number or 0) > 0]
        except Exception as e:
            self.__reply(bot, chat_id, f"❌ 查询 TMDB 季信息失败：{html.escape(str(e))}", reply_to)
            return
        if not tmdb_seasons:
            self.__reply(bot, chat_id, f"❌ TMDB {req.tmdbid} 没查到剧集信息，ID 是不是错了？", reply_to)
            return
        seasons = sorted(tmdb_seasons) if req.all_seasons else [n for n in req.seasons if n in tmdb_seasons]
        missing = [] if req.all_seasons else [n for n in req.seasons if n not in tmdb_seasons]
        if not seasons:
            self.__reply(bot, chat_id, f"❌ 这部剧只有 {', '.join(f'S{n:02d}' for n in sorted(tmdb_seasons))}，"
                                       f"没有你说的那几季", reply_to)
            return
        if len(seasons) > self._max_seasons:
            self.__reply(bot, chat_id, f"这部剧有 {len(seasons)} 季，一次最多求 {self._max_seasons} 季，"
                                       f"拆开发吧，例如：tmdb {req.tmdbid} 第1-{self._max_seasons}季", reply_to)
            return
        note = f"（TMDB 上没有 {', '.join(f'S{n:02d}' for n in missing)}，已跳过）" if missing else ""
        self.__reply(bot, chat_id, f"📚 识别到：TMDB {req.tmdbid} 共 {len(seasons)} 季"
                                   f"（{', '.join(f'S{n:02d}' for n in seasons)}），逐季处理…{note}", reply_to)
        user = msg.get("from") or {}
        batch: Dict[str, Any] = {"free_used": 0, "committed": 0.0, "stopped": False,
                                 "user_id": user.get("id", 0), "chat_id": chat_id, "pending": list(seasons)}
        self._batches.append(batch)
        try:
            self.__run_seasons(bot, msg, req, batch)
        finally:
            if batch in self._batches:
                self._batches.remove(batch)

    def __run_seasons(self, bot: TelegramBot, msg: Dict, req: Request, batch: Dict[str, Any]):
        chat_id, reply_to = msg["chat"]["id"], msg.get("message_id")
        user = msg.get("from") or {}
        while batch["pending"]:
            if batch["stopped"]:
                return
            n = batch["pending"].pop(0)
            sub = Request(tmdbid=req.tmdbid, season=n, episodes=[], intent=req.intent)
            job = Job(req=sub, user_id=user.get("id", 0),
                      user_name=user.get("first_name") or user.get("username") or str(user.get("id", "")),
                      chat_id=chat_id, reply_to=reply_to or 0, batch=batch)
            jid = secrets.token_hex(4)
            with self._jobs_lock:
                self._jobs[jid] = job
            sent = bot.send(chat_id, f"🔍 {sub.describe()}，正在搜索…", reply_to=reply_to)
            job.message_id = sent["message_id"]
            self.__search(bot, jid)

    def __search(self, bot: TelegramBot, jid: str):
        job = self._jobs.get(jid)
        if not job:
            return
        req = job.req
        in_library, lib_note = self.__check_library(job)
        if in_library:
            bot.edit(job.chat_id, job.message_id, in_library)
            self.__expire_job(job)
            self.__record(job, "已在库里")
            return
        refuse = self.__check_credits(job)
        if refuse:
            bot.edit(job.chat_id, job.message_id, refuse)
            self.__expire_job(job)
            self.__record(job, "积分不足" if "积分不足" in refuse else "未通过积分检查")
            return
        try:
            with self._search_lock:
                results = SearchChain().search_by_id(
                    media_source=MediaSource.TMDB,
                    media_id=str(req.tmdbid),
                    mtype=MediaType.MOVIE if req.is_movie else MediaType.TV,
                    season=None if req.is_movie else req.season,
                ) or []
        except Exception as e:
            logger.error(f"TG群组智能补集：搜索 {req.describe()} 失败：{e}", exc_info=True)
            bot.edit(job.chat_id, job.message_id, f"❌ 搜索失败：{html.escape(str(e))}")
            self.__expire_job(job)
            return
        if not results:
            bot.edit(job.chat_id, job.message_id, f"😢 {req.describe()} 没搜到任何资源")
            self.__expire_job(job)
            self.__record(job, "没搜到资源")
            return

        media = results[0].media_info
        if media:
            job.title = f"{media.title} ({media.year})" if media.year else (media.title or "")
        season_eps = len(((getattr(media, "seasons", None) or {}).get(req.season) or [])) or None
        for ep in req.episodes or [0]:
            job.choices[ep] = pick(results, req.season, ep or None, allow_pack=self._allow_pack,
                                   min_seeders=self._min_seeders, low_seeders=self._low_seeders,
                                   limit=self._candidates, season_eps=season_eps)
            self.__log_candidates(req, ep, job.choices[ep], season_eps)

        note = ""
        if self._auto_download:
            note = self.__download_all(job)
        self.__render(bot, jid, "\n".join(n for n in (lib_note, note) if n))

    def __check_library(self, job: Job) -> Tuple[str, str]:
        """求片 / 缺集先查媒体库：全在库里返回 (回复, "")；部分在库里只留缺的集，返回 ("", 说明)。
        报资源有问题的不查，直接重下；查询出错时不拦，照常搜索。"""
        req = job.req
        if not self._check_library or req.intent == "replace":
            return "", ""
        mtype = MediaType.MOVIE if req.is_movie else MediaType.TV
        try:
            media = self.chain.recognize_media(mtype=mtype, media_source=MediaSource.TMDB,
                                               media_id=str(req.tmdbid))
            exist = self.chain.media_exists(mediainfo=media) if media else None
        except Exception as e:
            logger.warning(f"TG群组智能补集：查询媒体库失败，照常搜索 {req.describe()}：{e}")
            return "", ""
        if not media or not exist:
            return "", ""
        job.title = f"{media.title} ({media.year})" if media.year else (media.title or "")
        title = f"<b>{html.escape(job.title)}</b>"
        tip = "\n如果是资源有问题（花屏、没字幕、音画不同步等），请带上具体问题再发一次，会重新下载别的版本。"
        if req.is_movie:
            return f"📚 {title} 已经在库里了，可以直接去看。{tip}", ""
        have = set((exist.seasons or {}).get(req.season) or [])
        if not have:
            return "", ""
        wanted = req.episodes or sorted((getattr(media, "seasons", None) or {}).get(req.season) or [])
        if not wanted:
            return "", ""
        exists = [e for e in wanted if e in have]
        missing = [e for e in wanted if e not in have]
        season = f"S{req.season:02d}" if req.season else ""
        if not missing:
            what = " ".join(self.__ep_label(e) for e in exists) if req.episodes else "整季"
            return f"📚 {title} {season} {what} 已经在库里了，可以直接去看。{tip}", ""
        if not exists:
            return "", ""
        req.episodes = missing
        shown = exists if len(exists) <= 8 else exists[:8]
        more = f" 等 {len(exists)} 集" if len(exists) > 8 else ""
        return "", f"📚 {' '.join(self.__ep_label(e) for e in shown)}{more} 已在库里，只下缺的 {len(missing)} 集"

    def __log_candidates(self, req: Request, ep: int, cands: list, season_eps: Optional[int]):
        if not cands:
            logger.info(f"TG群组智能补集：{req.describe()} {self.__ep_label(ep)} 没有合适的候选")
            return
        lines = []
        for i, c in enumerate(cands, 1):
            t = c.torrent_info
            size = episode_size(c, ep or None, season_eps)
            dvf = getattr(t, "downloadvolumefactor", None)
            lines.append(f"  {i}. 做种 {getattr(t, 'seeders', 0) or 0}"
                         f" | 每集 {f'{size / 1024 ** 3:.2f}G' if size else '?'}"
                         f" | {'免费' if dvf == 0 else '不免费' if dvf is None or dvf >= 1 else f'{int(dvf * 100)}%'}"
                         f" | 规则等级 {getattr(t, 'pri_order', 0) or 0} | {getattr(t, 'title', '')}")
        logger.info(f"TG群组智能补集：{req.describe()} {self.__ep_label(ep)} 候选排序：\n" + "\n".join(lines))

    def __download_all(self, job: Job) -> str:
        """每集按排序依次尝试，第一个失败就换下一个候选。"""
        errors = []
        for ep, cands in job.choices.items():
            last_err = None
            for cand in cands:
                if ep in job.done:  # 可能已被前面某个包一并覆盖
                    break
                last_err = self.__download(job, ep, cand)
                if not last_err:
                    break
            if ep not in job.done and last_err:
                errors.append(f"❌ {self.__ep_label(ep)} 下载失败：{html.escape(last_err)}")
        return "\n".join(errors)

    def __download(self, job: Job, ep: int, ctx) -> Optional[str]:
        """下载一个候选。整季包/多集包只勾选本次请求里它覆盖的集，避免整包下载。"""
        title = ctx.torrent_info.title
        pending = [e for e in job.choices if e and e not in job.done]
        eps = episodes_of(ctx)
        if ep:
            covered = {e for e in pending if eps is None or e in eps} | {ep}
            # 单集种子不用选集；包才需要
            select = covered if eps is None or len(eps) > 1 else None
        else:
            covered, select = {0}, None
        try:
            did, error = DownloadChain().download_single(
                context=ctx,
                episodes=select,
                downloader=self._downloader or None,
                save_path=self._save_path or None,
                username=f"TG:{job.user_name}",
                source=self.plugin_name,
                return_detail=True,
            )
        except Exception as e:
            logger.error(f"TG群组智能补集：添加下载失败：{e}", exc_info=True)
            return str(e)
        if not did:
            return error or "添加下载失败"
        if select:
            self.__ensure_selected(str(did), sorted(select), title)
        for e in covered:
            job.done[e] = title
        self.__track(job, str(did), sorted(e for e in covered if e))
        self.__record(job, "已下载", title, sorted(e for e in covered if e))
        logger.info(f"TG群组智能补集：{job.user_name} 请求 {job.req.describe()}，已推送 {title}")
        return None

    def __ensure_selected(self, torrent_hash: str, episodes: List[int], title: str):
        """种子之前就在下载器里时，MoviePilot 不会改它的文件勾选，这里把本次要的集重新勾上并开始下载。"""
        try:
            from app.sdk.services import DownloaderHelper
            services = DownloaderHelper().get_services() or {}
        except Exception as e:
            logger.warning(f"TG群组智能补集：获取下载器失败，无法检查 {title} 的文件勾选：{e}")
            return
        if self._downloader and self._downloader in services:
            services = {self._downloader: services[self._downloader]}
        for name, service in services.items():
            inst = getattr(service, "instance", None)
            if not inst or getattr(service, "type", None) != "qbittorrent":
                continue
            try:
                files = inst.get_files(torrent_hash)
            except Exception:
                files = None
            if not files:
                continue
            ids = files_to_enable(files, episodes)
            if ids:
                ok = inst.set_files(torrent_hash=torrent_hash, file_ids=ids, priority=1)
                inst.start_torrents(ids=torrent_hash)
                logger.info(f"TG群组智能补集：{title} 已在下载器 {name} 中，"
                            f"{'已重新勾选' if ok else '重新勾选失败'} {len(ids)} 个文件并开始下载")
            return

    @staticmethod
    def __ep_label(ep: int) -> str:
        return "资源" if ep == 0 else f"E{ep:02d}"

    def __render(self, bot: TelegramBot, jid: str, note: str = ""):
        job = self._jobs.get(jid)
        if not job:
            return
        req = job.req
        if req.intent == "replace":
            action = "🛠 资源有问题 → 重新下载"
        else:
            action = "📥 求片 → 下载" if req.is_movie else "📥 缺集 → 补下载"
        lines = [f"<b>{html.escape(job.title)}</b>  {req.describe()}", action, ""]
        rows = []
        for ep, cands in job.choices.items():
            label = self.__ep_label(ep)
            if ep in job.done:
                lines.append(f"✅ <b>{label}</b> 已推送下载：<code>{html.escape(clean_title(job.done[ep]))}</code>")
                continue
            if not cands:
                lines.append(f"❌ <b>{label}</b> 没有合适的资源")
                continue
            lines.append(f"<b>{label}</b>")
            for i, c in enumerate(cands, 1):
                lines.append(f"  {i}. {html.escape(brief(c))}")
            rows.append([button(f"{label} 选{i}", f"d|{jid}|{ep}|{i - 1}") for i in range(1, len(cands) + 1)])
        pending = [ep for ep, c in job.choices.items() if c and ep not in job.done]
        if len(pending) > 1:
            rows.append([button("⚡ 全部用第1个", f"a|{jid}")])
        if pending:
            rows.append([button("✖ 取消", f"x|{jid}")])
        if note:
            lines += ["", note]
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "…"
        bot.edit(job.chat_id, job.message_id, text, rows)
        # 还有按钮没点完就等任务过期再删
        self.__expire_job(job, pending=bool(rows))

    def __on_callback(self, bot: TelegramBot, q: Dict):
        parts = (q.get("data") or "").split("|")
        jid = parts[1] if len(parts) > 1 else ""
        job = self._jobs.get(jid)
        if not job:
            bot.answer(q["id"], "任务已过期，请重新发送", alert=True)
            return
        uid = (q.get("from") or {}).get("id")
        if uid != job.user_id and uid not in self._admin_ids:
            bot.answer(q["id"], "只有发起人或管理员可以操作", alert=True)
            return

        kind = parts[0]
        if kind == "s" and len(parts) == 3:
            job.req.season = int(parts[2])
            bot.answer(q["id"])
            bot.edit(job.chat_id, job.message_id, f"🔍 识别到：{job.req.describe()}，正在搜索…")
            self.__search(bot, jid)
        elif kind == "d" and len(parts) == 4:
            ep, idx = int(parts[2]), int(parts[3])
            cands = job.choices.get(ep) or []
            if ep in job.done or idx >= len(cands):
                bot.answer(q["id"], "已处理")
                return
            bot.answer(q["id"], "正在添加下载…")
            err = self.__download(job, ep, cands[idx])
            self.__render(bot, jid, f"❌ {self.__ep_label(ep)} 下载失败：{html.escape(err)}" if err else "")
        elif kind == "a":
            bot.answer(q["id"], "正在添加下载…")
            self.__render(bot, jid, self.__download_all(job))
        elif kind == "x":
            with self._jobs_lock:
                self._jobs.pop(jid, None)
            bot.answer(q["id"], "已取消")
            bot.edit(job.chat_id, job.message_id, f"已取消：{job.req.describe()}")
            self.__expire_job(job)
            self.__record(job, "已取消")

    # ---------------- 自动删除群消息 ----------------

    def __reply(self, bot: TelegramBot, chat_id: int, text: str, reply_to: Optional[int]):
        """一次性回复（帮助、识别失败等），到时间连同触发它的消息一起删。"""
        sent = bot.send(chat_id, text, reply_to=reply_to)
        self.__expire(chat_id, (sent or {}).get("message_id"), self._auto_delete)
        if self._delete_request:
            self.__expire(chat_id, reply_to, self._auto_delete)

    def __expire_job(self, job: Job, pending: bool = False):
        """求片处理结束（或还在等按钮）时安排删除机器人的消息和原求片消息。"""
        if not self._auto_delete:
            return
        minutes = max(self._auto_delete, self.JOB_TTL // 60) if pending else self._auto_delete
        self.__expire(job.chat_id, job.message_id, minutes)
        if self._delete_request:
            self.__expire(job.chat_id, job.reply_to, minutes)

    def __expire(self, chat_id: int, message_id: Optional[int], minutes: int):
        if not minutes or not message_id:
            return
        with self._data_lock:
            deletes = self.get_data("deletes") or {}
            deletes[f"{chat_id}:{message_id}"] = time.time() + minutes * 60
            self.save_data("deletes", deletes)

    def __purge(self, bot: TelegramBot, now: Optional[float] = None):
        now = now or time.time()
        with self._data_lock:
            deletes = self.get_data("deletes") or {}
            due = [k for k, at in deletes.items() if at <= now]
            if not due:
                return
            for k in due:
                deletes.pop(k, None)
            self.save_data("deletes", deletes)
        for k in due:
            chat_id, _, message_id = k.rpartition(":")
            bot.delete(int(chat_id), int(message_id))

    ARRIVAL_POLL_SECONDS = 300

    def __cleaner(self, bot: TelegramBot, stop: threading.Event):
        last_poll = 0.0
        while not stop.wait(20):
            try:
                self.__purge(bot)
            except Exception as e:
                logger.warning(f"TG群组智能补集：自动删除消息出错：{e}")
            if time.time() - last_poll >= self.ARRIVAL_POLL_SECONDS:
                last_poll = time.time()
                try:
                    self.__poll_arrivals()
                except Exception as e:
                    logger.warning(f"TG群组智能补集：检查入库记录出错：{e}")

    # ---------------- PMS 积分 ----------------

    def __billable(self, job: Job) -> bool:
        if not (self._pms_enabled and self._pms_url and self._pms_token):
            return False
        if job.req.intent == "replace":  # 报错重下不收费
            return False
        return not (self._admin_free and job.user_id in self._admin_ids)

    def __price(self, is_movie: bool) -> Tuple[str, float, int]:
        """(额度类型, 价格, 每月免费数)"""
        if is_movie:
            return "movie", self._movie_price, self._free_movies
        return "season", self._season_price, self._free_seasons

    @staticmethod
    def __kind_name(kind: str) -> str:
        return "电影" if kind == "movie" else "剧集一季"

    def __usage(self) -> Usage:
        return Usage(self.get_data("pms_usage"))

    def __check_credits(self, job: Job) -> Optional[str]:
        """求片前检查：有免费额度或积分够就放行，否则返回拒绝理由。真正扣费在入库后。"""
        if not self.__billable(job):
            return None
        req = job.req
        kind, price, quota = self.__price(req.is_movie)
        usage = self.__usage()
        if price <= 0 or usage.is_paid(job.user_id, charge_key(req.tmdbid, req.is_movie, req.season)):
            return None
        batch = job.batch if job.batch is not None else {"free_used": 0, "committed": 0.0}
        if usage.left(job.user_id, kind, quota) > batch["free_used"]:
            batch["free_used"] += 1
            return None
        try:
            balance = PmsClient(self._pms_url, self._pms_token).balance(job.user_id)
        except PmsUserNotFound:
            return "❌ 你还没有在 PMS 绑定账号，绑定后才能求片"
        except PmsError as e:
            logger.error(f"TG群组智能补集：查询积分失败：{e}")
            return "❌ 积分系统暂时连不上，稍后再试"
        if balance < batch["committed"] + price:
            if batch["committed"]:
                return (f"❌ 积分不足：前面几季入库后共需 {batch['committed']:g} 积分，"
                        f"再求这一季还要 {price:g}，你当前 {balance:g}")
            return (f"❌ 积分不足：本月免费的{self.__kind_name(kind)}额度已用完，"
                    f"求{self.__kind_name(kind)}需要 {price:g} 积分，你当前 {balance:g}")
        batch["committed"] += price
        return None

    def __settle(self, rec: Dict) -> str:
        """入库后结算一次：先用免费额度，用完扣 PMS 积分。返回附在提醒里的说明。"""
        if not rec.get("billable") or rec.get("settled"):
            return ""
        uid = rec["user_id"]
        key = charge_key(rec["tmdbid"], rec["is_movie"], rec.get("season"))
        kind, price, quota = self.__price(rec["is_movie"])
        name = self.__kind_name(kind)
        with self._data_lock:
            usage = self.__usage()
            if price <= 0 or usage.is_paid(uid, key):
                rec["settled"] = True
                return ""
            if usage.left(uid, kind, quota) > 0:
                usage.consume(uid, kind)
                usage.mark_paid(uid, key)
                self.save_data("pms_usage", usage.to_dict())
                rec["settled"] = True
                return f"（使用本月免费{name}额度，还剩 {usage.left(uid, kind, quota)} 次）"
        reason = f"求片 {rec.get('title') or ''} {rec.get('label') or ''}".strip()
        try:
            res = PmsClient(self._pms_url, self._pms_token).charge(uid, price, f"mp:{uid}:{key}", reason)
        except PmsInsufficient as e:
            rec["settled"] = True
            logger.warning(f"TG群组智能补集：{rec['user_name']} 入库扣费失败（积分不足）：{e}")
            return f"（积分不足，{price:g} 积分没能扣除）"
        except PmsError as e:
            logger.error(f"TG群组智能补集：{rec['user_name']} 入库扣费失败：{e}")
            return "（积分暂未扣除，下一集入库时会重试）" if not rec["is_movie"] else "（积分暂未扣除）"
        rec["settled"] = True
        with self._data_lock:
            usage = self.__usage()
            usage.mark_paid(uid, key)
            self.save_data("pms_usage", usage.to_dict())
        return f"（扣除 {price:g} 积分，余额 {res.get('balance', 0):g}）"

    # ---------------- 入库提醒 ----------------

    def __track(self, job: Job, download_hash: str, episodes: List[int]):
        """记下这个下载是谁求的，等 MoviePilot 整理完成后提醒他。"""
        if not (self._notify_arrival or self.__billable(job)) or not download_hash:
            return
        req = job.req
        with self._data_lock:
            arrivals = self.__live_arrivals()
            arrivals[download_hash.lower()] = {
                "user_id": job.user_id, "user_name": job.user_name, "chat_id": job.chat_id,
                "reply_to": job.reply_to, "tmdbid": req.tmdbid, "is_movie": req.is_movie,
                "season": req.season, "episodes": episodes, "arrived": [], "title": job.title,
                "label": "" if req.is_movie else f"S{req.season or 0:02d}",
                "billable": self.__billable(job), "settled": False, "created": time.time(),
            }
            self.save_data("arrivals", arrivals)

    def __live_arrivals(self) -> Dict[str, Dict]:
        ttl = self._pending_days * 86400
        now = time.time()
        return {h: r for h, r in (self.get_data("arrivals") or {}).items() if now - r.get("created", 0) < ttl}

    @staticmethod
    def __field(data, name):
        if data is None:
            return None
        if isinstance(data, dict):
            return data.get(name)
        return getattr(data, name, None)

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self.get_state():
            return
        data = getattr(event, "event_data", None)
        download_hash = str(self.__field(data, "download_hash") or "").lower()
        if not download_hash:
            return
        transferinfo = self.__field(data, "transferinfo")
        if transferinfo is not None and self.__field(transferinfo, "success") is False:
            return
        meta = self.__field(data, "meta")
        meta_eps = set(self.__field(meta, "episode_list") or [])
        begin, end = self.__field(meta, "begin_episode"), self.__field(meta, "end_episode")
        if not meta_eps and begin:
            meta_eps = set(range(begin, (end or begin) + 1))
        self.__transferred(download_hash, meta_eps)

    @staticmethod
    def __history_episodes(text: Optional[str]) -> Set[int]:
        """整理历史里的集数字段，如 E05、E11-E12。"""
        eps: Set[int] = set()
        for a, b in re.findall(r"E(\d{1,4})(?:-E(\d{1,4}))?", text or "", re.I):
            eps.update(range(int(a), int(b or a) + 1))
        return eps

    @staticmethod
    def __history_time(text: Optional[str]) -> float:
        try:
            return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except (TypeError, ValueError):
            return 0.0

    def __poll_arrivals(self):
        """MoviePilot 的整理完成事件可能积压很久才投递，这里定时直接查整理历史兜底。"""
        if not self.get_state():
            return
        with self._data_lock:
            pending = {h: r.get("created", 0) for h, r in self.__live_arrivals().items()}
        if not pending:
            return
        from app.sdk.queries import list_transfer_history
        for download_hash, created in pending.items():
            page = list_transfer_history({"download_hash": download_hash, "status": True}, {"count": 100})
            # 同一个种子以前整理过的记录不算，只看求片之后的
            items = [i for i in (getattr(page, "items", None) or [])
                     if self.__history_time(getattr(i, "date", None)) >= created - 60]
            if not items:
                continue
            eps: Set[int] = set()
            for item in items:
                eps |= self.__history_episodes(getattr(item, "episodes", None))
            self.__transferred(download_hash, eps)
        if self._wait_library:
            self.__poll_library()

    def __transferred(self, download_hash: str, eps: Set[int]):
        """MoviePilot 整理完成（本地）。等媒体库模式下只记下来，等媒体库里能看到再提醒。"""
        if not self._wait_library:
            self.__arrived(download_hash, eps)
            return
        with self._data_lock:
            arrivals = self.__live_arrivals()
            rec = arrivals.get(download_hash)
            if not rec:
                return
            before = (rec.get("transferred_any"), sorted(rec.get("transferred") or []))
            rec["transferred_any"] = True
            rec["transferred"] = sorted(set(rec.get("transferred") or []) | set(eps))
            if before != (True, rec["transferred"]):
                logger.info(f"TG群组智能补集：{download_hash[:8]} 已整理"
                            f"{' ' + ','.join(f'E{e:02d}' for e in rec['transferred']) if rec['transferred'] else ''}"
                            f"，等媒体库里出现后再提醒")
                self.save_data("arrivals", arrivals)

    def __poll_library(self):
        """已整理的请求去媒体库里查，上传完、媒体库里能看到了才算入库。"""
        with self._data_lock:
            pending = {h: dict(r) for h, r in self.__live_arrivals().items() if r.get("transferred_any")}
        media_cache: Dict[Tuple[int, bool], Any] = {}
        for download_hash, rec in pending.items():
            key = (rec["tmdbid"], bool(rec["is_movie"]))
            try:
                if key not in media_cache:
                    media_cache[key] = self.chain.recognize_media(
                        mtype=MediaType.MOVIE if rec["is_movie"] else MediaType.TV,
                        media_source=MediaSource.TMDB, media_id=str(rec["tmdbid"]))
                media = media_cache[key]
                exist = self.chain.media_exists(mediainfo=media) if media else None
            except Exception as e:
                logger.warning(f"TG群组智能补集：查询媒体库失败（{rec.get('title') or rec['tmdbid']}）：{e}")
                continue
            if not exist:
                continue
            if rec["is_movie"]:
                self.__arrived(download_hash, set())
                continue
            have = set((getattr(exist, "seasons", None) or {}).get(rec.get("season")) or [])
            arrived = set(rec.get("arrived") or [])
            if rec["episodes"]:
                ready = (set(rec["episodes"]) - arrived) & have
                if ready:
                    self.__arrived(download_hash, ready)
            elif have:
                # 整季请求：整理过的集都在媒体库里了才提醒（认不出集数时媒体库有这季就算）
                done = set(rec.get("transferred") or [])
                if done <= have:
                    self.__arrived(download_hash, done)

    def __arrived(self, download_hash: str, meta_eps: Set[int]):
        with self._data_lock:
            arrivals = self.__live_arrivals()
            rec = arrivals.get(download_hash)
            if not rec:
                return
            wanted = set(rec["episodes"]) - set(rec["arrived"])
            # 认不出集数（或电影）时当作这次请求的都到了
            new = sorted(wanted & meta_eps) if (wanted and meta_eps) else sorted(wanted)
            if wanted and not new:
                return
            rec["arrived"] = sorted(set(rec["arrived"]) | set(new))
            if rec["arrived"] == sorted(rec["episodes"]):
                arrivals.pop(download_hash, None)
            self.save_data("arrivals", arrivals)

        note = self.__settle(rec)
        if rec.get("settled"):
            # 剧集还有没到的集：记下已结算，后面的集只提醒不再扣费
            with self._data_lock:
                cur = self.__live_arrivals()
                if download_hash in cur:
                    cur[download_hash]["settled"] = True
                    self.save_data("arrivals", cur)
        if self._notify_arrival:
            self.__notify(rec, new, note)

    def __notify(self, rec: Dict, episodes: List[int], note: str):
        bot = self._bot
        if not bot:
            return
        who = f'<a href="tg://user?id={rec["user_id"]}">{html.escape(rec["user_name"] or "你")}</a>'
        title = rec.get("title") or f"TMDB {rec['tmdbid']}"
        what = f"《{html.escape(title)}》"
        if not rec["is_movie"]:
            what += " " + (rec.get("label") or "")
            if episodes:
                what += " " + "、".join(f"E{e:02d}" for e in episodes)
        text = f"🎬 {who} 你求的 {what} 已入库，可以去看了！{note}"
        try:
            sent = bot.send(rec["chat_id"], text, reply_to=rec.get("reply_to") or None)
            self.__expire(rec["chat_id"], (sent or {}).get("message_id"), self._notice_delete)
        except Exception as e:
            logger.warning(f"TG群组智能补集：发送入库提醒失败：{e}")

    # ---------------- 历史 ----------------

    def __record(self, job: Job, status: str, torrent: str = "", episodes: Optional[List[int]] = None):
        history = self.get_data("history") or []
        history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user": job.user_name,
            "chat": job.chat_id,
            "request": job.req.describe(),
            "intent": "重下" if job.req.intent == "replace" else "缺集",
            "title": job.title,
            "episodes": ",".join(f"E{e:02d}" for e in episodes or []),
            "status": status,
            "torrent": torrent,
        })
        self.save_data("history", history[:self.HISTORY_LIMIT])

    # ---------------- 页面 ----------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def col(md, comp, **props):
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": comp, "props": props}]}

        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VRow", "content": [
                        col(4, "VSwitch", model="enabled", label="启用插件"),
                        col(4, "VSwitch", model="proxy", label="使用系统代理访问 Telegram"),
                        col(4, "VSwitch", model="auto_download", label="自动下载最优资源（关闭则需点按钮选择）"),
                        col(4, "VSwitch", model="check_library", label="求片前先查媒体库（已有的不下，报问题的照常重下）"),
                    ]},
                    {"component": "VRow", "content": [
                        col(12, "VTextField", model="token", label="机器人 Token",
                            placeholder="123456:ABC-xxxx",
                            hint="必须是单独的机器人，不能和 MoviePilot 自带 Telegram 通知用同一个", persistent_hint=True),
                    ]},
                    {"component": "VRow", "content": [
                        col(6, "VTextField", model="allowed_chats", label="允许的群组 ID",
                            placeholder="-1001234567890,-1009876543210", hint="逗号分隔，留空不限制", persistent_hint=True),
                        col(6, "VTextField", model="admin_ids", label="管理员 TG 用户 ID",
                            placeholder="123456789", hint="可以替任何人点按钮；留空则只有发起人能点", persistent_hint=True),
                    ]},
                    {"component": "VRow", "content": [
                        col(4, "VSelect", model="trigger", label="触发方式", items=[
                            {"title": "识别群里所有消息", "value": "auto"},
                            {"title": "只响应 /mp 命令", "value": "command"},
                        ]),
                        col(4, "VSwitch", model="allow_bare_id", label="允许不写 tmdb 的纯数字 ID"),
                        col(4, "VSwitch", model="allow_pack", label="没有单集时用整季包（只勾选需要的集）"),
                    ]},
                    {"component": "VRow", "content": [
                        col(4, "VTextField", model="candidates", label="每集候选数", type="number"),
                        col(4, "VTextField", model="min_seeders", label="最少做种数（低于直接不要）", type="number"),
                        col(4, "VTextField", model="max_episodes", label="单次最多集数", type="number"),
                        col(4, "VTextField", model="max_seasons", label="一次最多求几季", type="number"),
                    ]},
                    {"component": "VRow", "content": [
                        col(4, "VTextField", model="low_seeders", label="做种少于此数排到最后", type="number"),
                        {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [{
                            "component": "VAlert",
                            "props": {"type": "info", "variant": "tonal", "density": "compact", "text":
                                      "排序：做种太少的放最后 → MoviePilot 规则组画质 → 做种多的优先 → 免费优先 → 单集优先"},
                        }]},
                    ]},
                    {"component": "VRow", "content": [
                        col(6, "VTextField", model="downloader", label="下载器名称", placeholder="留空用默认下载器"),
                        col(6, "VTextField", model="save_path", label="保存路径", placeholder="留空按目录设置自动选择"),
                    ]},
                    {"component": "VRow", "content": [
                        col(4, "VSwitch", model="notify_arrival", label="入库后在群里 @ 求片的人"),
                        col(4, "VSwitch", model="wait_library", label="等媒体库里能看到再提醒（上传 / 生成 strm 之后）"),
                        col(4, "VTextField", model="pending_days", label="等待入库天数", type="number",
                            hint="超过这么多天还没入库就不再提醒、不扣积分", persistent_hint=True),
                    ]},
                    {"component": "VRow", "content": [
                        col(4, "VTextField", model="auto_delete", label="处理完几分钟后删除机器人消息", type="number",
                            hint="0 = 不删", persistent_hint=True),
                        col(4, "VSwitch", model="delete_request", label="同时删除群友的求片消息",
                            hint="需要机器人是群管理员并有删除消息权限", persistent_hint=True),
                        col(4, "VTextField", model="notice_delete", label="入库提醒几分钟后删除", type="number",
                            hint="0 = 保留", persistent_hint=True),
                    ]},
                    {"component": "VDivider"},
                    {"component": "VRow", "content": [
                        col(4, "VSwitch", model="pms_enabled", label="对接 PMS 积分"),
                        col(4, "VSwitch", model="admin_free", label="管理员求片免费"),
                    ]},
                    {"component": "VRow", "content": [
                        col(6, "VTextField", model="pms_url", label="PMS 地址", placeholder="https://pms.example.com"),
                        col(6, "VTextField", model="pms_token", label="PMS 接口密钥",
                            hint="PMSManageBot 里的 MP_CREDITS_API_TOKEN", persistent_hint=True),
                    ]},
                    {"component": "VRow", "content": [
                        col(3, "VTextField", model="movie_price", label="电影 积分/部", type="number"),
                        col(3, "VTextField", model="season_price", label="剧集 积分/季", type="number"),
                        col(3, "VTextField", model="free_movies", label="每月免费电影（部）", type="number"),
                        col(3, "VTextField", model="free_seasons", label="每月免费剧集（季）", type="number"),
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [{
                            "component": "VAlert",
                            "props": {"type": "info", "variant": "tonal", "density": "compact", "text":
                                      "求片前检查免费额度和积分，不够直接拒绝；入库后才用掉额度或扣积分，没入库不收。"
                                      "同一个人求同一部电影 / 同一季只收一次；「资源有问题」重下不收费。"},
                        }]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [{
                            "component": "VAlert",
                            "props": {"type": "info", "variant": "tonal", "text":
                                      "在 @BotFather 用 /setprivacy 把机器人设为 Disable，否则群里只能收到命令。"
                                      "群成员发「tmdb 12345 缺第5集」「tmdb 12345 S02E05 资源有问题」即可。"
                                      "资源有问题时重新下载，旧文件是否被替换取决于 MoviePilot 整理的覆盖模式。"},
                        }]},
                    ]},
                ],
            }
        ], {
            "enabled": False,
            "token": "",
            "proxy": True,
            "auto_download": True,
            "max_seasons": 10,
            "check_library": True,
            "allowed_chats": "",
            "admin_ids": "",
            "trigger": "auto",
            "allow_bare_id": True,
            "allow_pack": True,
            "candidates": 3,
            "min_seeders": 1,
            "low_seeders": 3,
            "config_version": 2,
            "max_episodes": 12,
            "downloader": "",
            "save_path": "",
            "notify_arrival": True,
            "wait_library": True,
            "auto_delete": 3,
            "delete_request": False,
            "notice_delete": 0,
            "pending_days": 7,
            "pms_enabled": False,
            "pms_url": "",
            "pms_token": "",
            "admin_free": True,
            "movie_price": 10,
            "season_price": 20,
            "free_movies": 3,
            "free_seasons": 2,
        }

    def get_page(self) -> List[dict]:
        history = self.get_data("history") or []
        if not history:
            return [{"component": "div", "text": "暂无记录", "props": {"class": "text-center"}}]
        heads = ["时间", "用户", "请求", "类型", "剧集", "状态", "种子"]
        rows = [{
            "component": "tr",
            "content": [{"component": "td", "text": h.get(k) or ""} for k in
                        ("time", "user", "request", "intent", "title", "status", "torrent")],
        } for h in history[:100]]
        return [{
            "component": "VTable",
            "props": {"hover": True, "density": "compact"},
            "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": h} for h in heads]}]},
                {"component": "tbody", "content": rows},
            ],
        }]
