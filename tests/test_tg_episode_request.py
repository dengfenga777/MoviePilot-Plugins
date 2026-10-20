"""TG群组智能补集：消息识别、候选挑选和完整求片流程（MoviePilot 用桩替换）。"""
import importlib
import sys
import threading
import time
import types
import unittest
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from types import SimpleNamespace as NS

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins.v3" / "tgepisoderequest"


def _stub_moviepilot():
    """注册插件用到的最小 app.* 模块。"""
    calls = SimpleCalls()

    class MediaSource(str, Enum):
        TMDB = "themoviedb"

    class MediaType(Enum):
        MOVIE = "电影"
        TV = "电视剧"

    class EventType(Enum):
        TransferComplete = "transfer.complete"

    class _EventManager:
        @staticmethod
        def register(_etype):
            return lambda fn: fn

    class _Chain:
        @staticmethod
        def recognize_media(**kw):
            return NS(title="某剧", year="2026", seasons={1: list(range(1, 11))})

        @staticmethod
        def media_exists(mediainfo):
            calls.lib_checks += 1
            if isinstance(calls.library, Exception):
                raise calls.library
            return None if calls.library is None else NS(seasons=calls.library)

    class _PluginBase:
        def __init__(self):
            self._data = {}
            self.chain = _Chain()

        def get_data(self, key):
            return self._data.get(key)

        def save_data(self, key, value):
            self._data[key] = value

        def update_config(self, config):
            calls.saved_config = dict(config)

    class SearchChain:
        def search_by_id(self, **kw):
            calls.search.append(kw)
            return calls.results

    class DownloadChain:
        def download_single(self, **kw):
            calls.download.append(kw)
            title = kw["context"].torrent_info.title
            if title in calls.fail:
                return None, "种子下载失败"
            return "hash1", None

    class TmdbChain:
        def tmdb_seasons(self, tmdbid):
            return [NS(season_number=n) for n in calls.seasons]

    mods = {
        "app": {}, "app.chain": {}, "app.chain.search": {},
        "app.chain.download": {"DownloadChain": DownloadChain},
        "app.chain.search.facade": {"SearchChain": SearchChain},
        "app.chain.tmdb": {"TmdbChain": TmdbChain},
        "app.plugins": {"_PluginBase": _PluginBase},
        "app.schemas": {},
        "app.schemas.types": {"MediaSource": MediaSource, "MediaType": MediaType, "EventType": EventType},
        "app.sdk": {}, "app.sdk.config": {"settings": NS(PROXY_HOST=None)},
        "app.sdk.events": {"eventmanager": _EventManager(), "Event": object},
        "app.sdk.logging": {"logger": NS(info=print, warning=print, error=lambda *a, **k: print(*a))},
    }
    for name, attrs in mods.items():
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
    return calls


class SimpleCalls:
    def __init__(self):
        self.search, self.download, self.results, self.seasons = [], [], [], [1]
        self.fail, self.saved_config = set(), None
        self.library, self.lib_checks = None, 0  # 媒体库里已有的 {季: [集]}，None = 库里没有


def ctx(title, season, b=None, e=None, seeders=5, pri=0, free=True, gb=2):
    return NS(meta_info=NS(begin_season=season, begin_episode=b, end_episode=e, episode_list=[]),
              torrent_info=NS(title=title, seeders=seeders, site_name="X", size=gb * 1024 ** 3,
                              downloadvolumefactor=0 if free else 1, pri_order=pri),
              media_info=NS(title="某剧", year="2026"))


class FakeBot:
    def __init__(self):
        self.sent, self.edits, self.answers, self.deleted = [], [], [], []

    def send(self, chat_id, text, reply_to=None, buttons=None):
        self.sent.append((text, buttons))
        return {"message_id": 100 + len(self.sent)}

    def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append((text, buttons))

    def delete(self, chat_id, message_id):
        self.deleted.append(message_id)
        return True

    def answer(self, cid, text="", alert=False):
        self.answers.append(text)


calls = _stub_moviepilot()
spec = importlib.util.spec_from_file_location("tgepisoderequest", PLUGIN_DIR / "__init__.py",
                                              submodule_search_locations=[str(PLUGIN_DIR)])
plugin_mod = importlib.util.module_from_spec(spec)
sys.modules["tgepisoderequest"] = plugin_mod
spec.loader.exec_module(plugin_mod)
parse = importlib.import_module("tgepisoderequest.tgparse").parse
picker = importlib.import_module("tgepisoderequest.picker")
pick = picker.pick


