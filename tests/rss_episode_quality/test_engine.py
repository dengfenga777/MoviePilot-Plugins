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
    def test_complete_pagination(self):
        def request(endpoint, params):
            if endpoint == 'Items': return self.payload([self.series()])
            all_items = [self.episode(id=f'e{i}', ep=i) for i in range(1, 6)]
            start = params['StartIndex']
            return {'Items': all_items[start:start+2], 'TotalRecordCount': 5}
        self.assertEqual(self.query(request).coverage(), {'1': [1, 2, 3, 4, 5]})
    def test_incomplete_page_never_means_missing(self):
        with self.assertRaises(e.UncertainLibrary): self.query(lambda *_: {'Items': [], 'TotalRecordCount': 3})
    def test_unknown_episode_number_is_uncertain(self):
        def request(endpoint, params):
            return self.payload([self.series()]) if endpoint == 'Items' else self.payload([{'Id': 'e1'}])
        with self.assertRaises(e.UncertainLibrary): self.query(request)
    def test_changing_page_count_is_uncertain(self):
        def request(endpoint, params):
            if endpoint == 'Items': return self.payload([self.series()])
            return {'Items': [self.episode()], 'TotalRecordCount': 2 if params['StartIndex']==0 else 3}
        with self.assertRaises(e.UncertainLibrary): self.query(request)

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
    def test_actual_stream_height_overrides_filename(self):
        q=e.quality('Show.1080p.WEB-DL.mkv',{'MediaSources':[{'MediaStreams':[{'Type':'Video','Height':1600,'Width':3840,'VideoRange':'HDR'}]}]})
        self.assertEqual(q,(2160,1,2))

class SelectionTests(unittest.TestCase):
    def files(self, names): return [{'id':i,'name':name} for i,name in enumerate(names)]
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

if __name__=='__main__': unittest.main()
