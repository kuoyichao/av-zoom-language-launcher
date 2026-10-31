import importlib.util
import json
import sys
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('launcher', Path(__file__).resolve().parents[1] / 'zoom_language_launcher.py')
z = importlib.util.module_from_spec(spec)
spec.loader.exec_module(z)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        z.DATA_DIR = Path(self.tmp.name)
        z.SESSIONS_FILE = z.DATA_DIR / 'zoom_sessions.json'
        z.LANGUAGES_FILE = z.DATA_DIR / 'zoom_languages.json'
        self.server = z.ThreadingHTTPServer(('127.0.0.1', 0), z.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.data = dict(window_actions={'Chinese':{'pin_self':False,'mini_window':False},'Italian':{'pin_self':False,'mini_window':False}},apply_audio=False, session_name='Sunday conference', meeting='https://example.zoom.us/j/12345678901?pwd=token%2Bx',
                         password='', languages=['Chinese', 'Italian'], names={'Chinese': '*Z N Chinese', 'Italian': '*I T Italian'},
                         catalogue=[dict(language='Chinese', name='*Z N Chinese'), dict(language='Italian', name='*I T Italian')])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def api(self, path, data=None, authorized=True):
        headers = {'X-Launcher-Token': z.TOKEN} if authorized else {}
        req = urllib.request.Request(self.base+path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_custom_languages_and_named_sessions_persist(self):
        self.assertEqual(self.api('/save', self.data)[0], 200)
        state = self.api('/state')[1]
        self.assertEqual(state['sessions'][0]['languages'], ['Chinese', 'Italian'])
        self.assertEqual(state['languages'][1]['name'], '*I T Italian')
        self.data['password'] = 'new&password'
        self.assertEqual(self.api('/save', self.data)[0], 200)
        self.assertEqual(len(z.read_sessions()), 1)
        self.assertEqual(z.read_sessions()[0]['password'], 'new&password')
        self.assertEqual(z.SESSIONS_FILE.stat().st_mode & 0o777, 0o600)

    def test_import_v1_and_invalid_import_is_atomic(self):
        old = dict(self.data)
        old.pop('catalogue')
        self.assertEqual(self.api('/import', {'sessions': [old]})[0], 200)
        before = z.SESSIONS_FILE.read_bytes()
        self.assertEqual(self.api('/import', {'sessions': [old, {'invalid': True}]})[0], 400)
        self.assertEqual(z.SESSIONS_FILE.read_bytes(), before)

    def test_catalogue_duplicate_and_html_is_not_interpolated(self):
        self.assertEqual(self.api('/languages', {'languages': [{'language':'<Italian>', 'name':'<test>'}]})[0], 200)
        self.assertEqual(z.read_languages()[0]['language'], '<Italian>')
        self.assertEqual(self.api('/languages', {'languages': [{'language':'Italian','name':'a'}, {'language':'italian','name':'b'}]})[0], 400)
        self.assertNotIn('MHz', z.HTML)

    def test_launch_commands_and_names(self):
        with patch.object(z, 'find_zoom', return_value='/Applications/zoom.us.app'), patch.object(z.subprocess,'run') as run, patch.object(z.time,'sleep'):
            run.return_value.returncode = 0
            self.assertEqual(self.api('/launch', self.data)[0], 200)
            self.assertEqual(run.call_count, 2)
            args = run.call_args_list[1].args[0]
            self.assertEqual(args[:4], ['/usr/bin/open','-n','-a','/Applications/zoom.us.app'])
            query = parse_qs(urlparse(args[-1]).query)
            self.assertEqual(query['uname'], ['*I T Italian'])
            self.assertEqual(query['pwd'], ['token+x'])
        args = z.zoom_command(r'C:\Zoom\Zoom.exe', 'zoom.us','12345678901','p&x','*I T Italian',platform='win32')
        self.assertEqual(len(args), 3)
        self.assertEqual(args[1], '/secondary')
        self.assertTrue(args[2].startswith('--url=zoommtg://'))
        self.assertIn('pwd=p%26x', args[2])

    def test_audio_defaults_persistence_and_session_import(self):
        self.assertEqual({r['language']:r['input_channel'] for r in z.read_languages()},z.DEFAULT_CHANNELS)
        self.assertTrue(all(r['audio_input']=='auto' for r in z.read_languages()))
        self.data['catalogue'][1].update(audio_input='Loopback Audio',input_channel=30)
        self.data['audio']={'Chinese':{'audio_input':'auto','input_channel':18},'Italian':{'audio_input':'Loopback Audio','input_channel':30}}
        self.assertEqual(self.api('/save',self.data)[0],200)
        self.assertEqual(z.read_languages()[1]['input_channel'],30)
        self.assertEqual(z.read_sessions()[0]['audio']['Italian']['audio_input'],'Loopback Audio')
        records=z.read_sessions()
        self.assertEqual(self.api('/import',{'sessions':records})[0],200)
        self.assertEqual(z.read_sessions()[0]['audio']['Italian']['input_channel'],30)
        for invalid in [0,-1,1.5,True,257]:
            self.assertEqual(self.api('/languages',{'languages':[{'language':'Russian','name':'Russian','input_channel':invalid}]})[0],400)

    def test_native_audio_inventory_prefers_loopback(self):
        payload={'SPAudioDataType':[{'_items':[{'_name':'Microphone','coreaudio_device_input':1,'coreaudio_default_audio_input_device':'spaudio_yes'},{'_name':'Loopback Audio','coreaudio_device_input':36},{'_name':'Speakers','coreaudio_device_output':2}]}]}
        with patch.object(z.sys,'platform','darwin'),patch.object(z.subprocess,'run') as run:
            run.return_value.stdout=json.dumps(payload)
            result=z.audio_inputs()
            self.assertEqual(result['preferred'],'Loopback Audio')
            self.assertEqual(len(result['devices']),2)
            self.assertEqual(result['devices'][1]['channels'],36)
        with patch.object(z.sys,'platform','win32'):
            self.assertEqual(z.audio_inputs()['source'],'browser')
        self.assertEqual(self.api('/audio-inputs',authorized=False)[0],404)

    def test_launch_window_actions_report_their_actual_result(self):
        self.data['languages']=['Chinese']
        self.data['window_actions']={'Chinese':{'pin_self':True,'mini_window':True}}
        with patch.object(z,'find_zoom',return_value='/Applications/zoom.us.app'),patch.object(z.subprocess,'run') as run,patch.object(z.time,'sleep'),patch.object(z,'zoom_pids',return_value={1}),patch.object(z,'new_client_pid',return_value=4321),patch.object(z,'apply_window_actions',return_value={'ok':False,'message':'Pin is unavailable; window left open.'}) as apply:
            run.return_value.returncode=0
            code,result=self.api('/launch',self.data)
            self.assertEqual(code,200)
            self.assertIn('Pin is unavailable; window left open.',result['message'])
            apply.assert_called_once_with('*Z N Chinese',{'pin_self':True,'mini_window':True},pid=4321)

    def test_real_audio_routing_verifies_each_process(self):
        self.data['apply_audio']=True
        self.data['languages']=['Chinese']
        with patch.object(z,'find_zoom',return_value='/Applications/zoom.us.app'),patch.object(z,'accessibility_ready',return_value=True) as ready,patch.object(z,'zoom_pids',return_value={1}),patch.object(z,'new_client_pid',return_value=4321),patch.object(z,'resolve_audio_device',return_value='Loopback Audio'),patch.object(z,'configure_audio',return_value={'ok':True,'message':'Only channel 18 verified.'}) as configure,patch.object(z.subprocess,'run') as run:
            ready.return_value=True
            run.return_value.returncode=0
            code,result=self.api('/launch',self.data)
            self.assertEqual(code,200)
            self.assertEqual(configure.call_count,2)
            configure.assert_any_call(4321,'Loopback Audio',18)
            configure.assert_any_call(4321,'Loopback Audio',18,inspect_only=True)
            self.assertIn('Only channel 18 verified.',result['message'])

    def test_missing_permission_or_audio_failure_does_not_block_meetings(self):
        self.data['apply_audio']=True
        with patch.object(z,'find_zoom',return_value='/Applications/zoom.us.app'),patch.object(z,'accessibility_ready',return_value=True) as ready,patch.object(z.subprocess,'run') as run,patch.object(z.time,'sleep'):
            ready.return_value=False
            run.return_value.returncode=0
            code,result=self.api('/launch',self.data)
            self.assertEqual(code,200)
            self.assertEqual(run.call_count,2)
            self.assertIn('Accessibility permission is missing',result['message'])
            self.assertIn('NOT applied',result['message'])
        with patch.object(z,'find_zoom',return_value='/Applications/zoom.us.app'),patch.object(z,'accessibility_ready',return_value=True) as ready,patch.object(z,'zoom_pids',return_value={1}),patch.object(z,'new_client_pid',return_value=4321),patch.object(z,'resolve_audio_device',return_value='Loopback Audio'),patch.object(z,'configure_audio',return_value={'ok':False,'message':'Channel not confirmed.'}),patch.object(z.subprocess,'run') as run:
            ready.return_value=True
            run.return_value.returncode=0
            code,result=self.api('/launch',self.data)
            self.assertEqual(code,200)
            self.assertEqual(run.call_count,2)
            self.assertIn('Channel not confirmed',result['message'])
            self.assertIn('meetings remain open',result['message'])

    def test_window_defaults_are_on_and_migrate_only_once(self):
        self.assertEqual(z.window_settings({}),{'pin_self':True,'mini_window':True})
        self.assertEqual(self.api('/save',self.data)[0],200)
        z.migrate_window_defaults()
        self.assertTrue(all(r['pin_self'] and r['mini_window'] for r in z.read_languages()))
        self.assertTrue(all(r['pin_self'] and r['mini_window'] for r in z.read_sessions()[0]['window_actions'].values()))
        rows=z.read_languages();rows[0].update(pin_self=False,mini_window=False);z.atomic_json(z.LANGUAGES_FILE,rows)
        z.migrate_window_defaults()
        self.assertFalse(z.read_languages()[0]['pin_self'])

    def test_window_options_persist_and_validate(self):
        self.data['window_actions']={'Chinese':{'pin_self':True,'mini_window':True},'Italian':{'pin_self':False,'mini_window':True}}
        self.data['catalogue'][0].update(pin_self=True,mini_window=True)
        self.assertEqual(self.api('/save',self.data)[0],200)
        self.assertTrue(z.read_languages()[0]['pin_self'])
        self.assertTrue(z.read_sessions()[0]['window_actions']['Chinese']['mini_window'])
        self.assertEqual(self.api('/import',{'sessions':z.read_sessions()})[0],200)
        self.assertEqual(self.api('/languages',{'languages':[{'language':'Russian','name':'Russian','pin_self':'yes'}]})[0],400)
        with patch.object(z,'apply_window_actions',return_value={'ok':False,'message':'Self profile not found.'}) as apply:
            result=self.api('/window-actions',{'name':'*Z N Chinese','pin_self':True,'mini_window':True})
            self.assertEqual(result[0],200)
            self.assertFalse(result[1]['ok'])
            apply.assert_called_once()
        self.assertEqual(self.api('/window-actions',{'name':'*Z N Chinese'},authorized=False)[0],403)

    def test_reject_unauthorized_and_non_zoom_link(self):
        self.assertEqual(self.api('/state', authorized=False)[0], 404)
        self.assertEqual(self.api('/save', self.data, authorized=False)[0], 403)
        self.data['meeting'] = 'https://zoom.us.evil.example/j/12345678901'
        self.assertEqual(self.api('/save', self.data)[0], 400)
        self.assertFalse(z.SESSIONS_FILE.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
