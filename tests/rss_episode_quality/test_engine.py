import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / 'plugins.v3' / 'rssepisodequality'
spec = importlib.util.spec_from_file_location('rss_policy', ROOT / 'engine.py')
e = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = e
spec.loader.exec_module(e)

class LibraryTests(unittest.TestCase):
    def query(self, responder):
        return e.EmbyLibrary(responder, page_size=2).query('tmdb', '301494', '余红旧事', 2026)
    def payload(self, items):
        return {'Items': items, 'TotalRecordCount': len(items)}
    def series(self, id='1', identity='301494'):
        return {'Id': id, 'Name': '余红旧事', 'ProductionYear': 2026,
                'ProviderIds': {'Tmdb': identity}}
    def episode(self, id='e1', ep=1, **extra):
        return {'Id': id, 'ParentIndexNumber': 1, 'IndexNumber': ep, **extra}
    def test_moviepilot_tmdb_source_alias_maps_to_emby_provider(self):
        calls=[]
        def request(endpoint, params):
            calls.append(params)
            return self.payload([])
        e.EmbyLibrary(request).query('themoviedb','301494','余红旧事',2026)
        self.assertEqual(calls[0]['AnyProviderIdEquals'],'tmdb.301494')
    def test_successful_absence(self):
        self.assertEqual(self.query(lambda *_: self.payload([])).coverage(), {})
    def test_no_items_field_is_failure(self):
        with self.assertRaises(e.UncertainLibrary): self.query(lambda *_: {'error': 'offline'})
    def test_invalid_count_is_failure(self):
        with self.assertRaises(e.UncertainLibrary): self.query(lambda *_: {'Items': [], 'TotalRecordCount': True})
    def test_wrong_id_filter_is_failure(self):
        with self.assertRaises(e.UncertainLibrary): self.query(lambda *_: self.payload([self.series(identity='other')]))
    def test_missing_provider_id_same_title_is_uncertain(self):
        def request(endpoint, params):
            return self.payload([{'Id': '1', 'Name': '余红旧事', 'ProductionYear': 2026}]) if 'SearchTerm' in params else self.payload([])
        with self.assertRaises(e.UncertainLibrary): self.query(request)
    def test_duplicate_series_union_and_multiepisode_file(self):
        def request(endpoint, params):
            if endpoint == 'Items': return self.payload([self.series('1'), self.series('2')])
            if endpoint == 'Shows/1/Episodes': return self.payload([self.episode(IndexNumberEnd=2)])
            return self.payload([self.episode(id='e3', ep=3)])
        self.assertEqual(self.query(request).coverage(), {'1': [1, 2, 3]})
    def test_complete_series_pagination(self):
        series = [self.series(id=str(i)) for i in range(1, 6)]
        def request(endpoint, params):
            if endpoint == 'Items':
                start = params['StartIndex']
                return {'Items': series[start:start+2], 'TotalRecordCount': 5}
            sid = endpoint.split('/')[1]
            return self.payload([self.episode(id=f'e{sid}', ep=int(sid))])
        self.assertEqual(self.query(request).coverage(), {'1': [1, 2, 3, 4, 5]})
    def test_episode_list_is_one_unpaged_request(self):
        calls = []
        def request(endpoint, params):
            if endpoint == 'Items': return self.payload([self.series()])
            calls.append(params)
            return self.payload([self.episode(id=f'e{i}', ep=i) for i in range(1, 6)])
        self.assertEqual(self.query(request).coverage(), {'1': [1, 2, 3, 4, 5]})
        self.assertEqual(len(calls), 1)
        self.assertNotIn('StartIndex', calls[0]); self.assertNotIn('Limit', calls[0])
    def test_multi_version_library_counts_episodes_not_versions(self):
        # 34 episodes, several versions each: Emby reports 34 but lists 280 version items.
        versions = [self.episode(id=f'v{n}', ep=(n % 34) + 1) for n in range(280)]
        def request(endpoint, params):
            if endpoint == 'Items': return self.payload([self.series()])
            return {'Items': versions, 'TotalRecordCount': 34}
        library = self.query(request)
        self.assertEqual(library.coverage(), {'1': list(range(1, 35))})
        self.assertEqual(sum(len(v) for v in library.episodes.values()), 280)
    def test_incomplete_page_never_means_missing(self):
        with self.assertRaises(e.UncertainLibrary): self.query(lambda *_: {'Items': [], 'TotalRecordCount': 3})
    def test_unknown_episode_number_is_uncertain(self):
        def request(endpoint, params):
            return self.payload([self.series()]) if endpoint == 'Items' else self.payload([{'Id': 'e1'}])
        with self.assertRaises(e.UncertainLibrary): self.query(request)
    def test_more_items_than_reported_total_is_accepted(self):
        # Emby search counts can under-report; extra items only make episodes look present.
        def request(endpoint, params):
            if endpoint == 'Items': return {'Items': [self.series()], 'TotalRecordCount': 0}
            return {'Items': [self.episode(id='e1', ep=1)], 'TotalRecordCount': 0}
        self.assertEqual(self.query(request).coverage(), {'1': [1]})
    def test_server_ignoring_limit_returns_everything(self):
        def request(endpoint, params):
            if endpoint == 'Items': return self.payload([self.series()])
            return self.payload([self.episode(id=f'e{i}', ep=i) for i in range(1, 6)])
        self.assertEqual(self.query(request).coverage(), {'1': [1, 2, 3, 4, 5]})
    def test_server_ignoring_start_index_is_uncertain(self):
        # Series search honors Limit but always returns the first page: the rest can never be read.
        def request(endpoint, params):
            if endpoint == 'Items':
                return {'Items': [self.series(id='1'), self.series(id='2')], 'TotalRecordCount': 5}
            return self.payload([])
        with self.assertRaises(e.UncertainLibrary) as ctx: self.query(request)
        self.assertIn('2 / 报告 5', str(ctx.exception))
    def test_fewer_items_than_reported_is_uncertain(self):
        def request(endpoint, params):
            if endpoint == 'Items': return self.payload([self.series()])
            return {'Items': [self.episode()], 'TotalRecordCount': 3}
        with self.assertRaises(e.UncertainLibrary): self.query(request)
    def test_duplicate_ids_within_one_page_are_kept(self):
        def request(endpoint, params):
            if endpoint == 'Items': return self.payload([self.series()])
            return {'Items': [self.episode(id='e1', ep=1)], 'TotalRecordCount': 1}
        e.EmbyLibrary(request, page_size=5).query('tmdb', '301494', '余红旧事', 2026)
    def test_changing_page_count_is_uncertain(self):
        def request(endpoint, params):
            if endpoint == 'Items':
                return {'Items': [self.series(id=str(params['StartIndex']+1)), self.series(id=str(params['StartIndex']+2))],
                        'TotalRecordCount': 4 if params['StartIndex']==0 else 5}
            return self.payload([])
        with self.assertRaises(e.UncertainLibrary): self.query(request)

    # ---- 1.5.0：第 0 集与缺编号条目不再阻止整部剧 ----
    def lib_with(self, episodes):
        def request(endpoint, params):
            if endpoint == 'Items': return self.payload([self.series()])
            return self.payload(episodes)
        return self.query(request)
    def test_episode_zero_special_is_ignored_not_fatal(self):
        lib = self.lib_with([{'Id': 's', 'ParentIndexNumber': 6, 'IndexNumber': 0, 'Path': '/tv/披荆斩棘 S06E00 1080p.strm'},
                             {'Id': 'a', 'ParentIndexNumber': 6, 'IndexNumber': 1}])
        self.assertEqual(lib.coverage(), {'6': [1]}); self.assertEqual(lib.unplaced, 0)
    def test_missing_numbers_fall_back_to_filename(self):
        lib = self.lib_with([{'Id': 'a', 'ParentIndexNumber': None, 'IndexNumber': None,
                              'Path': '/tv/Show/Season 1/Show.S01E07.2160p.WEB-DL.mkv'}])
        self.assertEqual(lib.coverage(), {'1': [7]})
    def test_unplaceable_item_is_counted_not_fatal(self):
        lib = self.lib_with([{'Id': 'a', 'ParentIndexNumber': None, 'IndexNumber': None, 'Path': '/tv/Show/extra.mkv'},
                             {'Id': 'b', 'ParentIndexNumber': 1, 'IndexNumber': 2}])
        self.assertEqual(lib.coverage(), {'1': [2]}); self.assertEqual(lib.unplaced, 1)
    def test_absurd_range_falls_back_to_filename(self):
        lib = self.lib_with([{'Id': 'a', 'ParentIndexNumber': 1, 'IndexNumber': 3, 'IndexNumberEnd': 1, 'Path': 'Show.S01E03.mkv'}])
        self.assertEqual(lib.coverage(), {'1': [3]})
    def test_special_filename_never_counts_as_episode(self):
        lib = self.lib_with([{'Id': 'a', 'ParentIndexNumber': None, 'IndexNumber': None, 'Path': 'Show.S01E00.mkv'},
                             {'Id': 'b', 'ParentIndexNumber': 1, 'IndexNumber': 1}])
        self.assertEqual(lib.coverage(), {'1': [1]}); self.assertEqual(lib.unplaced, 1)
    def test_all_items_unplaceable_is_uncertain(self):
        with self.assertRaises(e.UncertainLibrary):
            self.lib_with([{'Id': 'a', 'Path': 'extra1.mkv'}, {'Id': 'b', 'Path': 'extra2.mkv'}])
    def test_only_specials_is_empty_not_uncertain(self):
        lib = self.lib_with([{'Id': 's', 'ParentIndexNumber': 1, 'IndexNumber': 0}])
        self.assertEqual(lib.coverage(), {})
    def test_is_special(self):
        self.assertTrue(e.is_special('披荆斩棘 S06E00 2160p.strm'))
        self.assertFalse(e.is_special('Show.S01E05.mkv')); self.assertFalse(e.is_special('Show.S01E001.mkv'))

