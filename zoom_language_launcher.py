#!/usr/bin/env python3
"""Zoom Language Launcher: local UI, persistent sessions, editable languages."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
import ctypes
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from zoom_window_actions import apply_window_actions
from zoom_audio_routing import AX, RoutingError, zoom_pids, new_client_pid, configure_audio, accessibility_ready, close_idle_test_client

VERSION = '2.4.3'
DEFAULT_NAMES = {
    'Chinese': '*Z N Chinese', 'Dutch': '*N L Dutch', 'French': '*F R French',
    'Spanish': '*E S Spanish', 'Polish': '*P L polski', 'Portuguese': '*P R Portuguese',
    'Romanian': '*R O Romanian', 'German': '*D E German', 'Russian': '*R U Russian',
}
DEFAULT_CHANNELS = {
    'Russian': 17, 'Chinese': 18, 'Dutch': 19, 'French': 20, 'Spanish': 21,
    'Polish': 22, 'Portuguese': 23, 'Romanian': 24, 'German': 25,
}


def window_settings(row):
    result = {key: row.get(key, True) for key in ('pin_self', 'mini_window')}
    if any(type(value) is not bool for value in result.values()):
        raise ValueError('Pin profile and mini window options must be on or off.')
    return result


def audio_settings(row, language):
    device = row.get('audio_input', 'auto')
    channel = row.get('input_channel', DEFAULT_CHANNELS.get(language, 1))
    if not isinstance(device, str) or not device.strip() or len(device) > 256:
        raise ValueError('Choose an audio input device.')
    if type(channel) is not int or not 1 <= channel <= 256:
        raise ValueError('Input channel must be a whole number between 1 and 256 (numbered from 1).')
    return dict(audio_input=device.strip(), input_channel=channel)


def audio_inputs():
    # Read-only device inventory; never starts audio capture or changes Zoom.
    if sys.platform != 'darwin':
        return dict(devices=[], source='browser', preferred='auto')
    result = subprocess.run(['/usr/sbin/system_profiler', 'SPAudioDataType', '-json'],
                            capture_output=True, text=True, timeout=15, check=True)
    payload = json.loads(result.stdout)
    devices = []
    def visit(value):
        if isinstance(value, dict):
            channels = value.get('coreaudio_device_input', 0)
            if isinstance(channels, int) and channels > 0 and value.get('_name'):
                devices.append(dict(label=value['_name'], channels=channels,
                                    default=value.get('coreaudio_default_audio_input_device') == 'spaudio_yes'))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(payload)
    preferred = next((d['label'] for d in devices if d['label'].casefold() == 'loopback audio'),
                     next((d['label'] for d in devices if d['default']), 'system'))
    return dict(devices=devices, source='macOS', preferred=preferred)


TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
PROGRESS = dict(message='')
AUDIO_CLIENTS = {}
WINDOW_CLIENTS = {}


def documents_folder():
    if sys.platform == 'win32':
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
            return Path(buffer.value)
    return Path.home() / 'Documents'


def data_folder():
    if os.environ.get('ZOOM_LAUNCHER_DATA_DIR'):
        return Path(os.environ['ZOOM_LAUNCHER_DATA_DIR']).expanduser()
    if getattr(sys, 'frozen', False) or sys.platform == 'win32':
        return documents_folder() / 'Zoom Language Launcher'
    return Path(__file__).resolve().parent


DATA_DIR = data_folder()
SESSIONS_FILE = DATA_DIR / 'zoom_sessions.json'
LANGUAGES_FILE = DATA_DIR / 'zoom_languages.json'


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.zoom-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_catalogue(rows):
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('Use a list of up to 100 languages.')
    result, seen = [], set()
    for row in rows:
        label, name = row.get('language', ''), row.get('name', '')
        if not isinstance(label, str) or not label.strip() or len(label) > 64:
            raise ValueError('Enter a language name of up to 64 characters.')
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('Enter a participant name of up to 100 characters.')
        label, name = label.strip(), name.strip()
        if label.casefold() in seen:
            raise ValueError('Language names must be unique.')
        seen.add(label.casefold())
        result.append(dict(language=label, name=name, **audio_settings(row, label), **window_settings(row)))
    return result


def read_languages():
    return validate_catalogue(read_json(LANGUAGES_FILE,
        [dict(language=k, name=v) for k, v in DEFAULT_NAMES.items()]))


def read_sessions():
    rows = read_json(SESSIONS_FILE, [])
    if not isinstance(rows, list):
        raise ValueError('The sessions file must contain a list.')
    return rows


def meeting_details(value, password):
    if not isinstance(value, str) or not isinstance(password, str):
        raise ValueError('Enter a meeting link and passcode as text.')
    value = value.strip()
    if re.fullmatch(r'[\d\s-]+', value):
        meeting_id, domain = re.sub(r'\D', '', value), 'zoom.us'
    else:
        parsed = urlparse(value)
        domain = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or not (domain == 'zoom.us' or domain.endswith('.zoom.us') or domain == 'zoomgov.com' or domain.endswith('.zoomgov.com')):
            raise ValueError('Enter an HTTPS Zoom meeting link or a meeting ID.')
        match = re.fullmatch(r'/(?:j|s|w)/([0-9]+)/?', parsed.path)
        if not match:
            raise ValueError('Use a direct Zoom meeting link (/j/meeting-ID), or enter the meeting ID.')
        meeting_id = match.group(1)
        if not password:
            password = parse_qs(parsed.query).get('pwd', [''])[0]
    if not 9 <= len(meeting_id) <= 11:
        raise ValueError('The meeting ID must contain 9–11 digits.')
    return domain, meeting_id, password


def validate_session(data):
    name = data.get('session_name', '')
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError('Enter a session name (up to 120 characters).')
    meeting_details(data['meeting'], data['password'])
    languages, names = data['languages'], data['names']
    if not isinstance(languages, list) or not 1 <= len(languages) <= 100 or not isinstance(names, dict):
        raise ValueError('Select at least one language and enter participant names.')
    audio = data.get('audio', {})
    if not isinstance(audio, dict):
        raise ValueError('Invalid audio settings.')
    windows = data.get('window_actions', {})
    if not isinstance(windows, dict):
        raise ValueError('Invalid window options.')
    selected = validate_catalogue([dict(language=k, name=names.get(k, ''), **window_settings(windows.get(k, {})),
        **audio_settings(audio.get(k, {}), k)) for k in languages])
    apply_audio = data.get('apply_audio', sys.platform == 'darwin')
    if type(apply_audio) is not bool:
        raise ValueError('Apply audio must be on or off.')
    return dict(apply_audio=apply_audio, session_name=name.strip(), meeting=data['meeting'].strip(), password=data['password'],
                languages=[r['language'] for r in selected],
                names={r['language']: r['name'] for r in selected},
                audio={r['language']: audio_settings(r, r['language']) for r in selected},
                window_actions={r['language']: window_settings(r) for r in selected},
                saved_at=datetime.now(timezone.utc).isoformat())


def save_session(record):
    sessions = [record] + [s for s in read_sessions()
                           if s['session_name'].casefold() != record['session_name'].casefold()]
    atomic_json(SESSIONS_FILE, sessions)


def resolve_audio_device(choice, channel=None):
    inventory=audio_inputs()
    devices=inventory['devices']
    if choice=='auto':
        choice=inventory['preferred']
    elif choice=='system':
        choice=next((d['label'] for d in devices if d.get('default')),None)
    device=next((d for d in devices if d['label']==choice),None)
    if not device:
        raise ValueError('Audio input is unavailable: '+str(choice))
    if channel is not None and device.get('channels') and channel>device['channels']:
        raise ValueError(str(choice)+' has '+str(device['channels'])+' input channels; channel '+str(channel)+' is unavailable.')
    return device['label']


def find_zoom(platform=None):
    platform = platform or sys.platform
    if platform == 'win32':
        candidates = [os.environ.get('ZOOM_APP', '')]
        for variable in ['APPDATA', 'LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)']:
            if os.environ.get(variable):
                candidates.append(str(Path(os.environ[variable]) / 'Zoom' / 'bin' / 'Zoom.exe'))
        try:
            import winreg
            for root in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
                try:
                    with winreg.OpenKey(root, r'Software\Microsoft\Windows\CurrentVersion\App Paths\Zoom.exe') as key:
                        candidates.append(winreg.QueryValue(key, None))
                except OSError:
                    pass
        except ImportError:
            pass
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
    else:
        for candidate in [os.environ.get('ZOOM_APP', ''), '/Applications/zoom.us.app',
                          str(Path.home() / 'Applications/zoom.us.app'), '/Applications/Zoom Workplace.app']:
            if candidate and Path(candidate).is_dir():
                return candidate
    raise ValueError('Zoom Workplace was not found. Install Zoom first, or set ZOOM_APP to its application path.')


def zoom_command(app, domain, meeting_id, password, name, platform=None):
    query = urlencode(dict(action='join', confno=meeting_id, pwd=password, uname=name, zc='0'))
    url = f'zoommtg://{domain}/join?{query}'
    if (platform or sys.platform) == 'win32':
        # Community-reported Windows secondary-instance switch; version dependent.
        return [app, '/secondary', '--url=' + url]
    return ['/usr/bin/open', '-n', '-a', app, url]


HTML = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Zoom Language Launcher</title>
<style>
:root{--ink:#1a303b;--muted:#667b85;--line:#dce5e7;--green:#126d60;--wash:#f2f6f5}*{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select{font:inherit}button{cursor:pointer;border:0;border-radius:9px;padding:10px 15px;background:var(--green);color:white;font-weight:600;transition:background .15s}button:hover{filter:brightness(.95)}button.secondary{background:white;color:var(--ink);border:1px solid var(--line)}button.text{background:none;color:var(--muted);padding:8px}button:disabled{opacity:.45;cursor:default}button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #72bcb0;outline-offset:3px}input[type=text],input[type=password],input[type=number],select{width:100%;background:white;color:var(--ink);border:1px solid #cedadd;border-radius:8px;padding:11px 12px;min-height:42px}input::placeholder{color:#88989e}.field{display:block;font-size:12px;font-weight:650;color:#536a74;margin:15px 0 7px}h1,h2,h3,p{margin-top:0}h1{font-size:24px;letter-spacing:-.7px;margin-bottom:4px}h2{font-size:18px;letter-spacing:-.3px;margin-bottom:6px}p{line-height:1.55}.muted{color:var(--muted)}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;font-weight:700;color:var(--green);margin-bottom:8px}.shell{max-width:1280px;margin:auto;padding:28px 30px 150px}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:30px}.brand{display:flex;gap:14px;align-items:center}.logo{width:46px;height:46px;display:grid;place-items:center;background:var(--green);color:white;border-radius:13px;font-weight:750;letter-spacing:-1px;font-size:19px}.top p{margin:0;font-size:13px}.workspace{display:grid;grid-template-columns:320px minmax(0,1fr);gap:24px;align-items:start}.panel{background:white;border:1px solid var(--line);border-radius:16px;padding:23px}.session-panel{position:sticky;top:22px}.section-label{display:flex;align-items:center;gap:9px;margin-bottom:18px}.step{width:23px;height:23px;display:inline-grid;place-items:center;border-radius:7px;background:#e6f0ed;color:var(--green);font-size:12px;font-weight:700}.section-label h2{margin:0}.session-panel hr{border:0;border-top:1px solid var(--line);margin:22px 0}.session-panel small{display:block;font-size:12px;color:var(--muted);line-height:1.55;margin-top:7px}.password-wrap{display:flex;position:relative}.password-wrap input{padding-right:64px}.password-wrap button{position:absolute;right:4px;top:4px;background:transparent;color:var(--muted);font-size:12px;padding:10px}.language-heading{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:20px}.language-heading .section-label{margin:0}.language-heading p{margin:5px 0 0;font-size:13px}.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:16px}.toolbar input{flex:1;min-width:0}.toolbar button{font-size:12px;white-space:nowrap}.device-bar{border:1px solid var(--line);border-radius:12px;padding:15px 17px;margin-bottom:15px;background:#f7faf9}.device-title{display:flex;align-items:center;justify-content:space-between;gap:10px}.device-title strong{font-size:13px}.device-title button{font-size:12px;padding:5px}.device-bar p{font-size:12px;margin:7px 0;color:var(--muted)}.device-actions{display:flex;gap:9px}.device-actions select{min-width:0;font-size:13px;flex:1}.device-actions button{font-size:12px;white-space:nowrap}.routing-note{font-size:12px;color:#866329;margin:12px 0 18px;padding-left:11px;border-left:3px solid #d5b678;line-height:1.5}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.language{border:1px solid var(--line);border-radius:12px;background:#fff;padding:15px;transition:border-color .15s,background .15s}.language:has([name=language]:checked){border-color:#8cb9ad;background:#f7fbf9}.language .head{display:flex;align-items:center;gap:9px}.language input[type=checkbox]{width:18px;height:18px;accent-color:var(--green);cursor:pointer;flex:none}.language .head strong{font-size:14px;flex:1}.lang-code{display:grid;place-items:center;width:29px;height:29px;border-radius:8px;background:#e7eeed;font-size:10px;font-weight:750;letter-spacing:.3px}.language .remove{color:#809399;background:none;padding:3px 6px;font-size:19px;font-weight:400;line-height:1}.window-options{display:flex;flex-wrap:wrap;gap:8px 14px;margin-top:13px}.window-options label{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted)}.window-options input{accent-color:var(--green)}.window-retry{margin-top:9px;font-size:11px!important;padding:6px!important}.name-preview{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted);font-size:12px;margin:9px 0 12px}.channel-row{display:flex;align-items:center;justify-content:space-between;gap:10px}.channel-row label{font-size:12px;color:var(--muted)}.channel-row input{width:70px;min-height:34px;padding:6px 8px;font-weight:700;text-align:center}.language details{border-top:1px solid #e0e9e6;margin-top:13px;padding-top:10px}.language summary{font-size:12px;color:var(--green);font-weight:600}.language .field{font-size:11px;margin-top:12px}.language select,.language [data-name]{font-size:12px;padding:9px;min-height:36px}.language small{font-size:11px;line-height:1.5;display:block;margin-top:9px;color:var(--muted)}summary{cursor:pointer;list-style:none}summary::after{content:' +';float:right;color:#82968e}details[open]>summary::after{content:' −'}.below-grid{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:17px}.below-grid p{margin:0;font-size:12px}.advanced{margin-top:18px}.advanced>summary{font-size:12px;color:var(--muted);padding:8px 0}.advanced p{font-size:12px;color:var(--muted);margin:12px 0}.advanced button{font-size:12px;margin:6px 3px 0 0}.advanced input[type=file]{max-width:100%;font-size:11px}.checklist{font-size:12px;color:var(--muted);line-height:1.8;padding-left:17px}.footer-bar{position:fixed;bottom:0;left:0;right:0;background:#fffffff5;border-top:1px solid var(--line);backdrop-filter:blur(12px);z-index:10}.footer-inner{max-width:1280px;margin:auto;padding:15px 30px;display:flex;align-items:center;gap:20px}.footer-summary{flex:1;min-width:0}.footer-summary strong{font-size:14px}.footer-summary small{display:block;color:var(--muted);font-size:11px;margin-top:4px}.footer-buttons{display:flex;gap:10px;flex-shrink:0}#launch{padding:13px 24px}#status{white-space:pre-wrap;font-size:12px;color:var(--green);line-height:1.5;max-height:100px;overflow:auto;max-width:1220px;margin:0 auto;padding:0 30px}#status:not(:empty){padding-top:10px}footer{font-size:11px;color:var(--muted);margin-top:23px;line-height:1.6;overflow-wrap:anywhere}dialog{border:1px solid var(--line);border-radius:16px;width:440px;max-width:calc(100% - 32px);padding:26px;color:var(--ink);box-shadow:0 24px 90px #173b3b25}dialog::backdrop{background:#112e3860;backdrop-filter:blur(3px)}.dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:24px}.empty{display:none;padding:28px;text-align:center;font-size:13px;color:var(--muted)}@media(min-width:1200px){.shell{padding-top:35px}}@media(max-width:950px){.workspace{grid-template-columns:275px minmax(0,1fr);gap:17px}.grid{grid-template-columns:1fr}.panel{padding:19px}.shell{padding:22px 20px 160px}}@media(max-width:650px){.workspace{grid-template-columns:1fr}.session-panel{position:static}.shell{padding:18px 14px 175px}.top{margin-bottom:22px}.top h1{font-size:19px}.top p{font-size:11px}.logo{width:36px;height:36px;font-size:15px}.brand{gap:9px}.grid{grid-template-columns:1fr}.footer-inner{padding:12px 14px;gap:10px;flex-wrap:wrap}.footer-summary{min-width:100%}.footer-buttons{width:100%}.footer-buttons button{flex:1}#status{padding-left:14px;padding-right:14px}.language-heading{align-items:flex-start}.device-actions{flex-wrap:wrap}.toolbar{gap:6px}.toolbar button{padding:10px}.below-grid{align-items:flex-start;flex-direction:column}}
</style>
<div class="shell"><header class="top"><div class="brand"><div class="logo" aria-hidden="true">ZL</div><div><h1>Language Launcher</h1><p class="muted">Your meeting. Every language, ready.</p></div></div><button id="quit" class="text">Quit</button></header>
<main class="workspace"><aside class="panel session-panel"><div class="section-label"><span class="step">1</span><h2>Meeting details</h2></div>
<label class="field" for="recent">Saved sessions</label><select id="recent"><option value="">Start a new session</option></select>
<hr><label class="field" for="session_name">Session name</label><input id="session_name" type="text" placeholder="e.g. Sunday conference" maxlength="120" autocomplete="off">
<label class="field" for="meeting">Zoom link or meeting ID</label><input id="meeting" type="text" placeholder="Paste your Zoom link" autocomplete="off">
<label class="field" for="password">Passcode <span class="muted">(if needed)</span></label><div class="password-wrap"><input id="password" type="password" placeholder="Meeting passcode" autocomplete="off"><button id="show_password" aria-label="Show passcode" aria-pressed="false">Show</button></div><small>Leave blank if the passcode is in the link.</small>
<details class="advanced"><summary>Zoom joining setup</summary><p>Configure these once in Zoom → Settings → Meetings &amp; webinars:</p><ul class="checklist"><li>Camera off when joining</li><li>Microphone muted when joining</li><li>Video preview off</li><li>Connect to computer audio automatically</li></ul><p>Choose the interpretation language inside each Zoom window after joining.</p><p id="platform_note"></p></details>
<details class="advanced"><summary>Import / export sessions</summary><p>Transfer your saved sessions, including passcodes.</p><input type="file" id="import_file" accept=".json,application/json" aria-label="Import sessions file"><button id="import" class="secondary">Import</button><button id="export" class="secondary">Export</button></details>
<details class="advanced"><summary>Storage &amp; privacy</summary><p>Sessions and passcodes are stored unencrypted in <span id="data_path"></span>. Installers contain no personal session data.</p></details></aside>
<section class="panel"><div class="language-heading"><div><div class="section-label"><span class="step">2</span><h2>Choose languages</h2></div><p class="muted">Select the clients you want to open.</p></div><button id="open_add" class="secondary">+ Add language</button></div>
<div class="toolbar"><input id="search" type="text" aria-label="Search languages" placeholder="Search languages…"><button id="all" class="text">Select all</button><button id="none" class="text">Unselect all</button></div>
<div class="device-bar"><div class="device-title"><strong>Audio input</strong><button id="detect_audio" class="text">↻ Refresh devices</button></div><p id="audio_status" role="status">Looking for audio inputs…</p><div class="device-actions"><select id="bulk_device" aria-label="Audio input for selected languages"><option value="auto">Automatic · prefer Loopback Audio</option><option value="system">System default</option></select><button id="apply_device" class="secondary">Apply to selected</button></div></div>
<label class="routing-note" style="display:block"><input id="apply_audio" type="checkbox" checked> Apply microphone &amp; channel settings in Zoom after launching</label><p id="routing_note" class="routing-note">macOS requires Accessibility permission. Meetings open first. Audio setup errors are reported without stopping meeting launches. It does not unmute the microphone.</p><button id="test_audio" class="text">Test two channels without a meeting</button><button id="check_audio" class="text">Check running clients’ audio settings</button>
<details class="advanced"><summary>About automatic pinning &amp; mini windows</summary><p>Choose the options on each language card before launching. These experimental actions run after joining, and require Zoom’s Participants panel to expose your matching name marked “me”. macOS also requires Accessibility permission for the launcher. Pinning and mini-window actions run independently, even if audio setup fails. A pin failure does not cancel minimization. Video and microphone settings are never changed. Use “Apply window options” on a card to retry after joining. Floating mini view may not preserve your own tile on every Zoom version.</p></details><div class="device-bar"><div class="device-title"><strong>After joining</strong><div><button id="all_windows" class="text" aria-label="Select all window options">Select all</button><button id="no_windows" class="text" aria-label="Unselect all window options">Unselect all</button></div></div><div class="window-options"><label><input id="pin_all" type="checkbox" checked> Pin my profile for all</label><label><input id="mini_all" type="checkbox" checked> Mini window for all</label></div></div><div id="languages" class="grid"></div><p id="empty_search" class="empty">No languages match your search.</p>
<div class="below-grid"><p class="muted">Expand a language to edit its name or audio input.</p><button id="save_languages" class="text">Save language defaults</button></div>
<footer>Zoom Workplace required · Language Launcher 2.4.3 · Interpretation channels are selected in Zoom.</footer></section></main></div>
<div class="footer-bar"><div id="status" role="status" aria-live="polite"></div><div class="footer-inner"><div class="footer-summary"><strong id="count">Choose your languages</strong><small>Session settings are saved when you launch.</small></div><div class="footer-buttons"><button id="save" class="secondary">Save session</button><button id="launch">Launch selected →</button></div></div></div>
<dialog id="add_dialog" aria-labelledby="add_title"><div class="eyebrow">Expand your setup</div><h2 id="add_title">Add a language</h2><p class="muted">Give the new client a language and participant name.</p><label class="field" for="new_language">Language</label><input id="new_language" type="text" placeholder="e.g. Italian" maxlength="64"><label class="field" for="new_name">Participant name</label><input id="new_name" type="text" placeholder="e.g. *I T Italian" maxlength="100"><small class="muted">You can set its input and channel after adding it.</small><p id="add_error" role="status" style="color:#9a4b30;font-size:12px;margin-top:12px"></p><div class="dialog-actions"><button id="cancel_add" class="secondary">Cancel</button><button id="add">Add language</button></div></dialog>
<script>
const $=id=>document.getElementById(id);let catalogue=[],sessions=[],busy=false,audioDevices=[],preferredInput="system",currentPlatform="",audioScanned=false;
const defaultChannels={"Russian": 17, "Chinese": 18, "Dutch": 19, "French": 20, "Spanish": 21, "Polish": 22, "Portuguese": 23, "Romanian": 24, "German": 25};
function message(value){$('status').textContent=value;if($('add_dialog').open)$('add_error').textContent=value}
function filterLanguages(){let query=$('search').value.trim().toLowerCase(),visible=0;document.querySelectorAll('.language').forEach(row=>{row.hidden=!row.dataset.language.toLowerCase().includes(query);if(!row.hidden)visible++});$('empty_search').style.display=visible?'none':'block'}
function update(){let n=selected().length;$('count').textContent=n===1?'1 language selected':n+' languages selected';$('launch').textContent=busy?'Working…':'Launch '+(n===1?'1 client':n+' clients')+' →';$('launch').disabled=busy||n===0;['save','save_languages','add','import','export','all','none','detect_audio','apply_device','open_add','check_audio','test_audio','all_windows','no_windows'].forEach(id=>$(id).disabled=busy);document.querySelectorAll('#languages input,#languages select,#languages button,#session_name,#meeting,#password,#recent,#bulk_device,#apply_audio,#pin_all,#mini_all').forEach(e=>e.disabled=busy);for(let [id,selector] of [['pin_all','[data-pin]'],['mini_all','[data-mini]']]){let flags=[...document.querySelectorAll(selector)],count=flags.filter(e=>e.checked).length;$(id).checked=flags.length>0&&count===flags.length;$(id).indeterminate=count>0&&count<flags.length}filterLanguages()}
function getCatalogue(){return [...document.querySelectorAll('.language')].map(row=>({language:row.dataset.language,name:row.querySelector('[data-name]').value,audio_input:row.querySelector('[data-audio]').value,input_channel:Number(row.querySelector('[data-channel]').value),pin_self:row.querySelector('[data-pin]').checked,mini_window:row.querySelector('[data-mini]').checked}))}
function selected(){return [...document.querySelectorAll('[name=language]:checked')].map(e=>e.value)}
function render(rows,checked){$('languages').replaceChildren();const codes={Chinese:'ZH',Dutch:'NL',French:'FR',Spanish:'ES',Polish:'PL',Portuguese:'PT',Romanian:'RO',German:'DE',Russian:'RU'};for(let r of rows){let row=document.createElement('div');row.className='language';row.dataset.language=r.language;
let head=document.createElement('div');head.className='head';let cb=document.createElement('input');cb.type='checkbox';cb.name='language';cb.value=r.language;cb.checked=checked.includes(r.language);cb.setAttribute('aria-label','Launch '+r.language);cb.onchange=update;let code=document.createElement('span');code.className='lang-code';code.textContent=codes[r.language]||r.language.slice(0,2).toUpperCase();let label=document.createElement('strong');label.textContent=r.language;let remove=document.createElement('button');remove.className='remove';remove.textContent='×';remove.setAttribute('aria-label','Remove '+r.language);remove.onclick=async()=>{let old=getCatalogue(),sel=selected();render(old.filter(x=>x.language!==r.language),sel);try{await persistCatalogue();message('Language removed. Saved sessions are kept.')}catch(e){render(old,sel);message(e.message)}};head.append(cb,code,label,remove);
let namePreview=document.createElement('span');namePreview.className='name-preview';namePreview.textContent=r.name;let channelRow=document.createElement('div');channelRow.className='channel-row';let channelLabel=document.createElement('label');channelLabel.textContent='Input channel';let channel=document.createElement('input');channel.type='number';channel.min='1';channel.max='256';channel.step='1';channel.value=r.input_channel??defaultChannels[r.language]??1;channel.dataset.channel='';channel.setAttribute('aria-label','Input channel for '+r.language);channelLabel.htmlFor='channel-'+encodeURIComponent(r.language);channel.id=channelLabel.htmlFor;channelRow.append(channelLabel,channel);
let details=document.createElement('details');let summary=document.createElement('summary');summary.textContent='Name & audio settings';let nameLabel=document.createElement('label');nameLabel.className='field';nameLabel.textContent='Participant name';let name=document.createElement('input');name.type='text';name.maxLength=100;name.value=r.name;name.dataset.name='';name.setAttribute('aria-label','Participant name for '+r.language);name.oninput=()=>namePreview.textContent=name.value;nameLabel.append(name);let audioLabel=document.createElement('label');audioLabel.textContent='Audio input';audioLabel.className='field';let device=document.createElement('select');device.dataset.audio='';device.setAttribute('aria-label','Audio input for '+r.language);audioLabel.append(device);let detail=document.createElement('small');detail.dataset.audioDetail='';details.append(summary,nameLabel,audioLabel,detail);let options=document.createElement('div');options.className='window-options';for(let [key,title,marker] of [['pin_self','Pin my profile','pin'],['mini_window','Mini window','mini']]){let label=document.createElement('label'),box=document.createElement('input');box.type='checkbox';box.checked=r[key]!==false;box.dataset[marker]='';box.onchange=update;box.setAttribute('aria-label',title+' for '+r.language);label.append(box,document.createTextNode(title));options.append(label)}let retry=document.createElement('button');retry.className='text window-retry';retry.textContent='Apply window options';retry.setAttribute('aria-label','Apply window options for '+r.language);retry.onclick=async()=>{busy=true;update();let timer=setInterval(async()=>{try{let d=await api('/progress');if(d.message)message(d.message)}catch(_){}},1000);try{let current=getCatalogue().find(x=>x.language===r.language);message('Looking for '+current.name+' in Zoom…');let result=await api('/window-actions',{name:current.name,...current});message(result.message)}catch(e){message(e.message)}finally{clearInterval(timer);busy=false;update()}};let audioRetry=document.createElement('button');audioRetry.className='text window-retry';audioRetry.textContent='Apply audio settings';audioRetry.setAttribute('aria-label','Apply audio settings for '+r.language);audioRetry.onclick=async()=>{busy=true;update();message('Applying audio settings for '+r.language+'…');try{let current=getCatalogue().find(x=>x.language===r.language);let result=await api('/audio-route',current);message(result.message)}catch(e){message(e.message)}finally{busy=false;update()}};details.append(audioRetry,retry);row.append(head,namePreview,channelRow,options,details);$('languages').append(row);fillDevice(row,r.audio_input||'auto');device.onchange=()=>describeAudio(row);channel.oninput=()=>describeAudio(row)}update()}
function fillDevice(row,value){let select=row.querySelector('[data-audio]');select.replaceChildren(new Option('Auto · '+(audioScanned?preferredInput:'prefer Loopback Audio'),'auto'),new Option('System default','system'));for(let d of audioDevices)select.add(new Option(d.label,d.label));if(!['auto','system',...audioDevices.map(d=>d.label)].includes(value))select.add(new Option(value+' (not detected)',value));select.value=value;describeAudio(row)}
function describeAudio(row){let device=row.querySelector('[data-audio]').value,channel=Number(row.querySelector('[data-channel]').value);let resolved=device==='auto'?preferredInput:device==='system'?(audioDevices.find(d=>d.default)?.label||'system'):device;let found=audioDevices.find(d=>d.label===resolved);row.querySelector('[data-audio-detail]').textContent=found?.channels?(channel>found.channels?'Channel unavailable: ':'')+found.channels+' input channels detected.':(audioScanned&&!found&&device!=='system'?'Selected device not detected. ':'')+'Channel count not verified.'}
async function detectAudio(allowPermission=false){let old=getCatalogue(),sel=selected();try{let result=await api('/audio-inputs');if(result.source==='browser'){if(!navigator.mediaDevices?.enumerateDevices)throw Error('Device detection is unavailable in this browser.');if(allowPermission){let stream=await navigator.mediaDevices.getUserMedia({audio:true,video:false});stream.getTracks().forEach(track=>track.stop())}let devices=await navigator.mediaDevices.enumerateDevices();result.devices=devices.filter(d=>d.kind==='audioinput'&&d.label&&d.deviceId!=='communications').map(d=>({label:d.label,channels:null,default:d.deviceId==='default'}));result.preferred=result.devices.find(d=>d.label.toLowerCase()==='loopback audio')?.label||result.devices.find(d=>d.default)?.label||'system'}audioDevices=result.devices.filter((d,i,all)=>all.findIndex(x=>x.label===d.label)===i);preferredInput=result.preferred;audioScanned=true;let bulk=$('bulk_device'),choice=bulk.value;bulk.replaceChildren(new Option('Auto · '+preferredInput,'auto'),new Option('System default','system'));audioDevices.forEach(d=>bulk.add(new Option(d.label,d.label)));if([...bulk.options].some(o=>o.value===choice))bulk.value=choice;if(old.length)render(old,sel);$('audio_status').textContent=audioDevices.length+' inputs available · Default: '+preferredInput+(result.source==='browser'?' Channel counts are not exposed by this browser. Refresh may ask for microphone permission; any capture is immediately stopped.':'')}catch(e){$('audio_status').textContent='Could not detect inputs: '+e.message+'. Existing saved choices are retained.'}}
$('detect_audio').onclick=()=>detectAudio(true);
$('test_audio').onclick=async()=>{busy=true;update();message('Testing two separate Zoom clients without joining a meeting…');let timer=setInterval(async()=>{try{let d=await api('/progress');if(d.message)message(d.message)}catch(_){}},1000);try{let result=await api('/audio-test',{});message(result.message)}catch(e){message(e.message)}finally{clearInterval(timer);busy=false;update()}};
$('check_audio').onclick=async()=>{busy=true;update();message('Checking Zoom channel selections…');try{let result=await api('/audio-routing-check',{});message(result.message)}catch(e){message(e.message)}finally{busy=false;update()}};
$('search').oninput=filterLanguages;
for(let [id,selector] of [['pin_all','[data-pin]'],['mini_all','[data-mini]']])$(id).onchange=()=>{document.querySelectorAll(selector).forEach(e=>e.checked=$(id).checked);update()};
function allWindowOptions(value){document.querySelectorAll('[data-pin],[data-mini]').forEach(e=>e.checked=value);update()}
$('all_windows').onclick=()=>allWindowOptions(true);$('no_windows').onclick=()=>allWindowOptions(false);
$('open_add').onclick=()=>{$('add_error').textContent='';$('add_dialog').showModal();$('new_language').focus()};
$('cancel_add').onclick=()=>$('add_dialog').close();
$('show_password').onclick=()=>{let show=$('password').type==='password';$('password').type=show?'text':'password';$('show_password').textContent=show?'Hide':'Show';$('show_password').setAttribute('aria-label',show?'Hide passcode':'Show passcode');$('show_password').setAttribute('aria-pressed',String(show))};
$('apply_device').onclick=()=>{let chosen=selected();if(!chosen.length){message('Select at least one language first.');return}document.querySelectorAll('.language').forEach(row=>{if(chosen.includes(row.dataset.language)){row.querySelector('[data-audio]').value=$('bulk_device').value;describeAudio(row)}});message('Audio input updated for '+chosen.length+' selected languages. Save defaults or save your session to keep it.')};
async function api(path,data){let response=await fetch(path,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Launcher-Token':'TOKEN'},body:data===undefined?undefined:JSON.stringify(data)});let result=await response.json();if(!response.ok)throw Error(result.message);return result}
async function refresh(){let state=await api('/state');sessions=state.sessions;catalogue=state.languages;currentPlatform=state.platform;if(currentPlatform!=='darwin'){$('apply_audio').checked=false;$('apply_audio').disabled=true;$('routing_note').textContent='Automatic audio routing is currently macOS-only. On Windows, selections are saved but must be applied manually in Zoom.'}$('recent').replaceChildren(new Option('Start a new session',''));sessions.forEach((s,i)=>$('recent').add(new Option(s.session_name+' — '+new Date(s.saved_at).toLocaleString(),String(i))));$('data_path').textContent=state.data_dir;$('platform_note').textContent=state.platform==='win32'?'Windows: experimental secondary-instance mode is enabled. Zoom may still reuse the existing client. Launch requests do not confirm separate participants; test with two languages first.':'Zoom instances share account/settings. Zoom may reuse or close instances; independent connections depend on Zoom.';return state}
async function persistCatalogue(){await api('/languages',{languages:getCatalogue()})}
function formData(){let rows=getCatalogue();return {apply_audio:$('apply_audio').checked,session_name:$('session_name').value,meeting:$('meeting').value,password:$('password').value,languages:selected(),names:Object.fromEntries(rows.map(r=>[r.language,r.name])),catalogue:rows,window_actions:Object.fromEntries(rows.map(r=>[r.language,{pin_self:r.pin_self,mini_window:r.mini_window}])),audio:Object.fromEntries(rows.map(r=>[r.language,{audio_input:r.audio_input,input_channel:r.input_channel}]))}}
$('all').onclick=()=>{document.querySelectorAll('[name=language]').forEach(b=>b.checked=true);update()};$('none').onclick=()=>{document.querySelectorAll('[name=language]').forEach(b=>b.checked=false);update()};
$('recent').onchange=e=>{if(e.target.value===''){['session_name','meeting','password'].forEach(k=>$(k).value='');message('New session. Enter your meeting details.');return}let s=sessions[Number(e.target.value)];$('apply_audio').checked=currentPlatform==='darwin'&&(s.apply_audio??true);['session_name','meeting','password'].forEach(k=>$(k).value=s[k]);let rows=getCatalogue();for(let language of s.languages){let r=rows.find(r=>r.language===language);if(r){Object.assign(r,s.window_actions?.[language]||{pin_self:true,mini_window:true});r.name=s.names[language];Object.assign(r,s.audio?.[language]||{audio_input:'auto',input_channel:defaultChannels[language]??1})}else rows.push({language,name:s.names[language],...(s.audio?.[language]||{}),...(s.window_actions?.[language]||{})})}render(rows,s.languages);message('Loaded '+s.session_name)};
$('add').onclick=async()=>{let language=$('new_language').value.trim(),name=$('new_name').value.trim()||language;let old=getCatalogue(),sel=selected();if(!language){message('Enter a language name.');return}render([...old,{language,name}],[...sel,language]);try{await persistCatalogue();$('new_language').value='';$('new_name').value='';$('add_dialog').close();message('Language added and saved.')}catch(e){render(old,sel);message(e.message)}};
$('save_languages').onclick=async()=>{try{await persistCatalogue();message('Language defaults saved, including names and audio choices.')}catch(e){message(e.message)}};
async function submit(path){if(!$('session_name').value.trim()){message('Give this session a name first.');$('session_name').focus();return}if(!$('meeting').value.trim()){message('Paste a Zoom link or meeting ID first.');$('meeting').focus();return}busy=true;update();let timer=path==='/launch'?setInterval(async()=>{try{let d=await api('/progress');if(d.message)message(d.message)}catch(_){}},1000):null;message(path==='/launch'?'Saving session and requesting launches…':'Saving session…');try{let d=await api(path,formData());message(d.message);await refresh()}catch(e){message(e.message)}finally{if(timer)clearInterval(timer);busy=false;update()}}
$('save').onclick=()=>submit('/save');$('launch').onclick=()=>submit('/launch');
$('import').onclick=async()=>{let file=$('import_file').files[0];if(!file){message('Choose a sessions JSON file first.');return}try{let result=await api('/import',{sessions:JSON.parse(await file.text())});await refresh();message(result.message)}catch(e){message(e.message)}};
$('export').onclick=async()=>{try{await refresh();let blob=new Blob([JSON.stringify(sessions,null,2)],{type:'application/json'});let url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='zoom_sessions.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(e){message(e.message)}};
$('quit').onclick=async()=>{try{await api('/quit',{});document.querySelectorAll('button').forEach(b=>b.disabled=true);message('Launcher stopped. You can close this tab. Zoom meetings remain open.')}catch(e){message(e.message)}};
refresh().then(async state=>{render(state.languages,state.languages.map(r=>r.language));await detectAudio(false)}).catch(e=>message(e.message));
</script></html>'''.replace('TOKEN', TOKEN)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, data, content_type='application/json'):
        body = data.encode('utf-8') if isinstance(data, str) else json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/?token=' + TOKEN:
            return self.respond(200, HTML, 'text/html')
        if self.path == '/progress' and self.headers.get('X-Launcher-Token') == TOKEN:
            return self.respond(200, PROGRESS)
        if self.path == '/audio-inputs' and self.headers.get('X-Launcher-Token') == TOKEN:
            try:
                return self.respond(200, audio_inputs())
            except (OSError, ValueError, subprocess.SubprocessError):
                return self.respond(500, dict(message='Could not read the audio input inventory.'))
        if self.path == '/state' and self.headers.get('X-Launcher-Token') == TOKEN:
            try:
                return self.respond(200, dict(sessions=read_sessions(), languages=read_languages(),
                                             data_dir=str(DATA_DIR), platform=sys.platform))
            except (OSError, ValueError, TypeError, AttributeError):
                return self.respond(500, dict(message='Could not read saved settings. Check the JSON files in the data folder.'))
        self.respond(404, dict(message='Open the launcher from its app shortcut.'))

    def do_POST(self):
        if self.path not in ('/launch', '/save', '/languages', '/import', '/quit', '/window-actions', '/audio-route', '/audio-routing-check', '/audio-test') or self.headers.get('X-Launcher-Token') != TOKEN:
            return self.respond(403, dict(message='Invalid launcher session.'))
        if not LOCK.acquire(blocking=False):
            return self.respond(409, dict(message='Another operation is in progress.'))
        launched = []
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 2_000_000:
                raise ValueError('Request is empty or too large (maximum 2 MB).')
            data = json.loads(self.rfile.read(size))
            if self.path == '/audio-test':
                if not accessibility_ready():
                    raise ValueError('No test clients opened. Accessibility permission is required for Language Launcher. Enable it in System Settings → Privacy & Security → Accessibility, then quit and reopen this app.')
                app=find_zoom()
                device=resolve_audio_device('auto',18)
                created=[]
                results=[]
                try:
                    for channel in (17,18):
                        PROGRESS['message']='Offline test: opening a new Zoom client for channel '+str(channel)+'…'
                        before=zoom_pids()
                        subprocess.run(['/usr/bin/open','-n','-a',app],capture_output=True,timeout=20,check=True)
                        pid=new_client_pid(before)
                        created.append((pid,channel))
                        outcome=configure_audio(pid,device,channel)
                        results.append(outcome['message'])
                        if not outcome['ok']:
                            raise ValueError('Offline routing test failed: '+outcome['message'])
                    for pid,channel in created:
                        outcome=configure_audio(pid,device,channel,inspect_only=True)
                        results.append(outcome['message'])
                        if not outcome['ok']:
                            raise ValueError('Independent client settings did not survive the test: '+outcome['message'])
                    return self.respond(200,dict(message='Independent Zoom settings verified for channels 17 and 18. No meeting joined and no microphone unmuted. This was a settings test, not a transmitted-audio test.\n'+'\n'.join(results)))
                finally:
                    for pid,channel in reversed(created):
                        close_idle_test_client(pid)
            if self.path == '/audio-routing-check':
                if not AUDIO_CLIENTS:
                    raise ValueError('No clients are tracked yet. Launch clients with this updated launcher first.')
                checks=[]
                for language, client in AUDIO_CLIENTS.items():
                    PROGRESS['message']='Verifying '+language+'…'
                    outcome=configure_audio(client['pid'],client['device'],client['channel'],inspect_only=True)
                    checks.append(language+': '+outcome['message'])
                return self.respond(200,dict(message='Zoom settings read-back (not an end-to-end audio test):\n'+'\n'.join(checks)))
            if self.path == '/audio-route':
                language=data.get('language')
                client=AUDIO_CLIENTS.get(language)
                if not client:
                    raise ValueError('This language has no tracked client. Launch it with this updated launcher first.')
                settings=audio_settings(data,language)
                device=resolve_audio_device(settings['audio_input'])
                outcome=configure_audio(client['pid'],device,settings['input_channel'])
                if outcome['ok']:
                    client.update(device=device,channel=settings['input_channel'])
                return self.respond(200,outcome)
            if self.path == '/window-actions':
                name = data.get('name', '')
                if not isinstance(name, str) or not name.strip() or len(name) > 100:
                    raise ValueError('Enter a participant name first.')
                settings = window_settings(data)
                PROGRESS['message'] = 'Looking for your own profile in Zoom…'
                outcome = apply_window_actions(name.strip(), settings, pid=WINDOW_CLIENTS.get(data.get('language')))
                return self.respond(200, outcome)
            if self.path == '/quit':
                self.respond(200, dict(message='Launcher stopped.'))
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == '/languages':
                atomic_json(LANGUAGES_FILE, validate_catalogue(data['languages']))
                return self.respond(200, dict(message='Language list saved.'))
            if self.path == '/import':
                rows = data['sessions']
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ValueError('Import a list of up to 1000 sessions.')
                # Validate the entire import before modifying anything.
                records = [validate_session(row) for row in rows]
                existing = read_sessions()
                imported = {r['session_name'].casefold() for r in records}
                if len(imported) != len(records):
                    raise ValueError('Imported session names must be unique.')
                atomic_json(SESSIONS_FILE, records + [r for r in existing if r['session_name'].casefold() not in imported])
                return self.respond(200, dict(message=f'Imported {len(records)} sessions. Matching names were updated.'))
            record = validate_session(data)
            catalogue = validate_catalogue(data.get('catalogue', read_languages()))
            audio_ready = False
            audio_warnings = []
            if self.path == '/launch':
                app = find_zoom()
                if record['apply_audio']:
                    try:
                        if sys.platform!='darwin':
                            raise RoutingError('Automatic audio setup is currently macOS-only.')
                        audio_ready=accessibility_ready()
                        if not audio_ready:
                            raise RoutingError('Accessibility permission is missing. Meetings will still open; audio channels were NOT applied. Enable Language Launcher in System Settings → Privacy & Security → Accessibility, then restart the launcher.')
                    except (OSError,RoutingError) as error:
                        audio_ready=False
                        audio_warnings.append(str(error))
            save_session(record)
            atomic_json(LANGUAGES_FILE, catalogue)
            if self.path == '/save':
                return self.respond(200, dict(message='Session, audio choices and window options saved. Audio selections will be applied on launch when enabled.'))
            domain, meeting_id, password = meeting_details(record['meeting'], record['password'])
            window_results = []
            audio_results = []
            pending_audio = {}
            audio_failed = set()
            # Meeting launch is independent of permission and channel setup.
            for language in record['languages']:
                PROGRESS['message'] = 'Opening meeting for ' + language + '…'
                before_pids=None
                WINDOW_CLIENTS.pop(language,None)
                track_window=sys.platform=='darwin' and any(record['window_actions'][language].values())
                if audio_ready or track_window:
                    try:
                        before_pids=zoom_pids()
                    except (OSError,RoutingError,subprocess.SubprocessError) as error:
                        audio_warnings.append(language+': could not identify the new Zoom process. '+str(error))
                command = zoom_command(app, domain, meeting_id, password, record['names'][language])
                if sys.platform == 'win32':
                    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    result = subprocess.run(command, capture_output=True, timeout=20)
                    if result.returncode:
                        raise ValueError('macOS could not launch Zoom. Check the installation.')
                launched.append(language)
                if before_pids is not None:
                    try:
                        pending_audio[language]=new_client_pid(before_pids)
                        WINDOW_CLIENTS[language]=pending_audio[language]
                    except (OSError,RoutingError,subprocess.SubprocessError) as error:
                        audio_warnings.append(language+': '+str(error))
                else:
                    time.sleep(2)
            # All join requests have now been delivered. Audio failures are reported
            # per client and never cancel the remaining meeting launches.
            if record['apply_audio']:
                for language in launched:
                    if language not in pending_audio:
                        audio_failed.add(language)
                        continue
                    settings=record['audio'][language]
                    try:
                        device=resolve_audio_device(settings['audio_input'],settings['input_channel'])
                        PROGRESS['message']=language+': applying microphone channel '+str(settings['input_channel'])+'…'
                        outcome=configure_audio(pending_audio[language],device,settings['input_channel'])
                        if not outcome['ok']:
                            raise RoutingError(outcome['message'])
                        AUDIO_CLIENTS[language]=dict(pid=pending_audio[language],device=device,channel=settings['input_channel'])
                    except (OSError,ValueError,RoutingError,subprocess.SubprocessError) as error:
                        audio_failed.add(language)
                        audio_warnings.append(language+': '+str(error))
                for language in launched:
                    if language in audio_failed:
                        continue
                    client=AUDIO_CLIENTS[language]
                    PROGRESS['message']='Checking independent audio settings for '+language+'…'
                    try:
                        outcome=configure_audio(client['pid'],client['device'],client['channel'],inspect_only=True)
                        if not outcome['ok']:
                            raise RoutingError(outcome['message'])
                        audio_results.append(language+': '+outcome['message'])
                    except (OSError,RoutingError,subprocess.SubprocessError) as error:
                        audio_failed.add(language)
                        audio_warnings.append(language+': audio selection could not be verified. '+str(error))
            for language in launched:
                options=record['window_actions'][language]
                if options['pin_self'] or options['mini_window']:
                    PROGRESS['message']=language+': applying window options…'
                    deadline=time.monotonic()+45
                    while True:
                        outcome=apply_window_actions(record['names'][language],options,pid=WINDOW_CLIENTS.get(language))
                        if outcome['ok'] or not outcome.get('retry',outcome['message'].startswith('Could not uniquely identify')) or time.monotonic()>=deadline:
                            break
                        time.sleep(3)
                    window_results.append(language+': '+outcome['message'])
            self.respond(200, dict(message='Launch requests sent (meeting connections not verified):\n' +
                '\n'.join(f'• {record["names"][x]} — choose Interpretation → {x}' for x in launched) + ('\n\nVerified Zoom input settings:\n'+'\n'.join(audio_results) if audio_results else ('\nAudio settings were NOT applied or could not be verified.' if record['apply_audio'] else '\nAudio setup disabled.')) + ('\n\nAudio setup needs attention (meetings remain open):\n'+'\n'.join(audio_warnings) if audio_warnings else '') + ('\n\nWindow actions:\n' + '\n'.join(window_results) if window_results else '')))
        except (OSError, ValueError, RoutingError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
            message = str(error) if isinstance(error, (ValueError, RoutingError)) else 'Could not save or launch. Check your settings, Zoom installation and data folder permissions.'
            self.respond(400, dict(message=message + ('\nRequests already sent: ' + ', '.join(launched) if launched else '')))
        finally:
            PROGRESS['message'] = ''
            LOCK.release()


def migrate_window_defaults():
    """Apply the user's new default once; retain later saved custom choices."""
    marker=DATA_DIR / 'window_defaults_v2_4_2.json'
    if marker.exists():
        return
    rows=read_languages()
    for row in rows:
        row.update(pin_self=True,mini_window=True)
    sessions=read_sessions()
    for session in sessions:
        session['window_actions']={language:dict(pin_self=True,mini_window=True)
                                   for language in session.get('languages',[])}
    atomic_json(LANGUAGES_FILE,rows)
    atomic_json(SESSIONS_FILE,sessions)
    atomic_json(marker,dict(pin_self=True,mini_window=True))


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    migrate_window_defaults()
    if not LANGUAGES_FILE.exists():
        atomic_json(LANGUAGES_FILE, read_languages())
    if not SESSIONS_FILE.exists():
        atomic_json(SESSIONS_FILE, [])
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    address = f'http://127.0.0.1:{server.server_port}/?token={TOKEN}'
    if sys.stdout:
        print('Zoom Language Launcher: ' + address, flush=True)
        print('Data folder: ' + str(DATA_DIR), flush=True)
    webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
