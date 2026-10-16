import html
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
from app.schemas.types import MediaSource, MediaType
from app.sdk.config import settings
from app.sdk.logging import logger

from .picker import brief, episodes_of, pick
from .telegram import TelegramBot, TelegramConflict, button
from .tgparse import Request, parse


@dataclass
class Job:
    req: Request
    user_id: int
    user_name: str
    chat_id: int
    message_id: int = 0
    title: str = ""
    # 集数（0 = 整季 / 电影）-> 候选 Context
    choices: Dict[int, list] = field(default_factory=dict)
    # 集数 -> 已推送的种子标题
    done: Dict[int, str] = field(default_factory=dict)
    created: float = field(default_factory=time.time)


class TgEpisodeRequest(_PluginBase):
    # 插件名称
    plugin_name = "TG群组智能补集"
    # 插件描述
    plugin_desc = "群成员发 TMDB ID + 缺哪集 / 哪集有问题，自动识别后搜索并下载对应集数。"
    # 插件图标
    plugin_icon = "Telegram_A.png"
    # 插件版本
    plugin_version = "1.0.0"
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
    _jobs_lock = threading.Lock()
    _bot_username: str = ""

    # 配置
    _enabled: bool = False
    _token: str = ""
    _proxy: bool = True
    _allowed_chats: Set[int] = set()
    _admin_ids: Set[int] = set()
    _trigger: str = "auto"
    _auto_download: bool = False
    _allow_bare_id: bool = True
    _allow_pack: bool = True
    _min_seeders: int = 1
    _candidates: int = 3
    _max_episodes: int = 12
    _downloader: str = ""
    _save_path: str = ""

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = bool(config.get("enabled"))
            self._token = str(config.get("token") or "").strip()
            self._proxy = config.get("proxy", True)
            self._allowed_chats = self.__ids(config.get("allowed_chats"))
            self._admin_ids = self.__ids(config.get("admin_ids"))
            self._trigger = config.get("trigger") or "auto"
            self._auto_download = bool(config.get("auto_download"))
            self._allow_bare_id = config.get("allow_bare_id", True)
            self._allow_pack = config.get("allow_pack", True)
            self._min_seeders = self.__int(config.get("min_seeders"), 1)
            self._candidates = max(1, min(self.__int(config.get("candidates"), 3), 8))
            self._max_episodes = self.__int(config.get("max_episodes"), 12)
            self._downloader = str(config.get("downloader") or "").strip()
            self._save_path = str(config.get("save_path") or "").strip()

        self._jobs = {}
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

    # ---------------- Telegram 轮询 ----------------

    def __run(self, bot: TelegramBot, stop: threading.Event):
        try:
            me = bot.get_me()
            self._bot_username = me.get("username") or ""
            logger.info(f"TG群组智能补集：机器人 @{self._bot_username} 已启动")
        except Exception as e:
            logger.error(f"TG群组智能补集：连接 Telegram 失败，请检查 Token 和代理：{e}")
        last_error = [""]

        def on_error(e: Exception):
            msg = str(e)
            if msg == last_error[0]:
                return
            last_error[0] = msg
            if isinstance(e, TelegramConflict):
                logger.error("TG群组智能补集：这个 Token 正被其他程序使用（例如 MoviePilot 自带的 Telegram 通知）"
                             "或设置了 Webhook，请给本插件单独创建一个机器人")
            else:
                logger.warning(f"TG群组智能补集：获取消息失败：{e}")

        def on_update(update: Dict):
            last_error[0] = ""
            executor = self._executor
            if executor:
                executor.submit(self.__safe, self.__handle_update, bot, update)

        bot.poll(stop, on_update, on_error)

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
            bot.send(chat_id, self.__help_text(), reply_to=msg.get("message_id"))
            return
        if command == "/mp":
            req = parse(rest, allow_bare_id=True)
            if not req:
                bot.send(chat_id, "没识别出来 😵 例：/mp tmdb 12345 S01E05 缺集", reply_to=msg.get("message_id"))
                return
        elif command or self._trigger != "auto":
            return
        else:
            req = parse(text, allow_bare_id=self._allow_bare_id)
            if not req:
                return
        self.__handle_request(bot, msg, req)

    @staticmethod
    def __help_text() -> str:
        return ("在群里直接说就行，例如：\n"
                "• tmdb 12345 缺第5集\n"
                "• tmdb 12345 S02E05 资源有问题\n"
                "• https://www.themoviedb.org/tv/12345 第二季 3-5集 花屏\n"
                "• /mp 12345 第3、7集\n\n"
                "没写季的话，单季剧自动识别，多季剧会让你选。")

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
            bot.send(chat_id, f"一次最多处理 {self._max_episodes} 集，拆开发吧～", reply_to=reply_to)
            return
        user = msg.get("from") or {}
        job = Job(req=req, user_id=user.get("id", 0),
                  user_name=user.get("username") or user.get("first_name") or str(user.get("id", "")),
                  chat_id=chat_id)
        jid = secrets.token_hex(4)
        with self._jobs_lock:
            self._jobs[jid] = job

        if not req.is_movie and req.season is None:
            try:
                seasons = [s.season_number for s in (TmdbChain().tmdb_seasons(tmdbid=req.tmdbid) or [])
                           if (s.season_number or 0) > 0]
            except Exception as e:
                bot.send(chat_id, f"❌ 查询 TMDB 季信息失败：{html.escape(str(e))}", reply_to=reply_to)
                return
            if not seasons:
                bot.send(chat_id, f"❌ TMDB {req.tmdbid} 没查到剧集信息，是不是电影或者 ID 错了？", reply_to=reply_to)
                return
            if len(seasons) == 1:
                req.season = seasons[0]
            else:
                rows = [[button(f"S{n:02d}", f"s|{jid}|{n}") for n in seasons[i:i + 5]]
                        for i in range(0, len(seasons), 5)]
                sent = bot.send(chat_id, f"这部剧有 {len(seasons)} 季，是哪一季？", reply_to=reply_to, buttons=rows)
                job.message_id = sent["message_id"]
                return

        sent = bot.send(chat_id, f"🔍 识别到：{req.describe()}，正在搜索…", reply_to=reply_to)
        job.message_id = sent["message_id"]
        self.__search(bot, jid)

    def __search(self, bot: TelegramBot, jid: str):
        job = self._jobs.get(jid)
        if not job:
            return
        req = job.req
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
            return
        if not results:
            bot.edit(job.chat_id, job.message_id, f"😢 {req.describe()} 没搜到任何资源")
            self.__record(job, "没搜到资源")
            return

        media = results[0].media_info
        if media:
            job.title = f"{media.title} ({media.year})" if media.year else (media.title or "")
        for ep in req.episodes or [0]:
            job.choices[ep] = pick(results, req.season, ep or None, allow_pack=self._allow_pack,
                                   min_seeders=self._min_seeders, limit=self._candidates)

        note = ""
        if self._auto_download:
            note = self.__download_all(job)
        self.__render(bot, jid, note)

    def __download_all(self, job: Job) -> str:
        errors = []
        for ep, cands in job.choices.items():
            if cands and ep not in job.done:
                err = self.__download(job, ep, cands[0])
                if err:
                    errors.append(f"❌ {self.__ep_label(ep)} 下载失败：{html.escape(err)}")
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
        for e in covered:
            job.done[e] = title
        self.__record(job, "已下载", title, sorted(e for e in covered if e))
        logger.info(f"TG群组智能补集：{job.user_name} 请求 {job.req.describe()}，已推送 {title}")
        return None

    @staticmethod
    def __ep_label(ep: int) -> str:
        return "资源" if ep == 0 else f"E{ep:02d}"

    def __render(self, bot: TelegramBot, jid: str, note: str = ""):
        job = self._jobs.get(jid)
        if not job:
            return
        req = job.req
        action = "🛠 资源有问题 → 重新下载" if req.intent == "replace" else "📥 缺集 → 补下载"
        lines = [f"<b>{html.escape(job.title)}</b>  {req.describe()}", action, ""]
        rows = []
        for ep, cands in job.choices.items():
            label = self.__ep_label(ep)
            if ep in job.done:
                lines.append(f"✅ <b>{label}</b> 已推送下载：<code>{html.escape(job.done[ep])}</code>")
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
            self.__record(job, "已取消")

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
                        col(4, "VSwitch", model="auto_download", label="自动下载第一个候选（不用点按钮）"),
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
                        col(4, "VTextField", model="min_seeders", label="最少做种数", type="number"),
                        col(4, "VTextField", model="max_episodes", label="单次最多集数", type="number"),
                    ]},
                    {"component": "VRow", "content": [
                        col(6, "VTextField", model="downloader", label="下载器名称", placeholder="留空用默认下载器"),
                        col(6, "VTextField", model="save_path", label="保存路径", placeholder="留空按目录设置自动选择"),
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
            "auto_download": False,
            "allowed_chats": "",
            "admin_ids": "",
            "trigger": "auto",
            "allow_bare_id": True,
            "allow_pack": True,
            "candidates": 3,
            "min_seeders": 1,
            "max_episodes": 12,
            "downloader": "",
            "save_path": "",
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
