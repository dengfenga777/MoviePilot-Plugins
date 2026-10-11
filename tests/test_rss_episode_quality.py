"""Offline behavioral tests of the actual patched plugin class; no network/server access."""
import ast,copy,datetime,hashlib,re,unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace as NS
from threading import Lock

ROOT=Path(__file__).resolve().parents[1]
PLUGIN=ROOT/'plugins.v3/rssepisodequality/__init__.py'
class MediaType(Enum):
    TV='电视剧'; MOVIE='电影'
class MediaSource(Enum):
    TMDB='themoviedb'
class MetaInfo:
    def __init__(self,title,subtitle=''):
        self.title=title; self.name=title.split(' ')[0]
        text=title+' '+(subtitle or '')
        seasons=re.findall(r'S(\d+)',text)
        self.season_list=sorted(set(map(int,seasons))) or [1]
        self.begin_season=self.season_list[0]
        match=re.search(r'E(\d+)(?:-(?:S\d+)?E(\d+))?',text)
        self.episode_list=list(range(int(match[1]),int(match[2] or match[1])+1)) if match else []
        self.season=f'S{self.begin_season:02d}'; self.season_episode=self.season+str(self.episode_list)
class TorrentInfo(NS):
    def __init__(self,**kw):
        super().__init__(pri_order=0,**kw)
class Base:
    def get_data(self,key):return copy.deepcopy(self.data.get(key))
    def save_data(self,key,value):self.data[key]=copy.deepcopy(value)
class Logger:
    def __getattr__(self,name):return lambda *a,**k:None
class RuleHelper:
    def get_rule_group_by_media(self,**kw):return [NS(name='电视剧')]
class Chain:
    def recognize_media(self,meta):
        return NS(type=MediaType.MOVIE if meta.name=='Movie' else MediaType.TV,
                  media_source=MediaSource.TMDB,media_id=meta.name,category='电视剧',
                  title=meta.name,title_year=meta.name+' (2026)',year='2026',overview='',
                  get_poster_image=lambda:None)
    def filter_torrents(self,torrent_list,**kw):
        t=torrent_list[0]
        if 'Rejected' in t.title:return []
        t.pri_order=100 if '2160p' in t.title else 94
        return [t]
    def media_exists(self,mediainfo):return self.existing.get(mediainfo.media_id)
    existing={}
class Downloader:
    calls=[]; failed=set()
    def download_single(self,context,**kw):
        self.calls.append(context.torrent_info.title)
        return context.torrent_info.title not in self.failed
class Subscriber:
    calls=[]
    def exists(self,**kw):return False
    def add(self,**kw):self.calls.append(kw);return 1,None
ns=dict(re=re,datetime=datetime,hashlib=hashlib,traceback=__import__('traceback'),
        logger=Logger(),_PluginBase=Base,MediaType=MediaType,MediaSource=MediaSource,
        MetaInfo=MetaInfo,TorrentInfo=TorrentInfo,Context=NS,RuleHelper=RuleHelper,
        SilentDownloadChain=Downloader,SilentSubscribeChain=Subscriber,lock=Lock(),
        SystemConfigKey=NS(SubscribeFilterRuleGroups='groups'))
tree=ast.parse(PLUGIN.read_text())
klass=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='RssEpisodeQuality')
module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),klass],type_ignores=[])
exec(compile(ast.fix_missing_locations(module),str(PLUGIN),'exec'),ns)
Plugin=ns['RssEpisodeQuality']
def entry(title,size=100):return dict(title=title,description='',enclosure=title,link=title,size=size,pubdate=None)
def plugin(feeds,data=None,**opts):
    p=Plugin();p.data=copy.deepcopy(data or {});p.chain=Chain();p.systemconfig=NS(get=lambda key:['电视剧'])
    p._address='\n'.join(feeds);p._filter=True;p._action='download';p._save_path='/downloads'
    p._include='';p._exclude='';p._size_range='';p._clearflag=False;p._history_limit=1000
    p._smart_quality=True;p._skip_unknown_episodes=True
    for k,v in opts.items():setattr(p,k,v)
    setattr(p,'_RssEpisodeQuality__fetch_rss',lambda url:feeds[url])
    return p
