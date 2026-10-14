import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from torrentool.api import Torrent
from runtime_support import load

engine=load('engine'); adapter=load('adapters')

def torrent(names=None):
    names=names or [f'Show.S01E{i:02}.mkv' for i in range(1,13)]
    return Torrent({'info':{'name':'Show','files':[{'length':1,'path':[name]} for name in names],
                    'piece length':16384,'pieces':b'0'*20}}).to_string()

class QbTests(unittest.TestCase):
    def setup_service(self):
        files=[{'id':i,'name':f'Show/Show.S01E{i+1:02}.mkv','priority':1} for i in range(12)]
        service=SimpleNamespace(type='qbittorrent',name='QB',module=SimpleNamespace(normalize_path=lambda p,n:str(p)))
        server=Mock(); service.instance=server
        server.get_torrents.side_effect=lambda **kw: ([],False) if not getattr(server,'added',False) else ([{'tags':server.tag,'state':'pausedDL'}],False)
        server.added=False
        def add(**kwargs): server.added=True; server.tag=kwargs['tag'][-1]; return True,[]
        server.add_torrent.side_effect=add
        server.get_files.side_effect=lambda *a,**kw:copy.deepcopy(files)
        def set_files(**kwargs):
            for f in files:
                if f['id'] in kwargs['file_ids']: f['priority']=kwargs['priority']
            return True
        server.set_files.side_effect=set_files
        server.start_torrents.return_value=True
        server.stop_torrents.return_value=True
        server.get_content_layout.return_value='Original'
        return service,files
    def submit(self, service, content=None):
        return adapter.submit_selected(service,content or torrent(),Path('/downloads'),None,'TV',None,1,{11,12})
    def test_paused_verified_selection_before_start(self):
        service,files=self.setup_service()
        result=self.submit(service)
        self.assertEqual(result[0],'QB')
        self.assertTrue(service.instance.add_torrent.call_args.kwargs['is_paused'])
        self.assertEqual([f['id'] for f in files if f['priority']>0],[10,11])
        service.instance.start_torrents.assert_called_once()
        service.instance.stop_torrents.assert_not_called()
    def test_upgrade_filename_mismatch_refused_before_add(self):
        service,_=self.setup_service()
        with self.assertRaises(engine.UnsafeSelection):
            adapter.submit_selected(service,torrent(),Path('/downloads'),None,'TV',None,1,{11,12},
                                    {'episodes':{11},'quality':(2160,0,2),'axes':(0,1)})
        service.instance.add_torrent.assert_not_called()
    def test_upgrade_file_metadata_supports_advertised_grade(self):
        service,files=self.setup_service()
        for file in files:file['name']=file['name'].replace('.mkv','.2160p.WEB-DL.mkv')
        result=adapter.submit_selected(service,torrent([f'Show.S01E{i:02}.2160p.WEB-DL.mkv' for i in range(1,13)]),
                       Path('/downloads'),None,'TV',None,1,{11,12},
                       {'episodes':{11},'quality':(2160,0,2),'axes':(0,1)})
        self.assertEqual(result[0],'QB')
    def test_waits_for_metadata_check_to_finish_paused(self):
        service,_=self.setup_service()
        original=service.instance.get_torrents.side_effect
        seen=[0]
        def tasks(**kwargs):
            result=original(**kwargs)
            if result[0] and seen[0]==0:
                seen[0]+=1; result[0][0]['state']='checkingResumeData'
            return result
        service.instance.get_torrents.side_effect=tasks
        with patch.object(adapter.time,'sleep'):self.submit(service)
        service.instance.start_torrents.assert_called_once()
    def test_builtin_tag_keeps_moviepilot_transfer_detection(self):
        service,_=self.setup_service()
        adapter.submit_selected(service,torrent(),Path('/downloads'),None,'TV',None,1,{11,12},builtin_tag='MoviePilot')
        self.assertIn('MoviePilot',service.instance.add_torrent.call_args.kwargs['tag'])
    def test_explicit_labels_preserved(self):
        service,_=self.setup_service()
        adapter.submit_selected(service,torrent(),Path('/downloads'),None,'TV','RSS, TV',1,{11,12},builtin_tag='MoviePilot')
        self.assertEqual(service.instance.add_torrent.call_args.kwargs['tag'][:2],['RSS','TV'])
    def test_set_priority_failure_never_starts(self):
        service,_=self.setup_service(); service.instance.set_files.side_effect=lambda **kw:False
        with self.assertRaises(engine.UnsafeSelection): self.submit(service)
        service.instance.start_torrents.assert_not_called(); service.instance.stop_torrents.assert_called_once()
    def test_wrong_priority_readback_never_starts(self):
        service,_=self.setup_service(); service.instance.set_files.side_effect=lambda **kw:True
        with patch.object(adapter.time,'sleep'):
            with self.assertRaises(engine.UnsafeSelection): self.submit(service)
        service.instance.start_torrents.assert_not_called()
    def test_existing_task_never_modified(self):
        service,_=self.setup_service(); service.instance.get_torrents.side_effect=lambda **kw:([{'hash':'existing'}],False)
        with self.assertRaises(engine.UnsafeSelection): self.submit(service)
        service.instance.add_torrent.assert_not_called(); service.instance.set_files.assert_not_called(); service.instance.stop_torrents.assert_not_called()
    def test_downloader_query_failure_does_not_add(self):
        service,_=self.setup_service(); service.instance.get_torrents.side_effect=lambda **kw:([],True)
        with self.assertRaises(engine.UnsafeSelection): self.submit(service)
        service.instance.add_torrent.assert_not_called()
    def test_magnet_refused_before_add(self):
        service,_=self.setup_service()
        with self.assertRaises(engine.UnsafeSelection): self.submit(service,b'magnet:?xt=urn:btih:example')
        service.instance.add_torrent.assert_not_called()
    def test_unmapped_video_refused_before_add(self):
        service,_=self.setup_service()
        with self.assertRaises(engine.UnsafeSelection): self.submit(service,torrent(['unknown.mkv']))
        service.instance.add_torrent.assert_not_called()
    def test_missing_live_file_never_starts(self):
        service,_=self.setup_service(); service.instance.get_files.side_effect=lambda *a,**kw:None
        with self.assertRaises(engine.UnsafeSelection): self.submit(service)
        service.instance.start_torrents.assert_not_called()
    def test_unowned_task_not_paused_or_modified(self):
        service,_=self.setup_service()
        service.instance.get_torrents.side_effect=[([],False),([{'state':'pausedDL','tags':'other'}],False)]
        with self.assertRaises(engine.UnsafeSelection): self.submit(service)
        service.instance.set_files.assert_not_called(); service.instance.stop_torrents.assert_not_called()
    def test_unsupported_downloader_refused(self):
        service,_=self.setup_service(); service.type='transmission'
        with self.assertRaises(engine.UnsafeSelection): self.submit(service)
        service.instance.add_torrent.assert_not_called()
    def test_missing_layout_does_not_start(self):
        service,_=self.setup_service(); service.instance.get_content_layout.return_value=None
        with self.assertRaises(engine.UnsafeSelection): self.submit(service)
        service.instance.start_torrents.assert_not_called()
    def test_start_rejected_keeps_paused(self):
        service,_=self.setup_service(); service.instance.start_torrents.return_value=False
        with self.assertRaises(engine.UnsafeSelection): self.submit(service)
        service.instance.stop_torrents.assert_called_once()