class ParseTest(unittest.TestCase):
    def check(self, text, tmdbid, season, eps, intent="missing"):
        r = parse(text)
        self.assertIsNotNone(r, text)
        self.assertEqual((r.tmdbid, r.season, r.episodes, r.intent), (tmdbid, season, eps, intent), text)

    def test_cases(self):
        self.check("tmdb 12345 缺第5集", 12345, None, [5])
        self.check("tmdbid:12345 S02E05 资源有问题", 12345, 2, [5], "replace")
        self.check("https://www.themoviedb.org/tv/94997 第二季 3-5集 花屏", 94997, 2, [3, 4, 5], "replace")
        self.check("94997 第3、7、9集没有", 94997, None, [3, 7, 9])
        self.check("tmdb=12345 s1 e3 e4 字幕错了", 12345, 1, [3, 4], "replace")
        self.check("TMDB：12345 第十二集 音画不同步", 12345, None, [12], "replace")
        self.check("tmdb 12345 S01E03-E06", 12345, 1, [3, 4, 5, 6])
        self.check("求 tmdb 12345 第1季 第10到12集", 12345, 1, [10, 11, 12])
        self.check("tmdb12345 S03 第二十一集 打不开", 12345, 3, [21], "replace")

    def test_no_trigger(self):
        self.assertIsNone(parse("今天吃了 300 块的火锅"))
        self.assertIsNone(parse("我有 2 个问题 123 和 456"))

    def test_multi_season(self):
        cases = {"tmdb 1399 全部季": (True, []), "tmdb 1399 全集 求": (True, []),
                 "tmdb 1399 第1-3季": (False, [1, 2, 3]), "tmdb 1399 1到3季": (False, [1, 2, 3]),
                 "tmdb 1399 S01-S03": (False, [1, 2, 3]), "tmdb 1399 第一季到第三季": (False, [1, 2, 3]),
                 "tmdb 1399 第1、3、5季": (False, [1, 3, 5])}
        for text, (all_, seasons) in cases.items():
            r = parse(text)
            self.assertTrue(r.multi_season, text)
            self.assertEqual((r.all_seasons, r.seasons, r.season, r.episodes), (all_, seasons, None, []), text)
        for text in ["tmdb 1399 第2季", "tmdb 1399 第2季 全季", "tmdb 1399 S01-S03 第5集", "tmdb 550 电影 全集",
                     "tmdb 1399 第3-5集"]:
            self.assertFalse(parse(text).multi_season, text)
        self.check("tmdb 1399 S01-S03 第5集", 1399, 1, [5])

    def test_bare_id_strict(self):
        # 纯数字只认开头、后面不跟单位、不是分辨率、整句够短
        for text in ["2016 年的那部没有", "2016年那部有问题", "500 G 没有了", "300块 没有", "1080 有问题",
                     "1080p 花屏", "2160 p 有问题", "3.5 有问题", "15% 没有", "我 2000 年就看过 有问题",
                     "今天 300 块的东西没有", "12345 缺第5集" + "，" * 40]:
            self.assertIsNone(parse(text), text)
        self.check("12345 缺第5集", 12345, None, [5])
        self.check("12345 S01E05 有问题", 12345, 1, [5], "replace")
        self.check("94997 第二季 3-5集 花屏", 94997, 2, [3, 4, 5], "replace")
        self.check("tmdb 1080 第3集", 1080, None, [3])     # 写了 tmdb 就不受限制

    def test_movie(self):
        for text in ["https://www.themoviedb.org/movie/550 资源有问题", "tmdb 550 电影 资源有问题",
                     "tmdb movie 550 花屏", "movie/550 缺", "550 电影 有问题"]:
            r = parse(text)
            self.assertTrue(r and r.is_movie, text)
            self.assertEqual((r.tmdbid, r.season, r.episodes), (550, None, []), text)
        self.assertFalse(parse("tmdb 550 资源有问题").is_movie)       # 没说是电影，先按剧集查
        self.assertFalse(parse("tmdb 123 第5集 电影院版").is_movie)   # 带集数就是剧
        self.assertFalse(parse("tmdb tv 123 电影").is_movie)
        self.assertIsNone(parse("我看了 300 部电影"))


class PickTest(unittest.TestCase):
    @staticmethod
    def titles(cs):
        return [c.torrent_info.title for c in cs]

    def test_order_and_filters(self):
        rs = [ctx("pack", 2), ctx("S02E01-E08", 2, 1, 8), ctx("S02E05", 2, 5), ctx("S01E05", 1, 5),
              ctx("S02E05 dead", 2, 5, seeders=0)]
        self.assertEqual([c.torrent_info.title for c in pick(rs, 2, 5)], ["S02E05", "S02E01-E08", "pack"])
        self.assertEqual([c.torrent_info.title for c in pick(rs, 2, 5, allow_pack=False)],
                         ["S02E05", "S02E01-E08"])

    def test_quality_beats_kind(self):
        rs = [ctx("ep 1080p", 1, 5, seeders=50), ctx("pack 2160p", 1, seeders=50, pri=90)]
        self.assertEqual(self.titles(pick(rs, 1, 5)), ["pack 2160p", "ep 1080p"])

    def test_low_seeders_last_even_if_better_quality(self):
        rs = [ctx("2160p slow", 1, 5, seeders=2, pri=90), ctx("1080p fast", 1, 5, seeders=40, pri=10)]
        self.assertEqual(self.titles(pick(rs, 1, 5)), ["1080p fast", "2160p slow"])

    def test_seed_tier_ignores_free(self):
        rs = [ctx("paid 60", 1, 5, seeders=60, free=False), ctx("free 40", 1, 5, seeders=40),
              ctx("free 5", 1, 5, seeders=5)]
        # 60 和 40 同一档，不看免费 → 做种多的在前；5 做种低一档排后面
        self.assertEqual(self.titles(pick(rs, 1, 5)), ["paid 60", "free 40", "free 5"])