class DecisionTests(unittest.TestCase):
    def library(self, quality): return e.Library({(1, i): [quality] for i in range(1, 11)})
    def test_pack_only_selects_new_episodes(self):
        p = e.decide(range(1,13), 1, self.library((1080,0,2)), (2160,0,2), lambda q:q)
        self.assertEqual(p.selected, {11,12})
    def test_default_never_upgrades(self):
        self.assertFalse(e.decide([1],1,self.library((1080,0,2)),(2160,1,4),lambda q:q).selected)
    def test_balanced_upgrade(self):
        p=e.decide([1],1,self.library((1080,0,2)),(2160,0,2),lambda q:q,'upgrade',(0,1))
        self.assertEqual(p.upgrades,{1})
    def test_hdr_loss_is_not_balanced_upgrade(self):
        self.assertFalse(e.decide([1],1,self.library((1080,1,2)),(2160,0,2),lambda q:q,'upgrade',(0,1)).selected)
    def test_existing_best_version_prevents_upgrade(self):
        lib=e.Library({(1,1): [(1080,0,2),(2160,0,2)]})
        self.assertFalse(e.decide([1],1,lib,(2160,0,2),lambda q:q,'upgrade',(0,1)).selected)
    def test_unknown_quality_blocks_upgrade(self):
        self.assertFalse(e.decide([1],1,self.library(None),(2160,0,2),lambda q:q,'upgrade').selected)
    def test_unknown_hdr_blocks_balanced_upgrade(self):
        self.assertFalse(e.decide([1],1,self.library((1080,-1,2)),(2160,0,2),lambda q:q,'upgrade',(0,1)).selected)
    def test_deleted_episode_not_permanently_blocked_by_history(self):
        self.assertEqual(e.decide([1],1,e.Library(),(1080,0,2),lambda q:q).selected,{1})
    def chase(self, existing, candidate, target='2160p'):
        lib = e.Library({(1, 1): existing})
        return e.decide([1], 1, lib, candidate, lambda q: q, 'chase', target=e.TARGETS[target])
    def test_chase_upgrades_straight_to_target(self):
        self.assertEqual(self.chase([(1080, 0, 2)], (2160, 0, 2)).upgrades, {1})
    def test_chase_ignores_intermediate_tier(self):
        self.assertFalse(self.chase([(720, 0, 1)], (1080, 1, 3)).selected)
    def test_chase_any_version_at_target_stops(self):
        self.assertFalse(self.chase([(1080, 0, 2), (2160, 0, 2)], (2160, 1, 4)).selected)
    def test_chase_hdr_target(self):
        self.assertEqual(self.chase([(2160, 0, 2)], (2160, 1, 2), '2160p_hdr').upgrades, {1})
        self.assertFalse(self.chase([(2160, 0, 2)], (2160, 0, 4), '2160p_hdr').selected)
    def test_chase_unknown_existing_quality_is_kept(self):
        self.assertFalse(self.chase([None], (2160, 0, 2)).selected)
        self.assertFalse(self.chase([(2160, -1, 2)], (2160, 1, 2), '2160p_hdr').selected)
    def test_chase_missing_episode_downloads_any_quality(self):
        p = e.decide([2], 1, e.Library({(1, 1): [(1080, 0, 2)]}), (720, 0, 1), lambda q: q, 'chase', target=e.TARGETS['2160p'])
        self.assertEqual(p.missing, {2})
    def test_actual_stream_height_overrides_filename(self):
        q=e.quality('Show.1080p.WEB-DL.mkv',{'MediaSources':[{'MediaStreams':[{'Type':'Video','Height':1600,'Width':3840,'VideoRange':'HDR'}]}]})
        self.assertEqual(q,(2160,1,2))

