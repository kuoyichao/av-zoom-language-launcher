"""Best-effort post-join Zoom window actions, using native accessibility APIs.

Only acts on a unique meeting window with an explicit matching '(me)' identity.
Never turns on video/audio, spotlights anyone, or uses unscoped keystrokes.
"""
import json
import os
import subprocess
import sys

MAC_SCRIPT = r'''
ObjC.import('Foundation');
function run(){
 const request=JSON.parse(ObjC.unwrap($.NSProcessInfo.processInfo.environment.objectForKey('ZL_WINDOW_REQUEST')));
 const se=Application('System Events');
 function attr(e,k){try{return e.attributes.byName(k).value()}catch(_){return ''}}
 function text(e){return [attr(e,'AXTitle'),attr(e,'AXDescription'),attr(e,'AXValue')].filter(x=>typeof x==='string').join(' ')}
 function contents(e){try{return e.entireContents()}catch(_){return []}}
 function own(e){let t=text(e);return t.indexOf(request.name)>=0 && /\([^)]*\bme\b[^)]*\)/i.test(t)}
 function press(e){try{e.actions.byName('AXPress').perform();return true}catch(_){return false}}
 function finish(ok,message){return JSON.stringify({ok:ok,message:message})}
 let matches=[];
 for(let p of se.applicationProcesses()){
  try{if(p.bundleIdentifier()!=='us.zoom.xos')continue;
   for(let w of p.windows()){
    if(!/meeting|webinar/i.test(text(w)))continue;
    let selves=contents(w).filter(own);
    if(selves.length)matches.push({p:p,w:w,self:selves[0]});
   }
  }catch(_){}
 }
 if(matches.length!==1)return finish(false,'Could not uniquely identify the joined client by its own name (me). Leave its Participants panel open and retry.');
 let target=matches[0], w=target.w, p=target.p;
 if(request.pin_self){
  let node=target.self, button=null;
  for(let level=0;level<4 && node;level++){
   let choices=contents(node).filter(e=>/more/i.test(text(e)) && text(e).indexOf(request.name)>=0);
   if(choices.length===1){button=choices[0];break}
   node=attr(node,'AXParent');
  }
  if(!button || !press(button))return finish(false,'Self profile found, but its own More menu is unavailable. Pin manually; window left open.');
  delay(0.4);
  let menus=contents(p).filter(e=>attr(e,'AXRole')==='AXMenuItem');
  let already=menus.filter(e=>/^remove pin$|^unpin$/i.test(String(attr(e,'AXTitle'))));
  let pins=menus.filter(e=>/^pin$|^pin for myself$/i.test(String(attr(e,'AXTitle'))));
  if(!already.length){
   if(pins.length!==1 || !press(pins[0]))return finish(false,'Zoom does not expose Pin for this self profile. Window left open; camera remains off.');
   delay(0.4);
   if(!press(button))return finish(false,'Pin requested, but the result could not be verified. Window left open.');
   delay(0.3);
   already=contents(p).filter(e=>attr(e,'AXRole')==='AXMenuItem' && /^remove pin$|^unpin$/i.test(String(attr(e,'AXTitle'))));
   if(!already.length)return finish(false,'Pin requested, but no Remove pin state was found. Window left open.');
  }
  // Dismiss only this profile menu, with no keyboard shortcut.
  try{let menu=attr(already[0],'AXParent');menu.actions.byName('AXCancel').perform()}catch(_){return finish(false,'Profile is pinned; close its menu manually before minimizing.')}
 }
 if(request.mini_window){
  try{w.attributes.byName('AXMinimized').value=true}catch(_){return finish(false,'Could not minimize the matched meeting window. macOS Accessibility permission may be required.')}
  delay(1);
  let mini=contents(p).some(e=>/show full meeting window|exit minimized video|expand meeting/i.test(text(e)));
  if(!mini)return finish(false,'Minimize requested, but Zoom floating mini window could not be confirmed. Check the Zoom window.');
 }
 return finish(true,request.pin_self?(request.mini_window?'Own profile pinned; floating mini window verified.':'Own profile pinned.'):'Floating mini window verified.');
}
'''

