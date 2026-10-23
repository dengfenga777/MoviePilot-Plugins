"""Emby媒体库封面生成（异步版）：入库事件只排队、去重合并、不触发全量同步（MoviePilot 用桩替换）。"""
import importlib
import logging
import sys
import time
import types
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace as NS

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins.v3"
PREFIX = "_MediaCoverGeneratorAsync__"


class _Chain:
    def __init__(self):
        self.exists = None
        self.exists_calls = 0
        self.sync_calls = 0

    def media_exists(self, mediainfo):
        self.exists_calls += 1
        return self.exists

    def iteminfo(self, server, item_id):
        return NS(library="L1", path="/media/tv/x", name=f"item-{item_id}")

    def sync(self):
        self.sync_calls += 1


def _stub_moviepilot(tmp: Path):
    class EventType(Enum):
        TransferComplete = "transfer.complete"
        WebhookMessage = "webhook.message"
        PluginAction = "plugin.action"

    class _EventManager:
        @staticmethod
        def register(_etype):
            return lambda fn: fn

    class _PluginBase:
        def __init__(self):
            self._data = {}
            self._configs = {}

        def get_data(self, key):
            return self._data.get(key)

        def save_data(self, key, value):
            self._data[key] = value

        def get_config(self, plugin_id=None):
            return self._configs.get(plugin_id or type(self).__name__)

        def update_config(self, config, plugin_id=None):
            self._configs[plugin_id or type(self).__name__] = dict(config)

        def get_data_path(self, plugin_id=None):
            return tmp

        def post_message(self, **kw):
            pass

    class _Response:
        def __class_getitem__(cls, _item):
            return cls

    logger = logging.getLogger("mcg-test")
    plugins_pkg = types.ModuleType("app.plugins")
    plugins_pkg.__path__ = [str(PLUGINS_DIR)]
    plugins_pkg._PluginBase = _PluginBase
    mods = {
        "app": {},
        "app.schemas": {"Response": _Response},
        "app.schemas.types": {"EventType": EventType},
        "app.chain": {},
        "app.chain.mediaserver": {"MediaServerChain": _Chain},
        "app.sdk": {},
        "app.sdk.config": {"settings": NS(TZ="Asia/Shanghai")},
        "app.sdk.events": {"eventmanager": _EventManager, "Event": object},
        "app.sdk.services": {"MediaServerHelper": lambda: NS(get_services=lambda **kw: {})},
        "app.sdk.logging": {"logger": logger},
        "app.sdk.media": {"MediaInfo": object},
        "app.sdk.network": {"RequestUtils": object, "UrlUtils": object},
        "app.log": {"logger": logger},
    }
    for name, attrs in mods.items():
        module = types.ModuleType(name)
        module.__path__ = []
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
    sys.modules["app.plugins"] = plugins_pkg
    sys.modules["app"].schemas = sys.modules["app.schemas"]
    return EventType


class MediaCoverAsyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        _stub_moviepilot(Path(cls._tmp.name))
        module = importlib.import_module("app.plugins.mediacovergeneratorasync")
        cls.Plugin = module.MediaCoverGeneratorAsync

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        plugin = self.Plugin()
        plugin.mschain = _Chain()
        plugin._enabled = True
        plugin._transfer_monitor = True
        plugin._delay = 60
        plugin._include_libraries = []
        plugin._servers = {"emby": NS(type="emby", name="emby")}
        setattr(plugin, PREFIX + "get_server_libraries",
                lambda service: [{"Id": "L1", "Name": "TV", "Locations": ["/media/tv"]}])
        self.updated = []
        setattr(plugin, PREFIX + "update_library",
                lambda service, library: self.updated.append(library["Id"]) or True)
        setattr(plugin, PREFIX + "get_fonts", lambda: None)
        self.plugin = plugin

    def _event(self, tmdb_id, title="某剧 (2026)"):
        mediainfo = NS(tmdb_id=tmdb_id, type=NS(value="电视剧"), title_year=title)
        return NS(event_data={"mediainfo": mediainfo})

    def _private(self, name):
        return getattr(self.plugin, PREFIX + name)

    def test_transfer_event_returns_immediately_and_dedupes(self):
        started = time.monotonic()
        for _ in range(500):
            self.plugin.update_library_cover(self._event(1))
        self.plugin.update_library_cover(self._event(2, "另一部 (2025)"))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(self.plugin._monitor_media_tasks), 2)
        self.assertEqual(self.plugin.mschain.exists_calls, 0)

    def test_missing_media_retries_without_full_sync(self):
        self.plugin.update_library_cover(self._event(1))
        task = next(iter(self.plugin._monitor_media_tasks.values()))
        self.assertTrue(self._private("process_media_task")(task))
        self.assertEqual(self.plugin.mschain.sync_calls, 0)

    def test_same_library_merges_into_one_cover_update(self):
        self.plugin.mschain.exists = NS(server="emby", itemid="100")
        self.plugin.update_library_cover(self._event(1))
        self.plugin.mschain.exists = NS(server="emby", itemid="200")
        self.plugin.update_library_cover(self._event(2, "另一部 (2025)"))
        process = self._private("process_media_task")
        self.plugin.mschain.exists = NS(server="emby", itemid="100")
        process(self.plugin._monitor_media_tasks[("media", "电视剧:tmdb:1")])
        self.plugin.mschain.exists = NS(server="emby", itemid="200")
        process(self.plugin._monitor_media_tasks[("media", "电视剧:tmdb:2")])
        self.assertEqual(list(self.plugin._monitor_library_tasks), [("emby", "L1")])
        task = self.plugin._monitor_library_tasks[("emby", "L1")]
        self.assertEqual(task["item_id"], "200")
        self.assertFalse(self._private("process_library_task")(task))
        self.assertEqual(self.updated, ["L1"])
        self.assertEqual(self.plugin.get_data("cover_history")[-1]["item_id"], "200")

    def test_worker_processes_due_tasks_and_stops(self):
        self.plugin._delay = 0
        self.plugin._MONITOR_POLL_SECONDS = 0.05
        self.plugin._MONITOR_LIBRARY_DEBOUNCE_SECONDS = 0
        self.plugin.mschain.exists = NS(server="emby", itemid="100")
        self._private("start_monitor_worker")()
        try:
            self.plugin.update_library_cover(self._event(1))
            deadline = time.monotonic() + 3
            while not self.updated and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(self.updated, ["L1"])
        finally:
            self._private("stop_monitor_worker")()
        self.assertIsNone(self.plugin._monitor_thread)

    def test_legacy_config_migrates_disabled(self):
        self.plugin._configs["MediaCoverGenerator"] = {"enabled": True, "transfer_monitor": True, "delay": 30}
        config = self._private("migrate_legacy_config")()
        self.assertFalse(config["enabled"])
        self.assertEqual(config["delay"], 30)
        self.assertFalse(self.plugin.get_config()["enabled"])


if __name__ == "__main__":
    unittest.main()