class SelectionTests(unittest.TestCase):
    def files(self, names): return [{'id':i,'name':name} for i,name in enumerate(names)]
    def test_special_episode_file_is_skipped_not_fatal(self):
        enabled, disabled=e.select_files(self.files(['Show.S01E00.mkv','Show.S01E11.mkv','Show.S01E12.mkv']),1,{11,12})
        self.assertEqual(enabled,[1,2]); self.assertEqual(disabled,[0])
    def test_variant_files_are_not_selected(self):
        enabled, disabled=e.select_files(self.files(['节目.纯享.S01E11.Pure.mkv','节目.S01E11.mkv','节目.纯享.S01E11.Pure.ass']),1,{11})
        self.assertEqual(enabled,[1]); self.assertEqual(disabled,[0,2])
    def test_is_variant(self):
        for name in ('Natural High S04E06 Pure 2026 2160p','现在就出发.先导片.S04E02.Prologue.mkv','现在就出发.加更.S04E01.Plus.mkv','Show S01E03 [纯享版] 1080p'):
            self.assertTrue(e.is_variant(name),name)
        for name in ('Natural High S04E03 2026 2160p TX WEB-DL','Show S01E19 2160p 60Fps MAXPLUS HDRVivid','幕后之王 S01E03 1080p','Purely S01E02 1080p'):
            self.assertFalse(e.is_variant(name),name)
    def test_update_pack_and_subtitles(self):
        files=self.files([f'Show.S01E{i:02}.mkv' for i in range(1,13)]+['Show.S01E11.ass','readme.txt'])
        enabled, disabled=e.select_files(files,1,{11,12})
        self.assertEqual(enabled,[10,11,12]); self.assertEqual(len(disabled),11)
    def test_pack_folder_cannot_supply_episode_scope(self):
        with self.assertRaises(e.UnsafeSelection): e.select_files(self.files(['Show.S01E01-E12/unknown.mkv']),1,{11,12})
    def test_missing_episode_rejects_partial_plan(self):
        with self.assertRaises(e.UnsafeSelection): e.select_files(self.files(['S01E11.mkv']),1,{11,12})
    def test_multi_episode_file_cannot_mix_old_and_new(self):
        with self.assertRaises(e.UnsafeSelection): e.select_files(self.files(['S01E10-E12.mkv']),1,{11,12})
    def test_other_season_not_selected(self):
        enabled, disabled=e.select_files(self.files(['S02E11.mkv','S01E11.mkv']),1,{11})
        self.assertEqual(enabled,[1]); self.assertEqual(disabled,[0])
    def test_sample_not_selected(self):
        enabled, disabled=e.select_files(self.files(['S01E11.mkv','sample.S01E11.mkv']),1,{11})
        self.assertEqual(enabled,[0]); self.assertEqual(disabled,[1])
    def test_invalid_ids_refused(self):
        with self.assertRaises(e.UnsafeSelection): e.select_files([{'id':0,'name':'S01E11.mkv'},{'id':0,'name':'S01E12.mkv'}],1,{11,12})
    def test_year_is_not_episode(self):
        self.assertEqual(e.file_scope('Name2026.mkv',1),(1,set()))
    def test_unknown_season_is_not_guessed_in_scope_fallback(self):
        self.assertEqual(e.file_scope('E11.mkv',None),(None,{11}))
    def test_multi_episode_filename(self):
        self.assertEqual(e.file_scope('Show.S01E11E12.mkv',None),(1,{11,12}))
    def test_conflicting_seasons_refused(self):
        with self.assertRaises(e.UnsafeSelection): e.file_scope('Season 02/Show.S01E11.mkv',None)