WINDOWS_SCRIPT = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$request=$env:ZL_WINDOW_REQUEST | ConvertFrom-Json
function Finish([bool]$ok,[string]$message){@{ok=$ok;message=$message}|ConvertTo-Json -Compress;exit 0}
function Children($element){@($element.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition))}
function IsSelf($element){$t=$element.Current.Name;return ($t.Contains([string]$request.name) -and $t -match '\([^)]*\bme\b[^)]*\)')}
function Invoke($element){$pattern=$null;if($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)){$pattern.Invoke();return $true};return $false}
try{
 $processes=@(Get-Process -Name Zoom -ErrorAction SilentlyContinue)
 $matches=@()
 $desktop=[System.Windows.Automation.AutomationElement]::RootElement
 foreach($process in $processes){
  $condition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty,$process.Id)
  $windows=$desktop.FindAll([System.Windows.Automation.TreeScope]::Children,$condition)
  foreach($window in $windows){
   if($window.Current.Name -notmatch 'meeting|webinar'){continue}
   $selves=@(Children $window | Where-Object {IsSelf $_})
   if($selves.Count -gt 0){$matches+=@{window=$window;self=$selves[0];process=$process}}
  }
 }
 if($matches.Count -ne 1){Finish $false 'Could not uniquely identify the joined client by its own name (me). Leave its Participants panel open and retry.'}
 $target=$matches[0];$window=$target.window
 if($request.pin_self){
  $node=$target.self;$button=$null
  for($i=0;$i -lt 4 -and $null -ne $node;$i++){
   $buttons=@(Children $node | Where-Object {$_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and $_.Current.Name -match 'More' -and $_.Current.Name.Contains([string]$request.name)})
   if($buttons.Count -eq 1){$button=$buttons[0];break}
   $node=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($node)
  }
  if($null -eq $button -or !(Invoke $button)){Finish $false 'Self profile found, but its own More menu is unavailable. Pin manually; window left open.'}
  Start-Sleep -Milliseconds 400
  $menus=@(Children $desktop | Where-Object {$_.Current.ProcessId -eq $target.process.Id -and $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::MenuItem})
  $already=@($menus|Where-Object {$_.Current.Name -match '^(Remove pin|Unpin)$'})
  if($already.Count -eq 0){
   $pins=@($menus|Where-Object {$_.Current.Name -match '^(Pin|Pin for myself)$'})
   if($pins.Count -ne 1 -or !(Invoke $pins[0])){Finish $false 'Zoom does not expose Pin for this self profile. Window left open; camera remains off.'}
   Start-Sleep -Milliseconds 400
   if(!(Invoke $button)){Finish $false 'Pin requested but the result could not be verified. Window left open.'}
   Start-Sleep -Milliseconds 300
   $already=@(Children $desktop|Where-Object {$_.Current.ProcessId -eq $target.process.Id -and $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::MenuItem -and $_.Current.Name -match '^(Remove pin|Unpin)$'})
   if($already.Count -eq 0){Finish $false 'Pin requested but no Remove pin state was found. Window left open.'}
  }
  # Only dismiss the menu by reopening its own trigger. Do not send global keys.
  if(!(Invoke $button)){Finish $false 'Profile pinned. Close its menu manually before minimizing.'}
 }
 if($request.mini_window){
  $pattern=$null
  if(!$window.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern,[ref]$pattern)){Finish $false 'Meeting window does not expose a minimize control.'}
  $pattern.SetWindowVisualState([System.Windows.Automation.WindowVisualState]::Minimized)
  Start-Sleep -Milliseconds 1000
  $mini=@(Children $desktop|Where-Object {$_.Current.ProcessId -eq $target.process.Id -and $_.Current.Name -match 'Show full meeting window|Exit minimized video|Expand meeting'})
  if($mini.Count -eq 0){Finish $false 'Minimize requested but floating mini window could not be confirmed. Check Zoom.'}
 }
 Finish $true 'Requested profile/window actions verified.'
}catch{Finish $false 'Zoom accessibility controls are unavailable. Complete the pin and mini-window actions manually.'}
'''


def apply_window_actions(name, settings, platform=None, pid=None):
    if not settings.get('pin_self') and not settings.get('mini_window'):
        return dict(ok=True, message='No window actions requested.')
    platform = platform or sys.platform
    if platform == 'darwin':
        from zoom_mac_windows import apply_mac_window_actions
        return apply_mac_window_actions(name, settings, pid=pid)
    if platform not in ('darwin', 'win32'):
        return dict(ok=False, message='Window actions require macOS or Windows.')
    env = dict(os.environ, ZL_WINDOW_REQUEST=json.dumps(dict(name=name, **settings)))
    if platform == 'win32':
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', WINDOWS_SCRIPT]
    else:
        command = ['/usr/bin/osascript', '-l', 'JavaScript', '-e', MAC_SCRIPT]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=25, env=env)
        if result.returncode:
            return dict(ok=False, message='Window actions unavailable. On macOS, grant Accessibility access to the launcher in System Settings.')
        data = json.loads(result.stdout.strip())
        if not isinstance(data.get('ok'), bool) or not isinstance(data.get('message'), str):
            raise ValueError('Invalid helper response')
        return data
    except (OSError, ValueError, subprocess.SubprocessError):
        return dict(ok=False, message='Window actions could not be verified. Complete them manually in Zoom.')