class SizeRankTest(unittest.TestCase):
    titles = staticmethod(PickTest.titles)

    def test_bigger_per_episode_wins_within_same_quality(self):
        rs = [ctx("small free busy", 1, 11, 12, seeders=80, gb=0.9),   # 两集包，每集 0.45G
              ctx("big paid", 1, 11, seeders=12, free=False, gb=3),
              ctx("big pack", 1, 11, 12, seeders=12, gb=6)]             # 两集包，每集 3G
        # 两个大的同一档、做种一样 → 单集优先；小的做种再多也排后面
        self.assertEqual(self.titles(pick(rs, 1, 11)), ["big paid", "big pack", "small free busy"])

    def test_quality_and_low_seeders_still_first(self):
        rs = [ctx("huge 1080p", 1, 5, seeders=40, pri=10, gb=12), ctx("2160p", 1, 5, seeders=40, pri=90, gb=3),
              ctx("huge dead", 1, 5, seeders=1, pri=90, gb=20)]
        self.assertEqual(self.titles(pick(rs, 1, 5)), ["2160p", "huge 1080p", "huge dead"])

    def test_similar_size_falls_back_to_seeders(self):
        rs = [ctx("2.2G free", 1, 5, seeders=40, gb=2.2), ctx("3.9G paid", 1, 5, seeders=45, free=False, gb=3.9)]
        self.assertEqual(self.titles(pick(rs, 1, 5)), ["3.9G paid", "2.2G free"])   # 同一档，免费不加分
        rs = [ctx("free busy", 1, 5, seeders=120, gb=2.2), ctx("paid", 1, 5, seeders=40, free=False, gb=2.4)]
        self.assertEqual(self.titles(pick(rs, 1, 5)), ["free busy", "paid"])          # 同一档，做种档位高的优先

    def test_files_to_enable(self):
        files = [{"id": 0, "name": "Show/Show.S01E10.2160p.mkv", "priority": 1},
                 {"id": 1, "name": "Show/Show.S01E11.2160p.mkv", "priority": 0},
                 {"id": 2, "name": "Show/Show.S01E12.2160p.mkv", "priority": 0},
                 {"id": 3, "name": "Show/Sample.mkv", "priority": 0},
                 {"id": 4, "name": "第11集.nfo", "priority": 0}]
        self.assertEqual(picker.files_to_enable(files, [11]), [1, 4])
        self.assertEqual(picker.files_to_enable(files, [10]), [])           # 已经勾选的不动
        self.assertEqual(picker.file_episodes("Show.S01E03-E04.mkv"), [3, 4])
        self.assertEqual(picker.file_episodes("Show.EP07.1080p.mkv"), [7])

    def test_season_pack_uses_season_episode_count(self):
        rs = [ctx("pack 30G", 1, gb=30), ctx("single 1G", 1, 5, gb=1)]
        self.assertEqual(self.titles(pick(rs, 1, 5, season_eps=10)), ["pack 30G", "single 1G"])
        self.assertEqual(self.titles(pick(rs, 1, 5)), ["single 1G", "pack 30G"])  # 不知道总集数就不比大小


class HideSiteTest(unittest.TestCase):
    def test_brief_has_no_site_or_group(self):
        c = ctx("Some Show 2026 S01E04 1080p WEB-DL H264 AAC 2Audios-FOOWEB", 1, 4)
        c.torrent_info.site_name = "FooSite"
        text = picker.brief(c)
        self.assertNotIn("FooSite", text)
        self.assertNotIn("FOOWEB", text)
        self.assertIn("2Audios", text)

    def test_clean_title_keeps_format_suffix(self):
        self.assertEqual(picker.clean_title("Show.S01E01.1080p.WEB-DL"), "Show.S01E01.1080p.WEB-DL")
        self.assertEqual(picker.clean_title("Show.Ep03.HDTV.1080p.H264-BARGRP"), "Show.Ep03.HDTV.1080p.H264")
        self.assertEqual(picker.clean_title("Show S01E02 2160p@BAZWEB"), "Show S01E02 2160p")


