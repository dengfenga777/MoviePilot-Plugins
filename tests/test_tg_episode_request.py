"""TG群组智能补集：消息识别、候选挑选和完整求片流程（MoviePilot 用桩替换）。"""
import importlib
import sys
import types
import unittest
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

    class _PluginBase:
        def __init__(self):
            self._data = {}

        def get_data(self, key):
            return self._data.get(key)

        def save_data(self, key, value):
            self._data[key] = value

    class SearchChain:
        def search_by_id(self, **kw):
            calls.search.append(kw)
            return calls.results

    class DownloadChain:
        def download_single(self, **kw):
            calls.download.append(kw)
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
        "app.schemas": {}, "app.schemas.types": {"MediaSource": MediaSource, "MediaType": MediaType},
        "app.sdk": {}, "app.sdk.config": {"settings": NS(PROXY_HOST=None)},
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


def ctx(title, season, b=None, e=None, seeders=5):
    return NS(meta_info=NS(begin_season=season, begin_episode=b, end_episode=e, episode_list=[]),
              torrent_info=NS(title=title, seeders=seeders, site_name="X", size=2 * 1024 ** 3,
                              downloadvolumefactor=0),
              media_info=NS(title="某剧", year="2026"))


class FakeBot:
    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []

    def send(self, chat_id, text, reply_to=None, buttons=None):
        self.sent.append((text, buttons))
        return {"message_id": 100 + len(self.sent)}

    def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append((text, buttons))

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

    def test_movie(self):
        r = parse("https://www.themoviedb.org/movie/550 资源有问题")
        self.assertTrue(r.is_movie)


class PickTest(unittest.TestCase):
    def test_order_and_filters(self):
        rs = [ctx("pack", 2), ctx("S02E01-E08", 2, 1, 8), ctx("S02E05", 2, 5), ctx("S01E05", 1, 5),
              ctx("S02E05 dead", 2, 5, seeders=0)]
        self.assertEqual([c.torrent_info.title for c in pick(rs, 2, 5)], ["S02E05", "S02E01-E08", "pack"])
        self.assertEqual([c.torrent_info.title for c in pick(rs, 2, 5, allow_pack=False)],
                         ["S02E05", "S02E01-E08"])


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
        self.p = plugin_mod.TgEpisodeRequest()
        self.p.init_plugin({"enabled": False, "admin_ids": "999"})
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

    def test_multi_season_asks_then_searches(self):
        calls.seasons = [1, 2, 3]
        calls.results = [ctx("S02E05", 2, 5)]
        self.handle(self.msg("tmdb 123 第5集 花屏"))
        _, buttons = self.bot.sent[-1]
        self.assertEqual([b["text"] for b in buttons[0]], ["S01", "S02", "S03"])
        self.click(buttons[0][1]["callback_data"], uid=999)  # 管理员
        self.assertEqual(calls.search[0]["season"], 2)
        self.assertIn("资源有问题", self.bot.edits[-1][0])

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

    def test_allowed_chats(self):
        self.p._allowed_chats = {-1}
        self.handle(self.msg("tmdb 123 缺第5集"))
        self.assertEqual(self.bot.sent, [])


if __name__ == "__main__":
    unittest.main()