class EpisodeNamingTests(unittest.TestCase):
    """1.2.0: common Chinese-drama and fansub naming must not stall a pack forever."""
    def scope(self, name): return e.file_scope(name, 1)[1]
    def test_ep_prefix(self):
        self.assertEqual(self.scope('某剧.EP05.2160p.WEB-DL.mkv'), {5})
        self.assertEqual(self.scope('某剧.S01EP05.mkv'), {5})
        self.assertEqual(self.scope('某剧.EP.05.mkv'), {5})
        self.assertEqual(self.scope('某剧.EP01-EP03.mkv'), {1, 2, 3})
    def test_chinese_hua(self):
        self.assertEqual(self.scope('某剧 第5话.mkv'), {5})
    def test_fansub_dash_and_bracket(self):
        self.assertEqual(self.scope('[Lilith-Raws] Show - 05 [1080p].mkv'), {5})
        self.assertEqual(self.scope('Show - 05v2 (1080p).mkv'), {5})
        self.assertEqual(self.scope('[Grp][Show][12v2][720p].mkv'), {12})
        self.assertEqual(self.scope('[Grp][Show][720][05].mkv'), {5})
    def test_ambiguous_bare_numbers_refused(self):
        self.assertEqual(self.scope('[Grp][Show][01][02].mkv'), set())
    def test_codec_tokens_are_not_episodes(self):
        self.assertEqual(self.scope('Show.S01E05.DDP5.1.E-AC3.mkv'), {5})
        self.assertEqual(self.scope('Show.2160p.HE 10.mkv'), set())
        self.assertEqual(self.scope('x264-Group.mkv'), set())
    def test_explicit_episode_wins_over_fansub_number(self):
        self.assertEqual(self.scope('Show - 03 - S01E05.mkv'), {5})
    def test_ep_pack_selects_only_missing(self):
        files = [{'id': i, 'name': f'某剧/某剧.EP{i+1:02}.2160p.mkv'} for i in range(12)]
        enabled, disabled = e.select_files(files, 1, {11, 12})
        self.assertEqual(enabled, [10, 11]); self.assertEqual(len(disabled), 10)
    def test_subtitle_in_episode_folder(self):
        files = [{'id': 0, 'name': 'Show/Show.S01E05.mkv'}, {'id': 1, 'name': 'Show/Subs/Show.S01E05/2_English.srt'},
                 {'id': 2, 'name': 'Show/Show.S01E04.mkv'}, {'id': 3, 'name': 'Show/Subs/Show.S01E04/2_English.srt'}]
        enabled, disabled = e.select_files(files, 1, {5})
        self.assertEqual(enabled, [0, 1]); self.assertEqual(disabled, [2, 3])
    def test_subtitle_in_range_folder_not_selected(self):
        self.assertEqual(e.subtitle_scope('Show.S01E01-E12/Subs/en.srt', 1), (1, set()))