class FlowTest(unittest.TestCase):
    def setUp(self):
        calls.search.clear()
        calls.download.clear()
        calls.seasons = [1]
        calls.library, calls.lib_checks = None, 0
        self.p = plugin_mod.TgEpisodeRequest()
        self.p.init_plugin({"enabled": False, "admin_ids": "999", "auto_download": False, "config_version": 2})
        self.bot = FakeBot()

    def msg(self, text, uid=1):
        return {"message": {"message_id": 1, "text": text, "chat": {"id": -100}, "from": {"id": uid, "username": "u"}}}

    def handle(self, update):
        self.p._TgEpisodeRequest__handle_update(self.bot, update)

    def click(self, data, uid=1):
        self.handle({"callback_query": {"id": "q", "data": data, "from": {"id": uid}}})

    def test_pack_selects_only_requested_episodes(self):
        calls.results = [ctx("S01 整季", 1)]
        self.handle(self.msg("tmdb 123 缺第5、6集"))
        self.assertEqual(calls.search[0]["season"], 1)
        text, buttons = self.bot.edits[-1]
        self.assertIn("E05", text)
        data = buttons[0][0]["callback_data"]
        self.click(data, uid=2)  # 非发起人
        self.assertIn("只有发起人或管理员可以操作", self.bot.answers)
        self.click(data)
        self.assertEqual(calls.download[0]["episodes"], {5, 6})
        self.assertEqual(len(calls.download), 1)  # 同一个包覆盖两集只下一次
        self.assertIn("已推送下载", self.bot.edits[-1][0])
        self.assertNotIn("[X]", "".join(t for t, _ in self.bot.edits))
        self.assertEqual(self.p.get_data("history")[0]["status"], "已下载")

    def test_announcement_is_ignored(self):
        notice = ("📢 求片机器人使用说明\n群里直接发消息就行：\n"
                  "tmdb 94997 缺第5集\ntmdb 94997 S02E05\ntmdb movie 550\n有问题请 @ 管理员。")
        for text in [notice, "tmdb 123 缺第5集 tmdb 456 缺第6集", "tmdb 123 缺第5集" + "啊" * 120]:
            self.handle(self.msg(text))
        self.assertEqual((calls.search, self.bot.sent), ([], []))
        calls.results = [ctx("S01E05", 1, 5)]
        self.handle(self.msg("tmdb 123\n缺第5集"))            # 两行的正常求片照样识别
        self.assertEqual(len(calls.search), 1)
        self.handle(self.msg("/mp " + notice))                  # /mp 不受限制
        self.assertEqual(len(calls.search), 2)

    def test_existing_torrent_reselects_episode(self):
        class Qb:
            def __init__(self):
                self.set, self.started = [], []

            def get_files(self, tid):
                return [{"id": 0, "name": "S/S.S01E11.mkv", "priority": 0},
                        {"id": 1, "name": "S/S.S01E12.mkv", "priority": 0}]

            def set_files(self, **kw):
                self.set.append(kw)
                return True

            def start_torrents(self, ids):
                self.started.append(ids)

        qb = Qb()
        mod = types.ModuleType("app.sdk.services")
        mod.DownloaderHelper = lambda: NS(get_services=lambda: {"qb": NS(instance=qb, type="qbittorrent")})
        sys.modules["app.sdk.services"] = mod
        try:
            calls.results = [ctx("S01E11-E12", 1, 11, 12)]
            self.p.init_plugin({"enabled": False, "auto_download": True, "config_version": 2})
            self.handle(self.msg("tmdb 123 缺第11集"))
        finally:
            del sys.modules["app.sdk.services"]
        self.assertEqual(qb.set, [{"torrent_hash": "hash1", "file_ids": [0], "priority": 1}])
        self.assertEqual(qb.started, ["hash1"])

    def test_library_all_present_skips_search(self):
        calls.library = {1: [5, 6]}
        self.handle(self.msg("tmdb 123 缺第5、6集"))
        self.assertEqual(calls.search, [])
        self.assertIn("已经在库里", self.bot.edits[-1][0])
        self.assertEqual(self.p.get_data("history")[0]["status"], "已在库里")

    def test_library_partial_downloads_only_missing(self):
        calls.library = {1: [5]}
        calls.results = [ctx("S01E05", 1, 5), ctx("S01E06", 1, 6)]
        self.handle(self.msg("tmdb 123 缺第5、6集"))
        self.assertEqual(list(self.p._jobs.values())[-1].req.episodes, [6])
        text = self.bot.edits[-1][0]
        self.assertIn("E05 已在库里，只下缺的 1 集", text)
        self.assertNotIn("S01E05", text)

    def test_library_whole_season_and_movie(self):
        calls.library = {1: list(range(1, 11))}
        self.handle(self.msg("tmdb 123 第1季 缺"))
        self.assertIn("整季 已经在库里", self.bot.edits[-1][0])
        calls.library = {}
        self.handle(self.msg("tmdb movie 550"))
        self.assertIn("已经在库里", self.bot.edits[-1][0])
        self.assertEqual(calls.search, [])

    def test_library_whole_season_partial(self):
        calls.library = {1: list(range(1, 9))}
        calls.results = [ctx("S01 整季", 1)]
        self.handle(self.msg("tmdb 123 第1季 缺"))
        self.assertEqual(list(self.p._jobs.values())[-1].req.episodes, [9, 10])

    def test_replace_skips_library(self):
        calls.library = {1: [5]}
        calls.results = [ctx("S01E05", 1, 5)]
        for text in ["tmdb 123 第5集 花屏", "tmdb 123 第5集 没字幕"]:
            self.handle(self.msg(text))
        self.assertEqual((calls.lib_checks, len(calls.search)), (0, 2))

    def test_library_error_does_not_block(self):
        calls.library = RuntimeError("媒体服务器连不上")
        calls.results = [ctx("S01E05", 1, 5)]
        self.handle(self.msg("tmdb 123 缺第5集"))
        self.assertEqual(len(calls.search), 1)

    def test_multi_season_asks_then_searches(self):
        calls.seasons = [1, 2, 3]
        calls.results = [ctx("S02E05", 2, 5)]
        self.handle(self.msg("tmdb 123 第5集 花屏"))
        _, buttons = self.bot.sent[-1]
        self.assertEqual([b["text"] for b in buttons[0]], ["S01", "S02", "S03"])
        self.click(buttons[0][1]["callback_data"], uid=999)  # 管理员
        self.assertEqual(calls.search[0]["season"], 2)
        self.assertIn("资源有问题", self.bot.edits[-1][0])

    def test_auto_download_is_default_and_migrated(self):
        p = plugin_mod.TgEpisodeRequest()
        p.init_plugin({"enabled": False})
        self.assertTrue(p._auto_download)
        calls.saved_config = None
        p.init_plugin({"enabled": False, "auto_download": False})  # 旧版本保存的配置
        self.assertTrue(p._auto_download)
        self.assertEqual(calls.saved_config["config_version"], 2)

    def test_auto_falls_back_to_next_candidate(self):
        self.p._auto_download = True
        calls.results = [ctx("S01E05 best", 1, 5, seeders=50), ctx("S01E05 second", 1, 5, seeders=20)]
        calls.fail = {"S01E05 best"}
        self.handle(self.msg("tmdb 123 缺第5集"))
        self.assertEqual([d["context"].torrent_info.title for d in calls.download],
                         ["S01E05 best", "S01E05 second"])
        text, buttons = self.bot.edits[-1]
        self.assertIn("已推送下载", text)
        self.assertNotIn("下载失败", text)
        self.assertFalse(buttons)
        calls.fail = set()

    def test_single_episode_torrent_no_selection(self):
        calls.results = [ctx("S01E05", 1, 5)]
        self.p._auto_download = True
        self.handle(self.msg("tmdb 123 S01E05"))
        self.assertIsNone(calls.download[0]["episodes"])

    def test_command_mode_ignores_chat(self):
        self.p._trigger = "command"
        self.handle(self.msg("tmdb 123 缺第5集"))
        self.assertEqual(self.bot.sent, [])
        calls.results = [ctx("S01E05", 1, 5)]
        self.handle(self.msg("/mp@bot tmdb 123 缺第5集"))
        self.assertEqual(len(calls.search), 1)

    def test_movie_flow(self):
        calls.results = [ctx("Some Movie 2160p", None, seeders=30)]
        self.p._auto_download = True
        self.handle(self.msg("tmdb 550 电影 资源有问题"))
        self.assertEqual(calls.search[0]["mtype"], plugin_mod.MediaType.MOVIE)
        self.assertIsNone(calls.search[0]["season"])
        self.assertIsNone(calls.download[0]["episodes"])
        self.assertIn("已推送下载", self.bot.edits[-1][0])

    def test_no_seasons_without_episodes_falls_back_to_movie(self):
        calls.seasons = []
        calls.results = [ctx("Some Movie 1080p", None)]
        self.handle(self.msg("tmdb 550 资源有问题"))
        self.assertEqual(calls.search[0]["mtype"], plugin_mod.MediaType.MOVIE)

    def test_no_seasons_with_episodes_reports_error(self):
        calls.seasons = []
        self.handle(self.msg("tmdb 550 缺第5集"))
        self.assertEqual(calls.search, [])
        self.assertIn("没查到剧集信息", self.bot.sent[-1][0])

    def test_allowed_chats(self):
        self.p._allowed_chats = {-1}
        self.handle(self.msg("tmdb 123 缺第5集"))
        self.assertEqual(self.bot.sent, [])