class Tests(unittest.TestCase):
    def setUp(self):Downloader.calls=[];Downloader.failed=set();Chain.existing={};Subscriber.calls=[]
    def run_plugin(self,p):p._RssEpisodeQuality__check();return Downloader.calls
    def test_cross_feed_high_wins_even_when_smaller(self):
        low=entry('Show S01E01 1080p WEB-DL H265',500)
        high=entry('Show S01E01 2160p WEB-DL H265',100)
        self.assertEqual(self.run_plugin(plugin({'feed1':[low],'feed2':[high]})),[high['title']])
    def test_equal_quality_different_size_only_one(self):
        a=entry('Show S01E01 2160p WEB-DL H265-TeamA',100)
        b=entry('Show S01E01 2160p WEB-DL H265-TeamB',500)
        self.assertEqual(self.run_plugin(plugin({'feed':[a,b]})),[b['title']])
    def test_failed_best_falls_back(self):
        high=entry('Show S01E01 2160p WEB-DL');low=entry('Show S01E01 1080p WEB-DL')
        Downloader.failed.add(high['title']);p=plugin({'feed':[low,high]})
        self.assertEqual(self.run_plugin(p),[high['title'],low['title']])
        ledger=p.data['episode_quality_v1'];self.assertTrue(all(v['title']==low['title'] for v in ledger.values()))
    def test_persistent_record_stops_later_downgrade_after_history_eviction(self):
        high=entry('Show S01E01 2160p WEB-DL');p=plugin({'feed':[high]});self.run_plugin(p)
        data=p.data;data['history']=[];Downloader.calls=[]
        low=entry('Show S01E01 1080p WEB-DL')
        self.assertEqual(self.run_plugin(plugin({'feed':[low]},data)),[])
    def test_later_upgrade_allowed_while_not_in_library(self):
        p=plugin({'feed':[entry('Show S01E01 1080p WEB-DL')]});self.run_plugin(p);Downloader.calls=[]
        high=entry('Show S01E01 2160p WEB-DL')
        self.assertEqual(self.run_plugin(plugin({'feed':[high]},p.data)),[high['title']])
    def test_distinct_seasons_and_media_do_not_collide(self):
        titles=['Show S01E01 2160p WEB-DL','Show S02E01 1080p WEB-DL','Other S01E01 1080p WEB-DL']
        self.assertCountEqual(self.run_plugin(plugin({'feed':list(map(entry,titles))})),titles)
    def test_multiepisode_pack_skips_covered_single(self):
        pack=entry('Show S01E01-E02 2160p WEB-DL');single=entry('Show S01E02 1080p WEB-DL')
        self.assertEqual(self.run_plugin(plugin({'feed':[single,pack]})),[pack['title']])
    def test_partial_overlap_new_episode_allowed(self):
        high=entry('Show S01E01-E02 2160p WEB-DL');p=plugin({'feed':[high]});self.run_plugin(p);Downloader.calls=[]
        pack=entry('Show S01E02-E03 1080p WEB-DL')
        self.assertEqual(self.run_plugin(plugin({'feed':[pack]},p.data)),[pack['title']])
        self.assertEqual(p.data['episode_quality_v1']['themoviedb:Show:S1:E2']['title'],high['title'])
    def test_missing_episode_defaults_to_skip(self):
        self.assertEqual(self.run_plugin(plugin({'feed':[entry('Show S01 2160p WEB-DL')]})),[])
    def test_missing_episode_can_opt_out(self):
        title='Show S01 2160p WEB-DL'
        self.assertEqual(self.run_plugin(plugin({'feed':[entry(title)]},_skip_unknown_episodes=False)),[title])
    def test_legacy_history_backfills_quality(self):
        old='Show S01E01 2160p WEB-DL'
        data={'history':[dict(type='电视剧',rss_title=old,media_id='Show',media_source='themoviedb',key='old',time='2026-10-01')]}
        p=plugin({'feed':[entry('Show S01E01 1080p WEB-DL')]},data)
        self.assertEqual(self.run_plugin(p),[]);self.assertTrue(p.data['episode_quality_v1'])
    def test_subtitle_episode_coverage(self):
        e=entry('Show S01 2160p WEB-DL');e['description']='S01E03-E04'
        p=plugin({'feed':[e]});self.assertEqual(self.run_plugin(p),[e['title']])
        self.assertEqual(len(p.data['episode_quality_v1']),2)
    def test_feature_off_preserves_multiple_downloads(self):
        titles=['Show S01E01 1080p WEB-DL','Show S01E01 2160p WEB-DL']
        self.assertEqual(self.run_plugin(plugin({'feed':list(map(entry,titles))},_smart_quality=False)),titles)
    def test_existing_library_is_not_washed(self):
        Chain.existing={'Show':NS(seasons={1:[1]})}
        self.assertEqual(self.run_plugin(plugin({'feed':[entry('Show S01E01 2160p WEB-DL')]})),[])
    def test_rule_reject_does_not_block_valid_candidate(self):
        low=entry('Show S01E01 1080p WEB-DL');bad=entry('Show S01E01 2160p Rejected')
        self.assertEqual(self.run_plugin(plugin({'feed':[bad,low]})),[low['title']])
    def test_clear_empty_run_clears_quality_too(self):
        p=plugin({'feed':[]},{'episode_quality_v1':{'old':{'rank':[100]}}},_clearflag=True)
        self.run_plugin(p);self.assertEqual(p.data['episode_quality_v1'],{})
    def test_movies_are_excluded(self):
        titles=['Movie 1080p WEB-DL','Movie 2160p WEB-DL']
        self.assertEqual(self.run_plugin(plugin({'feed':list(map(entry,titles))})),[])
    def test_duplicate_feed_torrent_is_only_submitted_once(self):
        a=entry('Show S01E01 2160p WEB-DL')
        self.assertEqual(self.run_plugin(plugin({'a':[a],'b':[a]})),[a['title']])
    def test_subscribe_action_keeps_unidentified_episode(self):
        p=plugin({'feed':[entry('Show S01 2160p WEB-DL')]},_action='subscribe')
        self.assertEqual(self.run_plugin(p),[]);self.assertEqual(len(Subscriber.calls),1)
    def test_size_limit_preserved(self):
        p=plugin({'feed':[entry('Show S01E01 2160p WEB-DL',3*1024**3),entry('Show S01E01 1080p WEB-DL',1024**3)]},_size_range='0-2')
        self.assertEqual(self.run_plugin(p),['Show S01E01 1080p WEB-DL'])
if __name__=='__main__':unittest.main(verbosity=2)
