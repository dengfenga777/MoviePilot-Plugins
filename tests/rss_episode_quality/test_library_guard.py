"""Exercise the actual plugin class with in-memory MoviePilot host ports."""
import ast
import copy
import datetime
import hashlib
import json
import re
import time
import unittest
from enum import Enum
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import Mock, patch
from runtime_support import load

engine=load('engine')
class MediaType(Enum): TV='电视剧'; MOVIE='电影'
class Base:
    def __init__(self):
        self.data={}; self.chain=SimpleNamespace(recognize_media=lambda **kw:self.media)
        self.systemconfig=SimpleNamespace(get=lambda key:[])
        self.saved_config=None
    def get_data(self,key):return copy.deepcopy(self.data.get(key))
    def save_data(self,key,value):self.data[key]=copy.deepcopy(value)
    def update_config(self,value):self.saved_config=value
class Meta:
    def __init__(self,title=None,**kw):
        self.name=title; self.begin_season=1; self.end_season=None
        self.season_list=[1]; self.episode_list=list(engine.file_scope(title,1)[1])
        self.season='S01'; self.season_episode='S01 episodes'
class TorrentInfo(SimpleNamespace):pass
class Download:
    calls=[]
    last_downloader='QB'; last_error=None
    def download_single(self,**kwargs): self.calls.append(kwargs); return 'fakehash'
    def download_torrent(self,info):
        from torrentool.api import Torrent
        names=[f'Show.S01E{i:02}.2160p.WEB-DL.mkv' for i in range(1,13)]
        # Different releases are different torrents; the same release from two sites is identical.
        return Torrent({'info':{'name':getattr(info,'title',None) or 'Show','files':[{'length':1,'path':[name]} for name in names],
                      'piece length':16384,'pieces':b'0'*20}}).to_string(),'Show',names
class Subscribe:
    calls=[]
    def exists(self,**kw):return False
    def add(self,**kw): self.calls.append(kw); return 12,None
class MediaHelper:
    services={'EMBY':SimpleNamespace(name='EMBY')}
    def get_services(self):return self.services
    def get_configs(self):return self.services
class DownloadHelper:
    services={'QB':SimpleNamespace(instance=Mock())}
    def get_services(self):return self.services
    def get_configs(self):return {'QB':SimpleNamespace(default=True)}

source=Path(__file__).resolve().parents[2]/'plugins.v3'/'rssepisodequality'/'__init__.py'
tree=ast.parse(source.read_text())
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='RssEpisodeQuality')
module=ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[]))
ns={'_PluginBase':Base,'Optional':Optional,'Any':Any,'Dict':Dict,'List':List,'Tuple':Tuple,
    'BackgroundScheduler':object,'Path':Path,'DeleteHistoryRequest':object,
    'schemas':SimpleNamespace(Response=List),'datetime':datetime,'hashlib':hashlib,
    'resolve_media_identity':lambda **kw:(None,None),'MediaSource':SimpleNamespace(TMDB='tmdb'),
    'MediaType':MediaType,'logger':Mock(),'time':time,'json':json,'re':re,'traceback':__import__('traceback'),
    'MediaServerHelper':MediaHelper,'DownloaderHelper':DownloadHelper,
    'query_library':Mock(),'Library':engine.Library,'UncertainLibrary':engine.UncertainLibrary,
    'UnsafeSelection':engine.UnsafeSelection,'decide':engine.decide,'quality':engine.quality,'rank':engine.rank,'TARGETS':engine.TARGETS,
    'file_scope':engine.file_scope,'VIDEO':engine.VIDEO,'MetaInfo':Meta,
    'Context':SimpleNamespace,'TorrentInfo':TorrentInfo,'MediaInfo':SimpleNamespace,
    'SystemConfigKey':SimpleNamespace(SubscribeFilterRuleGroups='rules'),
    'RuleHelper':object,'SilentDownloadChain':Download,'SilentSubscribeChain':Subscribe,
    'lock':Lock(),'settings':SimpleNamespace(TZ='Asia/Shanghai')}
exec(compile(module,str(source),'exec'),ns)
Plugin=ns['RssEpisodeQuality']

class PluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin=Plugin(); self.plugin._address='feed'; self.plugin._filter=False
        self.plugin._action='download'; self.plugin._mode='missing'; self.plugin._library_servers=''
        self.plugin.media=SimpleNamespace(media_source=SimpleNamespace(value='tmdb'),media_id='301494',
            title='余红旧事',year=2026,title_year='余红旧事 (2026)',type=MediaType.TV,category='电视剧',
            overview='',get_poster_image=lambda:'')
        ns['query_library']=Mock(); Download.calls=[]; Subscribe.calls=[]
        DownloadHelper.services={'QB':SimpleNamespace(type='qbittorrent',instance=Mock())}
        DownloadHelper.services['QB'].instance.get_torrents.return_value=([],False)
        self.plugin._RssEpisodeQuality__fetch_rss=lambda url:[{'title':'Show.S01E01-E12.2160p.WEB-DL',
                'description':'','enclosure':'https://feed.invalid/file.torrent','size':1}]
    def library(self,last=10):
        return engine.Library({(1,i):[{'Path':f'Show.S01E{i:02}.1080p.WEB-DL.mkv',
            'MediaStreams':[{'Type':'Video','Height':1080,'Width':1920,'VideoRange':'SDR'}]}] for i in range(1,last+1)})
    def run_check(self):self.plugin._RssEpisodeQuality__check()
    def test_pipeline_selects_only_missing_and_records_only_selected(self):
        ns['query_library'].return_value=self.library()
        self.run_check()
        self.assertEqual(Download.calls[0]['episodes'],{11,12})
        self.assertEqual(set(self.plugin.data['pending_downloads_v2']),{'tmdb:301494:S1:E11','tmdb:301494:S1:E12'})
        self.assertEqual(self.plugin.data['history'][0]['selected_episodes'],[11,12])
    def test_screenshot_full_library_skips_pack(self):
        ns['query_library'].return_value=self.library(34)
        self.run_check(); self.assertFalse(Download.calls)
    def test_failed_lookup_even_without_snapshot_never_downloads(self):
        ns['query_library'].side_effect=engine.UncertainLibrary('HTTP 500')
        with patch.object(time,'sleep'):self.run_check()
        self.assertFalse(Download.calls)
        self.assertEqual(self.plugin.data['decisions_v1'][-1]['status'],'deferred')
    def test_full_library_then_successful_empty_defers(self):
        ns['query_library'].return_value=self.library(34); self.run_check()
        ns['query_library'].return_value=engine.Library(); self.run_check()
        self.assertFalse(Download.calls)
    def test_confirmed_delete_can_refill_after_interval(self):
        clock=Mock(return_value=100000)
        with patch.object(time,'time',clock):
            ns['query_library'].return_value=self.library(34); self.run_check()
            ns['query_library'].return_value=engine.Library(); self.run_check()
            clock.return_value=100601; self.run_check()
        self.assertEqual(Download.calls[0]['episodes'],set(range(1,13)))
    def test_recovery_resets_delete_confirmation(self):
        clock=Mock(return_value=100000)
        with patch.object(time,'time',clock):
            ns['query_library'].return_value=self.library(34); self.run_check()
            ns['query_library'].return_value=engine.Library(); self.run_check()
            clock.return_value=100601
            ns['query_library'].return_value=self.library(34); self.run_check()
            ns['query_library'].return_value=engine.Library(); self.run_check()
        self.assertFalse(Download.calls)
    def test_cross_rss_pending_protects_active_download(self):
        ns['query_library'].return_value=self.library(); self.run_check()
        DownloadHelper.services['QB'].instance.get_torrents.return_value=([{'progress':0.5}],False)
        self.run_check(); self.assertEqual(len(Download.calls),1)
    def test_active_download_not_expired_after_six_hours(self):
        clock=Mock(return_value=100000)
        with patch.object(time,'time',clock):
            ns['query_library'].return_value=self.library(); self.run_check()
            clock.return_value=140000
            DownloadHelper.services['QB'].instance.get_torrents.return_value=([{'progress':0.5}],False)
            self.run_check()
        self.assertEqual(len(Download.calls),1)
    def test_completed_pending_eventually_allows_retry_transfer_failure(self):
        clock=Mock(return_value=100000)
        with patch.object(time,'time',clock):
            ns['query_library'].return_value=self.library(); self.run_check()
            clock.return_value=140000
            DownloadHelper.services['QB'].instance.get_torrents.return_value=([{'progress':1.0}],False)
            self.run_check()
        self.assertEqual(len(Download.calls),2)
    def test_pending_query_failure_defers(self):
        ns['query_library'].return_value=self.library(); self.run_check()
        DownloadHelper.services['QB'].instance.get_torrents.return_value=([],True)
        self.run_check(); self.assertEqual(len(Download.calls),1)
        self.assertEqual(self.plugin.data['decisions_v1'][-1]['status'],'deferred')
    def test_failed_submission_never_writes_pending_or_history(self):
        ns['query_library'].return_value=self.library()
        with patch.object(Download,'download_single',return_value=None):self.run_check()
        self.assertEqual(self.plugin.data.get('pending_downloads_v2'),{})
        self.assertEqual(self.plugin.data.get('history'),[])
    def test_upgrade_mode_uses_library_quality(self):
        self.plugin._mode='upgrade'; ns['query_library'].return_value=self.library()
        self.run_check(); self.assertEqual(Download.calls[0]['episodes'],set(range(1,13)))
    def test_bad_cache_stops_candidate(self):
        self.plugin.data['library_snapshot_v2']=['corrupt']; ns['query_library'].return_value=engine.Library()
        self.run_check(); self.assertFalse(Download.calls)
    def test_reservation_save_failure_prevents_submission(self):
        ns['query_library'].return_value=self.library()
        original=self.plugin.save_data
        def save(key,value):
            if key=='pending_downloads_v2' and value:
                raise RuntimeError('database unavailable')
            original(key,value)
        self.plugin.save_data=save
        with self.assertRaises(RuntimeError):self.run_check()
        self.assertFalse(Download.calls)
    def test_failed_paused_submission_retains_recoverable_intent(self):
        ns['query_library'].return_value=self.library()
        DownloadHelper.services['QB'].instance.get_torrents.return_value=([{'progress':0.0}],False)
        with patch.object(Download,'download_single',return_value=None):self.run_check()
        self.assertTrue(self.plugin.data['pending_downloads_v2'])
        self.assertTrue(all(r['state']=='reserved' for r in self.plugin.data['pending_downloads_v2'].values()))
        self.run_check(); self.assertFalse(Download.calls)
    def test_unknown_progress_defers_pending_recovery(self):
        ns['query_library'].return_value=self.library(); self.run_check()
        DownloadHelper.services['QB'].instance.get_torrents.return_value=([{}],False)
        self.run_check(); self.assertEqual(len(Download.calls),1)
    def test_old_history_is_not_a_permanent_refill_ban(self):
        self.plugin.data['history']=[{'key':'Show.S01E01-E12.2160p.WEB-DL'}]
        ns['query_library'].return_value=engine.Library()
        self.run_check(); self.assertEqual(len(Download.calls),1)
    def test_clear_history_does_not_remove_active_protection(self):
        ns['query_library'].return_value=self.library(); self.run_check()
        self.plugin._clearflag=True
        DownloadHelper.services['QB'].instance.get_torrents.return_value=([{'progress':0.5}],False)
        self.run_check(); self.assertEqual(len(Download.calls),1)
    def test_unknown_title_can_use_verified_single_season_files(self):
        self.plugin._skip_unknown_episodes=False
        self.plugin._RssEpisodeQuality__fetch_rss=lambda url:[{'title':'Show.S01.Complete.2160p.WEB-DL',
                'description':'','enclosure':'https://feed.invalid/file.torrent','size':1}]
        ns['query_library'].return_value=self.library()
        self.run_check(); self.assertEqual(Download.calls[0]['episodes'],{11,12})
    def test_cross_feed_best_candidate_wins_even_if_smaller(self):
        self.plugin._RssEpisodeQuality__fetch_rss=lambda url:[
            {'title':'Show.S01E01-E12.1080p.WEB-DL','description':'','enclosure':'low','size':500},
            {'title':'Show.S01E01-E12.2160p.WEB-DL','description':'','enclosure':'high','size':100}]
        ns['query_library'].return_value=engine.Library()
        DownloadHelper.services['QB'].instance.get_torrents.return_value=([{'progress':0.5}],False)
        self.run_check()
        self.assertEqual(len(Download.calls),1)
        self.assertIn('2160p',Download.calls[0]['context'].torrent_info.title)
    def test_duplicate_feed_entry_only_submitted_once(self):
        feed={'title':'Show.S01E01-E12.2160p.WEB-DL','description':'','enclosure':'same','size':100}
        self.plugin._RssEpisodeQuality__fetch_rss=lambda url:[feed,feed.copy()]
        ns['query_library'].return_value=engine.Library()
        self.run_check(); self.assertEqual(len(Download.calls),1)
    def test_movie_candidates_are_excluded(self):
        self.plugin.media.type=MediaType.MOVIE
        ns['query_library'].return_value=engine.Library()
        self.run_check(); self.assertFalse(Download.calls)
    def test_description_cannot_expand_title_scope(self):
        self.plugin._RssEpisodeQuality__fetch_rss=lambda url:[
            {'title':'Show.S01E11.2160p.WEB-DL','description':'S01E01-E34 总34集','enclosure':'single','size':100}]
        ns['query_library'].return_value=engine.Library()
        self.run_check(); self.assertEqual(Download.calls[0]['episodes'],{11})
    def test_config_validation_bounds_and_policy(self):
        config={'query_retries':99,'pending_hours':'invalid','absence_minutes':-1,'mode':'bad','upgrade_policy':'bad'}
        self.plugin._RssEpisodeQuality__validate_and_fix_config(config)
        self.assertEqual(config['query_retries'],5); self.assertEqual(config['pending_hours'],6)
        self.assertEqual(config['absence_minutes'],1); self.assertEqual(config['mode'],'missing')
    def test_ui_roundtrips_all_policy_fields(self):
        form, defaults=self.plugin.get_form()
        self.assertEqual(defaults['mode'],'chase')
        def models(nodes):
            result=set()
            for node in nodes:
                if 'model' in node.get('props',{}):result.add(node['props']['model'])
                result.update(models(node.get('content',[]) if isinstance(node.get('content',[]),list) else []))
            return result
        expected={'library_servers','mode','upgrade_policy','query_retries','absence_minutes','pending_hours','target_quality','max_downloads'}
        self.assertTrue(expected<=models(form)); self.assertTrue(expected<=set(defaults))
        self.plugin._RssEpisodeQuality__update_config(); self.assertTrue(expected<=set(self.plugin.saved_config))
    # ---- 1.2.0 ----
    def test_mislabeled_upgrade_is_not_downloaded_again(self):
        self.plugin._mode='upgrade'
        clock=Mock(return_value=100000)
        with patch.object(time,'time',clock):
            ns['query_library'].return_value=self.library(); self.run_check()
            self.assertEqual(len(Download.calls),1)
            # Imported, but the "2160p" release was actually 1080p; the task was cleaned up.
            clock.return_value=200000
            ns['query_library'].return_value=self.library(12)
            DownloadHelper.services['QB'].instance.get_torrents.return_value=([],False)
            self.run_check()
        self.assertEqual(len(Download.calls),1)
        last=self.plugin.data['decisions_v1'][-1]
        self.assertEqual(last['status'],'skipped')
        self.assertIn('疑似标题虚标',last['reasons']['1'])
    def test_same_torrent_from_other_feed_is_not_upgraded_again(self):
        self.plugin._mode='upgrade'
        clock=Mock(return_value=100000)
        with patch.object(time,'time',clock):
            ns['query_library'].return_value=self.library(12); self.run_check()
            self.assertEqual(len(Download.calls),1)
            clock.return_value=200000
            DownloadHelper.services['QB'].instance.get_torrents.return_value=([],False)
            # Another site's RSS item (different key) carrying the same torrent content.
            self.plugin._RssEpisodeQuality__fetch_rss=lambda url:[{'title':'Show.S01E01-E12.2160p.WEB-DL',
                    'description':'','enclosure':'https://other.invalid/file.torrent','size':1}]
            self.run_check()
        self.assertEqual(len(Download.calls),1)
        self.assertIn('疑似标题虚标',self.plugin.data['decisions_v1'][-1]['reasons']['1'])
    def test_genuine_missing_episode_still_downloads_after_upgrade_attempt(self):
        self.plugin._mode='upgrade'
        clock=Mock(return_value=100000)
        with patch.object(time,'time',clock):
            ns['query_library'].return_value=self.library(); self.run_check()
            clock.return_value=200000
            ns['query_library'].return_value=self.library(11)
            DownloadHelper.services['QB'].instance.get_torrents.return_value=([],False)
            self.run_check()
        self.assertEqual(Download.calls[-1]['episodes'],{12})
    def test_identical_skips_are_recorded_once(self):
        ns['query_library'].return_value=self.library(34)
        for _ in range(3): self.run_check()
        statuses=[r['status'] for r in self.plugin.data['decisions_v1']]
        self.assertEqual(statuses,['skipped'])
    def test_changed_outcome_is_recorded_again(self):
        ns['query_library'].return_value=self.library(34); self.run_check()
        ns['query_library'].side_effect=engine.UncertainLibrary('offline')
        with patch.object(time,'sleep'):self.run_check()
        self.assertEqual([r['status'] for r in self.plugin.data['decisions_v1']],['skipped','deferred'])
    def test_decisions_written_once_per_run(self):
        ns['query_library'].return_value=self.library()
        original=self.plugin.save_data; writes=[]
        def save(key,value):
            if key=='decisions_v1':writes.append(len(value))
            original(key,value)
        self.plugin.save_data=save
        self.run_check()
        self.assertEqual(len(writes),1)
        self.assertEqual([r['status'] for r in self.plugin.data['decisions_v1']],['planned','reserved','submitted'])
    def test_non_tv_items_are_not_recognized_every_run(self):
        self.plugin.media.type=MediaType.MOVIE
        calls=[]
        self.plugin.chain=SimpleNamespace(recognize_media=lambda **kw:(calls.append(1),self.plugin.media)[1])
        ns['query_library'].return_value=engine.Library()
        self.run_check(); self.run_check()
        self.assertEqual(len(calls),1)
    def test_skip_cache_invalidated_by_config_change(self):
        self.plugin.media.type=MediaType.MOVIE
        calls=[]
        self.plugin.chain=SimpleNamespace(recognize_media=lambda **kw:(calls.append(1),self.plugin.media)[1])
        self.run_check(); self.plugin._exclude='nothing-matches'; self.run_check()
        self.assertEqual(len(calls),2)
    def test_clear_history_clears_skip_cache(self):
        self.plugin.media.type=MediaType.MOVIE
        calls=[]
        self.plugin.chain=SimpleNamespace(recognize_media=lambda **kw:(calls.append(1),self.plugin.media)[1])
        self.run_check(); self.plugin._clearflag=True; self.run_check()
        self.assertEqual(len(calls),2)
    def test_invalid_regex_stops_run_without_downloading(self):
        self.plugin._include='('
        ns['query_library'].return_value=engine.Library()
        self.run_check(); self.assertFalse(Download.calls)
    def test_invalid_regex_logged_on_save(self):
        config={'include':'(','exclude':'['}
        with patch.object(ns['logger'],'error') as error:
            self.plugin._RssEpisodeQuality__validate_and_fix_config(config)
        self.assertEqual(error.call_count,2); self.assertEqual(config['include'],'(')
    def test_missing_size_with_size_range_skips_quietly(self):
        self.plugin._size_range='1-5'
        self.plugin._RssEpisodeQuality__fetch_rss=lambda url:[{'title':'Show.S01E11.2160p.WEB-DL',
                'description':'','enclosure':'x','size':None}]
        ns['query_library'].return_value=engine.Library()
        with patch.object(ns['logger'],'error') as error:
            self.run_check()
        self.assertFalse(Download.calls); error.assert_not_called()
    def test_stale_ledger_entries_are_pruned(self):
        now=time.time()
        self.plugin.data['pending_downloads_v2']={
            'tmdb:1:S1:E1':{'submitted_at':now-8*86400,'hash':'gone','downloader':'QB'},
            'tmdb:1:S1:E2':{'submitted_at':now-8*86400,'hash':'busy','downloader':'QB'},
            'tmdb:1:S1:E3':{'submitted_at':now-31*86400,'hash':'h','downloader':'removed'},
            'tmdb:1:S1:E4':{'submitted_at':now-3600,'hash':'gone','downloader':'QB'}}
        def torrents(ids=None):
            return ([{'progress':0.4}] if ids=='busy' else []),False
        DownloadHelper.services['QB'].instance.get_torrents.side_effect=torrents
        ns['query_library'].return_value=self.library(34)
        self.run_check()
        self.assertEqual(set(self.plugin.data['pending_downloads_v2']),{'tmdb:1:S1:E2','tmdb:1:S1:E4'})
    def test_ledger_prune_keeps_entries_when_downloader_unreachable(self):
        self.plugin.data['pending_downloads_v2']={
            'tmdb:1:S1:E1':{'submitted_at':time.time()-8*86400,'hash':'h','downloader':'QB'}}
        DownloadHelper.services['QB'].instance.get_torrents.return_value=(None,True)
        ns['query_library'].return_value=self.library(34)
        self.run_check()
        self.assertIn('tmdb:1:S1:E1',self.plugin.data['pending_downloads_v2'])
    def test_history_page_shows_selected_episodes(self):
        ns['query_library'].return_value=self.library(); self.run_check()
        text=json.dumps(self.plugin.get_page(),ensure_ascii=False)
        self.assertIn('选集：E11-E12',text)
    def test_history_page_is_bounded(self):
        self.plugin.data['history']=[{'title':f't{i}','time':f'2026-01-01 00:00:{i%60:02d}','record_id':str(i)} for i in range(500)]
        cards=self.plugin.get_page()[-1]['content']
        self.assertEqual(len(cards),100)
    def test_decision_page_hides_routine_skips(self):
        ns['query_library'].return_value=self.library(34); self.run_check()
        text=json.dumps(self.plugin.get_page(),ensure_ascii=False)
        self.assertIn('另有 1 条',text)
    def test_episode_formatter(self):
        fmt=self.plugin._RssEpisodeQuality__format_episodes
        self.assertEqual(fmt([1,2,3,5,7,8]),'E01-E03, E05, E07-E08')
    # ---- 1.3.0 追更优先 ----
    def feed(self,*titles):
        self.plugin._RssEpisodeQuality__fetch_rss=lambda url:[
            {'title':t,'description':'','enclosure':f'https://feed.invalid/{i}/{t}','size':1} for i,t in enumerate(titles)]
    def chase_library(self,uhd=()):
        def version(i):
            h,w,tag=(2160,3840,'2160p') if i in uhd else (1080,1920,'1080p')
            return [{'Path':f'Show.S01E{i:02}.{tag}.WEB-DL.mkv',
                     'MediaStreams':[{'Type':'Video','Height':h,'Width':w,'VideoRange':'SDR'}]}]
        return lambda last: engine.Library({(1,i):version(i) for i in range(1,last+1)})
    def chase(self):
        self.plugin._mode='chase'; self.plugin._target_quality='2160p'; self.plugin._max_downloads=2
    def test_chase_fast_then_single_upgrade_then_stop(self):
        self.chase(); lib=self.chase_library()
        qb=DownloadHelper.services['QB'].instance
        clock=Mock(return_value=100000)
        with patch.object(time,'time',clock):
            # E11 appears in 1080p only: download it right away.
            self.feed('Show.S01E11.1080p.WEB-DL'); ns['query_library'].return_value=lib(10); self.run_check()
            self.assertEqual([c['episodes'] for c in Download.calls],[{11}])
            # 10 minutes later: imported and finished, 4K shows up -> upgrade without waiting 6 hours.
            clock.return_value=100600; qb.get_torrents.return_value=([{'progress':1.0}],False)
            self.feed('Show.S01E11.1080p.WEB-DL','Show.S01E11.2160p.WEB-DL')
            ns['query_library'].return_value=lib(11); self.run_check()
            self.assertEqual(len(Download.calls),2)
            self.assertIn('2160p',Download.calls[1]['context'].torrent_info.title)
            # The "4K" was mislabeled (library still 1080p); another 4K source must not trigger a 3rd download.
            clock.return_value=200000; qb.get_torrents.return_value=([],False)
            self.feed('Show.S01E11.2160p.HDR.WEB-DL'); self.run_check()
        self.assertEqual(len(Download.calls),2)
        self.assertIn('最多下载次数',self.plugin.data['decisions_v1'][-1]['reasons']['11'])
    def test_chase_same_run_downloads_only_the_best(self):
        self.chase()
        self.feed('Show.S01E11.1080p.WEB-DL','Show.S01E11.2160p.WEB-DL','Show.S01E11.720p.HDTV')
        ns['query_library'].return_value=self.chase_library()(10)
        DownloadHelper.services['QB'].instance.get_torrents.return_value=([{'progress':0.1}],False)
        self.run_check()
        self.assertEqual(len(Download.calls),1)
        self.assertIn('2160p',Download.calls[0]['context'].torrent_info.title)
    def test_chase_skips_intermediate_tiers(self):
        self.chase()
        self.feed('Show.S01E05.1080p.HDR.BluRay')
        ns['query_library'].return_value=self.chase_library()(10); self.run_check()
        self.assertFalse(Download.calls)
        self.assertIn('等待更高画质',self.plugin.data['decisions_v1'][-1]['reasons']['5'])
    def test_chase_never_touches_episodes_at_target(self):
        self.chase()
        self.feed('Show.S01E05.2160p.DV.REMUX')
        ns['query_library'].return_value=self.chase_library(uhd={5})(10); self.run_check()
        self.assertFalse(Download.calls)
    def test_chase_does_not_upgrade_while_low_version_downloading(self):
        self.chase(); lib=self.chase_library()
        qb=DownloadHelper.services['QB'].instance
        self.feed('Show.S01E11.1080p.WEB-DL'); ns['query_library'].return_value=lib(10); self.run_check()
        qb.get_torrents.return_value=([{'progress':0.4}],False)
        self.feed('Show.S01E11.2160p.WEB-DL'); self.run_check()
        self.assertEqual(len(Download.calls),1)
    def test_chase_waits_for_import_before_upgrade(self):
        # Finished downloading but not yet in the library: keep protection.
        self.chase(); lib=self.chase_library()
        qb=DownloadHelper.services['QB'].instance
        self.feed('Show.S01E11.1080p.WEB-DL'); ns['query_library'].return_value=lib(10); self.run_check()
        qb.get_torrents.return_value=([{'progress':1.0}],False)
        self.feed('Show.S01E11.2160p.WEB-DL'); self.run_check()
        self.assertEqual(len(Download.calls),1)
    def test_chase_refill_after_delete_ignores_cap(self):
        self.chase(); self.plugin._max_downloads=1
        clock=Mock(return_value=100000)
        with patch.object(time,'time',clock):
            self.feed('Show.S01E11.2160p.WEB-DL'); ns['query_library'].return_value=self.chase_library()(10)
            self.run_check()
            DownloadHelper.services['QB'].instance.get_torrents.return_value=([],False)
            clock.return_value=200000; self.run_check()
        self.assertEqual(len(Download.calls),2)
    def test_hdr_target(self):
        self.chase(); self.plugin._target_quality='2160p_hdr'
        self.feed('Show.S01E05.2160p.HDR.WEB-DL')
        ns['query_library'].return_value=self.chase_library(uhd={5})(10); self.run_check()
        self.assertEqual(Download.calls[0]['episodes'],{5})
    def test_old_default_config_migrates_to_chase(self):
        self.plugin.init_plugin({'enabled':True,'mode':'missing'})
        self.assertEqual(self.plugin._mode,'chase'); self.assertEqual(self.plugin.saved_config['mode'],'chase')
        self.assertEqual(self.plugin.saved_config['target_quality'],'2160p')
    def test_explicit_choices_are_not_migrated(self):
        self.plugin.init_plugin({'enabled':True,'mode':'upgrade'}); self.assertEqual(self.plugin._mode,'upgrade')
        self.plugin.init_plugin({'enabled':True,'mode':'missing','target_quality':'2160p'})
        self.assertEqual(self.plugin._mode,'missing')
    def test_audit_page_without_download_history(self):
        ns['query_library'].side_effect=engine.UncertainLibrary('offline')
        with patch.object(time,'sleep'):self.run_check()
        self.assertEqual(self.plugin.get_page()[0]['component'],'VCard')

if __name__=='__main__':unittest.main()