billing = importlib.import_module("tgepisoderequest.billing")


class FakePms:
    def __init__(self, balances):
        self.balances, self.charges, self.refs = dict(balances), [], set()

    def __call__(self, url, token):
        return self

    def balance(self, tg_id):
        if tg_id not in self.balances:
            raise billing.PmsUserNotFound("用户不存在")
        return self.balances[tg_id]

    def charge(self, tg_id, amount, ref, reason=""):
        if ref in self.refs:
            return {"duplicate": True, "balance": self.balances[tg_id]}
        if self.balances[tg_id] < amount:
            raise billing.PmsInsufficient("积分不足", self.balances[tg_id])
        self.refs.add(ref)
        self.balances[tg_id] -= amount
        self.charges.append((tg_id, amount, ref))
        return {"duplicate": False, "balance": self.balances[tg_id]}


def arrived(hash_="hash1", eps=()):
    eps = list(eps)
    return NS(event_data={"download_hash": hash_.upper(), "transferinfo": NS(success=True),
                          "meta": NS(episode_list=eps, begin_episode=None, end_episode=None)})


class WaitLibraryTest(unittest.TestCase):
    """默认：本地整理完成还不算，媒体库里能看到（上传、生成 strm 之后）才提醒和扣费。"""

    def setUp(self):
        calls.search.clear()
        calls.download.clear()
        calls.seasons = [1]
        calls.library, calls.lib_checks = None, 0
        self.p = plugin_mod.TgEpisodeRequest()
        self.p.init_plugin({"enabled": False, "config_version": 2, "check_library": False})
        self.p.get_state = lambda: True
        self.bot = FakeBot()
        self.p._bot = self.bot

    def handle(self, text, uid=1):
        self.p._TgEpisodeRequest__handle_update(self.bot, {"message": {
            "message_id": 7, "text": text, "chat": {"id": -100}, "from": {"id": uid, "first_name": f"U{uid}"}}})

    def poll(self, history=()):
        mod = types.ModuleType("app.sdk.queries")
        mod.list_transfer_history = lambda filters, page: NS(items=[
            NS(date=d, episodes=e) for h, d, e in history if h == filters["download_hash"]])
        sys.modules["app.sdk.queries"] = mod
        try:
            self.p._TgEpisodeRequest__poll_arrivals()
        finally:
            del sys.modules["app.sdk.queries"]

    def notices(self):
        return [t for t, _ in self.bot.sent if "已入库" in t]

    def test_episode_waits_for_library(self):
        calls.results = [ctx("S01 整季", 1)]
        self.handle("tmdb 123 缺第5、6集")
        self.p.on_transfer_complete(arrived(eps=[5]))   # 本地整理完，还没上传
        self.poll()
        self.assertEqual(self.notices(), [])
        calls.library = {1: [5]}                         # 上传完，媒体库里有 E05 了
        self.poll()
        self.assertEqual(len(self.notices()), 1)
        self.assertIn("E05", self.notices()[-1])
        self.assertNotIn("E06", self.notices()[-1])
        self.poll()
        self.assertEqual(len(self.notices()), 1)         # 不重复
        calls.library = {1: [5, 6]}
        self.poll()
        self.assertIn("E06", self.notices()[-1])
        self.assertEqual(self.p.get_data("arrivals"), {})

    def test_movie_and_history_path(self):
        calls.results = [ctx("Movie A 2160p", None, seeders=30)]
        self.handle("tmdb movie 550 求")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.poll([("hash1", now, None)])                 # 整理历史查到了，媒体库还没有
        self.assertEqual(self.notices(), [])
        calls.library = {}
        self.poll()
        self.assertEqual(len(self.notices()), 1)

    def test_replace_needs_transfer_first(self):
        calls.library = {1: [5]}                          # 旧文件本来就在库里
        calls.results = [ctx("S01E05", 1, 5)]
        self.handle("tmdb 123 第5集 花屏")
        self.poll()
        self.assertEqual(self.notices(), [])              # 新版本还没整理，不能提醒
        self.p.on_transfer_complete(arrived(eps=[5]))
        self.poll()
        self.assertEqual(len(self.notices()), 1)