class HttpTests(unittest.TestCase):
    def service(self):
        return {'EMBY':SimpleNamespace(type='emby',config=SimpleNamespace(config={'host':'https://example.invalid/','apikey':'secret-key'}))}
    def media(self):
        return SimpleNamespace(media_source=SimpleNamespace(value='tmdb'),media_id='301494',title='余红旧事',year=2026)
    def test_http_401_is_failure_not_absence(self):
        with patch('requests.get',return_value=SimpleNamespace(status_code=401)):
            with self.assertRaises(engine.UncertainLibrary): adapter.query_library(self.service(),self.media())
    def test_http_500_is_failure_not_absence(self):
        with patch('requests.get',return_value=SimpleNamespace(status_code=500)):
            with self.assertRaises(engine.UncertainLibrary): adapter.query_library(self.service(),self.media())
    def test_invalid_json_is_failure(self):
        response=Mock(status_code=200); response.json.side_effect=ValueError()
        with patch('requests.get',return_value=response):
            with self.assertRaises(engine.UncertainLibrary): adapter.query_library(self.service(),self.media())
    def test_success_empty_and_key_in_header(self):
        response=Mock(status_code=200); response.json.return_value={'Items':[],'TotalRecordCount':0}
        with patch('requests.get',return_value=response) as get:
            self.assertEqual(adapter.query_library(self.service(),self.media()).coverage(),{})
            self.assertEqual(get.call_args.kwargs['headers'],{'X-Emby-Token':'secret-key'})
            self.assertNotIn('api_key',get.call_args.kwargs['params'])
    def test_network_failure_has_no_secret_in_message(self):
        import requests
        with patch('requests.get',side_effect=requests.ConnectionError('https://host?api_key=secret-key')):
            with self.assertRaises(engine.UncertainLibrary) as cm: adapter.query_library(self.service(),self.media())
            self.assertNotIn('secret',str(cm.exception))
    def test_missing_server_refuses_download(self):
        with self.assertRaises(engine.UncertainLibrary): adapter.query_library({},self.media())
    def test_one_failed_server_invalidates_entire_result(self):
        services=self.service(); services['PLEX']=SimpleNamespace(type='plex')
        response=Mock(status_code=200); response.json.return_value={'Items':[],'TotalRecordCount':0}
        with patch('requests.get',return_value=response):
            with self.assertRaises(engine.UncertainLibrary): adapter.query_library(services,self.media())

if __name__=='__main__':unittest.main()
