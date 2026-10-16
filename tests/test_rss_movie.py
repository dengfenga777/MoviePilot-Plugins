import copy
import importlib.util
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


class MediaType(Enum):
    MOVIE = '电影'
    TV = '电视剧'


def stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class Base:
    def __init__(self):
        self.data = {}
        self.chain = Mock()
        self.systemconfig = Mock()

    def get_data(self, key):
        return copy.deepcopy(self.data.get(key))

    def save_data(self, key, value):
        self.data[key] = copy.deepcopy(value)


class Chain:
    def __init__(self, *args, **kwargs):
        self.messagehelper = None


class Download(Chain):
    calls = []
    fail = set()

    def download_single(self, **kwargs):
        title = kwargs['context'].torrent_info.title
        self.calls.append(title)
        return None if title in self.fail else 'hash'


class Meta:
    def __init__(self, title=None, subtitle=None):
        self.name = title


for name in ('app', 'app.chain', 'app.sdk', 'app.plugins', 'app.schemas'):
    stub(name)
stub('pytz')
stub('apscheduler'); stub('apscheduler.schedulers'); stub('apscheduler.triggers')
stub('apscheduler.schedulers.background', BackgroundScheduler=object)
stub('apscheduler.triggers.cron', CronTrigger=object)
stub('pydantic', BaseModel=object)
sys.modules['app'].schemas = SimpleNamespace(Response=list)
stub('app.chain.download', DownloadChain=Download)
stub('app.chain.subscribe', SubscribeChain=Chain)
stub('app.sdk.config', settings=SimpleNamespace(TZ='Asia/Shanghai'))
stub('app.sdk.logging', logger=Mock())
stub('app.sdk.media', Context=SimpleNamespace, MediaInfo=object, MetaInfo=Meta,
     TorrentInfo=SimpleNamespace, resolve_media_identity=lambda **kw: (None, None))
stub('app.sdk.network', RssHelper=object)
stub('app.sdk.services', RuleHelper=Mock)
stub('app.plugins', _PluginBase=Base)
stub('app.schemas', ExistMediaInfo=object)
stub('app.schemas.types', MediaSource=SimpleNamespace(TMDB='tmdb'), SystemConfigKey=SimpleNamespace(
    SubscribeFilterRuleGroups='rules'), MediaType=MediaType)

source = Path(__file__).resolve().parents[1] / 'plugins.v3' / 'rsssubscribemovienonotify' / '__init__.py'
spec = importlib.util.spec_from_file_location('rss_movie_plugin', source)
plugin_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_module)
Plugin = plugin_module.RssSubscribeMovieNoNotify


def movie(media_id='1'):
    return SimpleNamespace(type=MediaType.MOVIE, media_source=SimpleNamespace(value='tmdb'), media_id=media_id,
                           title='Dear You', year=2026, title_year='Dear You (2026)', category='华语电影',
                           overview='', get_poster_image=lambda: '')


class MovieTests(unittest.TestCase):
    def setUp(self):
        Download.calls = []
        Download.fail = set()
        self.plugin = Plugin()
        self.plugin._address = 'https://a.invalid/rss?passkey=secret\nhttps://b.invalid/rss'
        self.plugin._filter = False
        self.plugin._action = 'download'
        self.plugin.chain.media_exists.return_value = None
        self.plugin.chain.recognize_media.side_effect = lambda meta: movie('2' if 'Other' in meta.name else '1')
        self.feeds = {}
        self.plugin._RssSubscribeMovieNoNotify__fetch_rss = lambda url: self.feeds.get(url.split('/')[2], [])

    def feed(self, site, *items):
        self.feeds[site] = [{'title': title, 'guid': f'{site}:{title}', 'size': size} for title, size in items]

    def run_check(self):
        self.plugin._RssSubscribeMovieNoNotify__check()

    def test_batch_downloads_only_the_best_version(self):
        self.feed('a.invalid', ('Dear You 2026 1080p WEB-DL', 5), ('Dear You 2026 2160p WEB-DL', 10),
                  ('Dear You 2026 2160p WEB-DL HQ 60fps', 30))
        self.feed('b.invalid', ('Dear You 2026 2160p TX WEB-DL', 12))
        self.run_check()
        self.assertEqual(Download.calls, ['Dear You 2026 2160p WEB-DL HQ 60fps'])
        self.run_check()
        self.assertEqual(len(Download.calls), 1)

    def test_each_movie_gets_one_download(self):
        self.feed('a.invalid', ('Dear You 2026 2160p', 10), ('Other 2026 2160p', 10), ('Other 2026 1080p', 5))
        self.run_check()
        self.assertEqual(sorted(Download.calls), ['Dear You 2026 2160p', 'Other 2026 2160p'])

    def test_equal_quality_prefers_earlier_feed(self):
        self.feed('a.invalid', ('Dear You 2026 2160p A', 10))
        self.feed('b.invalid', ('Dear You 2026 2160p B', 10))
        self.run_check()
        self.assertEqual(Download.calls, ['Dear You 2026 2160p A'])

    def test_upgrades_once_to_higher_resolution(self):
        self.feed('a.invalid', ('Dear You 2026 1080p', 5))
        self.run_check()
        self.feed('a.invalid', ('Dear You 2026 1080p', 5), ('Dear You 2026 1080p HQ', 9))
        self.run_check()
        self.assertEqual(Download.calls, ['Dear You 2026 1080p'])
        self.plugin.chain.media_exists.return_value = SimpleNamespace()
        self.feed('a.invalid', ('Dear You 2026 2160p', 10))
        self.run_check()
        self.feed('a.invalid', ('Dear You 2026 2160p HQ', 30))
        self.run_check()
        self.assertEqual(Download.calls, ['Dear You 2026 1080p', 'Dear You 2026 2160p'])

    def test_failed_download_tries_next_version(self):
        self.feed('a.invalid', ('Dear You 2026 2160p', 10), ('Dear You 2026 1080p', 5))
        Download.fail = {'Dear You 2026 2160p'}
        self.run_check()
        self.assertEqual(Download.calls, ['Dear You 2026 2160p', 'Dear You 2026 1080p'])
        self.assertEqual(self.plugin.data['movie_downloads_v1']['tmdb:1']['res'], 1080)

    def test_library_movie_is_not_downloaded(self):
        self.plugin.chain.media_exists.return_value = SimpleNamespace()
        self.feed('a.invalid', ('Dear You 2026 2160p', 10))
        self.run_check()
        self.assertEqual(Download.calls, [])

    def test_resolution_and_mask(self):
        self.assertEqual(plugin_module.resolution('X 2026 2160p WEB-DL'), 2160)
        self.assertEqual(plugin_module.resolution('X 4K UHD'), 2160)
        self.assertEqual(plugin_module.resolution('X 2026 1080p'), 1080)
        self.assertEqual(plugin_module.resolution('X 2026 WEB-DL'), 0)
        self.assertEqual(plugin_module.mask_url('https://a/rss?passkey=abc&rows=10&rsskey=x'),
                         'https://a/rss?passkey=***&rows=10&rsskey=***')


if __name__ == '__main__':
    unittest.main()