class MultiSeasonTest(unittest.TestCase):
    def setUp(self):
        calls.search.clear()
        calls.download.clear()
        calls.seasons = [1, 2, 3]
        calls.library, calls.lib_checks = None, 0
        self.pms = FakePms({1: 25})
        self._orig = plugin_mod.PmsClient
        plugin_mod.PmsClient = self.pms
        self.p = plugin_mod.TgEpisodeRequest()
        self.p.init_plugin({"enabled": False, "config_version": 2, "pms_enabled": True, "pms_url": "http://pms",
                            "pms_token": "k", "season_price": 20, "free_seasons": 1, "wait_library": False})
        self.bot = FakeBot()

    def tearDown(self):
        plugin_mod.PmsClient = self._orig

    def handle(self, text):
        self.p._TgEpisodeRequest__handle_update(self.bot, {"message": {
            "message_id": 7, "text": text, "chat": {"id": -100}, "from": {"id": 1, "first_name": "U1"}}})

    def test_all_seasons_one_job_each_with_batch_credits(self):
        calls.results = [ctx("S01 整季", 1), ctx("S02 整季", 2), ctx("S03 整季", 3)]
        self.handle("tmdb 1399 全部季")
        self.assertEqual([c["season"] for c in calls.search], [1, 2])   # S03 积分不够，不搜
        self.assertIn("共 3 季", self.bot.sent[0][0])
        # 1 季免费 + 1 季 20 积分；余额 25 不够第三季
        self.assertIn("积分不足", self.bot.edits[-1][0])
        self.assertIn("共需 20 积分", self.bot.edits[-1][0])
        self.assertEqual(len(calls.download), 2)

    def test_skips_seasons_in_library_and_unknown(self):
        calls.library = {1: list(range(1, 11))}
        calls.results = [ctx("S02 整季", 2), ctx("S03 整季", 3)]
        self.handle("tmdb 1399 第1-4季")
        self.assertIn("没有 S04", self.bot.sent[0][0])
        self.assertEqual([c["season"] for c in calls.search], [2, 3])  # S01 在库里；S02 免费，S03 用积分
        self.assertTrue(any("已经在库里" in t for t, _ in self.bot.edits))

    def test_too_many_seasons(self):
        calls.seasons = list(range(1, 13))
        self.handle("tmdb 1399 全部季")
        self.assertIn("一次最多求 10 季", self.bot.sent[-1][0])
        self.assertEqual(calls.search, [])