class SiteTests(unittest.TestCase):
    def test_keyword_order(self):
        keys = ['hdsky', 'ourbits']
        self.assertEqual(e.site_score(['https://hdsky.me/rss'], keys, 5), (2, 'hdsky'))
        self.assertEqual(e.site_score(['https://ourbits.club/rss'], keys, 0), (1, 'ourbits'))
    def test_download_link_host_also_matches(self):
        self.assertEqual(e.site_score(['https://proxy.local/feed', None, 'https://hdsky.me/dl/1'], ['hdsky'], 0)[0], 1)
    def test_unlisted_is_lowest(self):
        self.assertEqual(e.site_score(['https://x.example/rss'], ['hdsky'], 0)[0], 0)
    def test_rss_order_without_list(self):
        self.assertGreater(e.site_score(['https://a/rss'], [], 0)[0], e.site_score(['https://b/rss'], [], 1)[0])
    def test_bad_urls_ignored(self):
        self.assertEqual(e.site_score(['not a url', None], [], 0), (0, ''))

class RankTests(unittest.TestCase):
    def test_rank_shares_quality_parser(self):
        title = 'Show.S01E05.2160p.WEB-DL.DV.H.265'
        self.assertEqual(e.rank(title, 3)[:4], (3, 2160, 2, 1))
        self.assertEqual(e.quality(title), (2160, 1, 2))
    def test_bad_priority_is_zero(self):
        self.assertEqual(e.rank('x', 'bad')[0], 0)

if __name__=='__main__': unittest.main()