class BillingTest(unittest.TestCase):
    def setUp(self):
        calls.search.clear()
        calls.download.clear()
        calls.seasons = [1]
        calls.library, calls.lib_checks = None, 0
        self.pms = FakePms({1: 50, 2: 5})
        self._orig = plugin_mod.PmsClient
        plugin_mod.PmsClient = self.pms
        self.p = plugin_mod.TgEpisodeRequest()
        self.p.init_plugin({"enabled": False, "admin_ids": "999", "config_version": 2,
                            "pms_enabled": True, "pms_url": "http://pms", "pms_token": "k",
                            "movie_price": 10, "season_price": 20, "free_movies": 1, "free_seasons": 0,
                            "wait_library": False})
        self.p.get_state = lambda: True
        self.bot = FakeBot()
        self.p._bot = self.bot

    def tearDown(self):
        plugin_mod.PmsClient = self._orig

    def msg(self, text, uid=1):
        return {"message": {"message_id": 7, "text": text, "chat": {"id": -100},
                            "from": {"id": uid, "first_name": f"U{uid}"}}}

    def handle(self, text, uid=1):
        self.p._TgEpisodeRequest__handle_update(self.bot, self.msg(text, uid))

    def test_movie_uses_free_quota_then_needs_credits(self):
        calls.results = [ctx("Movie A 2160p", None, seeders=30)]
        self.handle("tmdb movie 550 求", uid=2)
        self.assertEqual(len(calls.download), 1)
        self.p.on_transfer_complete(arrived())
        text = self.bot.sent[-1][0]
        self.assertIn("已入库", text)
        self.assertIn("tg://user?id=2", text)
        self.assertIn("免费电影额度", text)
        self.assertEqual(self.pms.charges, [])
        # 额度用完，余额 5 < 10：拒绝且不搜索
        self.handle("tmdb movie 551 求", uid=2)
        self.assertEqual(len(calls.search), 1)
        self.assertIn("积分不足", self.bot.edits[-1][0])
        # 同一部电影再求不再收费也不拦
        self.handle("tmdb movie 550 求", uid=2)
        self.assertEqual(len(calls.search), 2)

    def poll(self, history):
        mod = types.ModuleType("app.sdk.queries")
        mod.list_transfer_history = lambda filters, page: NS(items=[
            NS(date=d, episodes=e) for h, d, e in history if h == filters["download_hash"]])
        sys.modules["app.sdk.queries"] = mod
        try:
            self.p._TgEpisodeRequest__poll_arrivals()
        finally:
            del sys.modules["app.sdk.queries"]

    def test_poll_finds_arrival_when_event_is_late(self):
        calls.results = [ctx("S01 整季", 1)]
        self.handle("tmdb 123 缺第5、6集")
        now = datetime.now()
        old = (now - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
        new = now.strftime("%Y-%m-%d %H:%M:%S")
        sent = len(self.bot.sent)
        self.poll([("hash1", old, "E06")])                     # 以前整理过的不算
        self.assertEqual(len(self.bot.sent), sent)
        self.poll([("hash1", old, "E06"), ("hash1", new, "E05")])
        self.assertIn("E05", self.bot.sent[-1][0])
        self.assertNotIn("E06", self.bot.sent[-1][0])
        self.assertEqual(len(self.pms.charges), 1)
        self.poll([("hash1", new, "E05")])                     # 重复轮询不重复提醒
        self.p.on_transfer_complete(arrived(eps=[5]))           # 积压的事件后到也不重复
        self.assertEqual(len(self.bot.sent), sent + 1)
        self.poll([("hash1", new, "E05-E06")])
        self.assertIn("E06", self.bot.sent[-1][0])
        self.assertEqual(self.p.get_data("arrivals"), {})

    def test_poll_movie(self):
        calls.results = [ctx("Movie A 2160p", None, seeders=30)]
        self.handle("tmdb movie 550 求", uid=2)
        self.poll([("hash1", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), None)])
        self.assertIn("已入库", self.bot.sent[-1][0])

    def test_season_charged_once_on_first_arrival(self):
        calls.results = [ctx("S01 整季", 1)]
        self.handle("tmdb 123 缺第5、6集")
        self.assertEqual(calls.download[0]["episodes"], {5, 6})
        self.p.on_transfer_complete(arrived(eps=[5]))
        self.assertEqual(self.pms.charges, [(1, 20, "mp:1:tv:123:s1")])
        self.assertIn("E05", self.bot.sent[-1][0])
        self.assertIn("扣除 20 积分，余额 30", self.bot.sent[-1][0])
        self.p.on_transfer_complete(arrived(eps=[6]))
        self.assertEqual(len(self.pms.charges), 1)
        self.assertIn("E06", self.bot.sent[-1][0])
        self.assertNotIn("积分", self.bot.sent[-1][0])
        n = len(self.bot.sent)
        self.p.on_transfer_complete(arrived(eps=[6]))  # 重复事件
        self.assertEqual(len(self.bot.sent), n)
        # 同一季再求：不再收费
        self.handle("tmdb 123 缺第7集")
        self.p.on_transfer_complete(arrived(eps=[7]))
        self.assertEqual(len(self.pms.charges), 1)

    def test_replace_and_admin_are_free(self):
        calls.results = [ctx("S01E05", 1, 5)]
        self.handle("tmdb 123 S01E05 资源有问题", uid=2)  # 余额只有 5
        self.handle("tmdb 124 S01E05 缺", uid=999)        # 管理员，PMS 里都没有
        self.assertEqual(len(calls.download), 2)
        self.p.on_transfer_complete(arrived(eps=[5]))
        self.assertEqual(self.pms.charges, [])

    def test_unbound_user_rejected(self):
        calls.results = [ctx("S01E05", 1, 5)]
        self.handle("tmdb 123 S01E05 缺", uid=3)
        self.assertEqual(calls.search, [])
        self.assertIn("绑定", self.bot.edits[-1][0])

    def test_unknown_hash_ignored(self):
        self.p.on_transfer_complete(arrived("other"))
        self.assertEqual(self.bot.sent, [])


class AutoDeleteTest(unittest.TestCase):
    def setUp(self):
        calls.search.clear()
        calls.download.clear()
        calls.seasons = [1]
        calls.library, calls.lib_checks = None, 0
        self.p = plugin_mod.TgEpisodeRequest()
        self.p.init_plugin({"enabled": False, "config_version": 2, "auto_delete": 3, "delete_request": True})
        self.bot = FakeBot()

    def handle(self, text):
        self.p._TgEpisodeRequest__handle_update(self.bot, {"message": {
            "message_id": 7, "text": text, "chat": {"id": -100}, "from": {"id": 1, "first_name": "U"}}})

    def purge(self, after_minutes):
        self.p._TgEpisodeRequest__purge(self.bot, time.time() + after_minutes * 60)

    def test_deletes_result_and_request_after_minutes(self):
        calls.results = [ctx("S01E05", 1, 5)]
        self.handle("tmdb 123 缺第5集")
        self.assertIn("已推送下载", self.bot.edits[-1][0])
        self.purge(2)
        self.assertEqual(self.bot.deleted, [])
        self.purge(4)
        self.assertEqual(sorted(self.bot.deleted), [7, 101])
        self.purge(10)
        self.assertEqual(len(self.bot.deleted), 2)  # 只删一次

    def test_pending_buttons_wait_for_job_expiry(self):
        self.p._auto_download = False
        calls.results = [ctx("S01E05", 1, 5)]
        self.handle("tmdb 123 缺第5集")
        self.purge(10)
        self.assertEqual(self.bot.deleted, [])
        self.purge(61)
        self.assertIn(101, self.bot.deleted)

    def test_disabled(self):
        self.p._auto_delete = 0
        calls.results = []
        self.handle("tmdb 123 缺第5集")
        self.purge(999)
        self.assertEqual(self.bot.deleted, [])


class ReloadConflictTest(unittest.TestCase):
    def test_startup_conflict_is_quiet(self):
        tg = importlib.import_module("tgepisoderequest.telegram")
        logs = []
        orig = plugin_mod.logger
        plugin_mod.logger = NS(info=lambda m: logs.append(("info", m)), warning=lambda m: logs.append(("warn", m)),
                               error=lambda m, **k: logs.append(("error", m)))

        class Bot:
            def get_me(self):
                return {"username": "b"}

            def poll(self, stop, on_update, on_error, on_ok):
                on_error(tg.TelegramConflict("409"))
                on_ok()

        try:
            p = plugin_mod.TgEpisodeRequest()
            p._TgEpisodeRequest__run(Bot(), threading.Event())
        finally:
            plugin_mod.logger = orig
        self.assertFalse([m for lvl, m in logs if lvl == "error"])

    def test_poll_reports_ok(self):
        tg = importlib.import_module("tgepisoderequest.telegram")
        bot = tg.TelegramBot.__new__(tg.TelegramBot)
        stop, seen = threading.Event(), []
        replies = iter([tg.TelegramConflict("409"), []])

        def call(method, **kw):
            r = next(replies, None)
            if r is None:
                stop.set()
                return []
            if isinstance(r, Exception):
                raise r
            return r
        bot.call = call
        stop_wait = stop.wait
        stop.wait = lambda t: None
        bot.poll(stop, lambda u: None, lambda e: seen.append("err"), lambda: seen.append("ok"))
        stop.wait = stop_wait
        self.assertEqual(seen[:2], ["err", "ok"])


if __name__ == "__main__":
    unittest.main()
