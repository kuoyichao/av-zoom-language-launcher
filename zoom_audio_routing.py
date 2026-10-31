"""macOS-only, process-scoped Zoom microphone/channel selection through AX.

No audio capture, signal generation, unmuting, or system-default changes.
"""
import ctypes as C
import os
import re
import subprocess
import sys
import time


class RoutingError(Exception):
    pass


class AX:
    def __init__(self):
        self.cf=C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.ax=C.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        def bind(lib,name,args,result):
            fn=getattr(lib,name);fn.argtypes=args;fn.restype=result;return fn
        P=C.c_void_p;L=C.c_long;I=C.c_int
        self.release=bind(self.cf,'CFRelease',[P],None)
        self.retain=bind(self.cf,'CFRetain',[P],P)
        self.typeid=bind(self.cf,'CFGetTypeID',[P],C.c_ulong)
        self.stringid=bind(self.cf,'CFStringGetTypeID',[],C.c_ulong)()
        self.arrayid=bind(self.cf,'CFArrayGetTypeID',[],C.c_ulong)()
        self.numberid=bind(self.cf,'CFNumberGetTypeID',[],C.c_ulong)()
        self.boolid=bind(self.cf,'CFBooleanGetTypeID',[],C.c_ulong)()
        self.string=bind(self.cf,'CFStringCreateWithCString',[P,C.c_char_p,C.c_uint32],P)
        self.getstr=bind(self.cf,'CFStringGetCString',[P,C.c_char_p,L,C.c_uint32],C.c_bool)
        self.strlen=bind(self.cf,'CFStringGetLength',[P],L)
        self.count=bind(self.cf,'CFArrayGetCount',[P],L)
        self.item=bind(self.cf,'CFArrayGetValueAtIndex',[P,L],P)
        self.number=bind(self.cf,'CFNumberGetValue',[P,I,P],C.c_bool)
        self.boolean=bind(self.cf,'CFBooleanGetValue',[P],C.c_bool)
        self.copy=bind(self.ax,'AXUIElementCopyAttributeValue',[P,P,C.POINTER(P)],I)
        self.setattr=bind(self.ax,'AXUIElementSetAttributeValue',[P,P,P],I)
        self.perform=bind(self.ax,'AXUIElementPerformAction',[P,P],I)
        self.create=bind(self.ax,'AXUIElementCreateApplication',[I],P)
        self.trusted=bind(self.ax,'AXIsProcessTrusted',[],C.c_bool)
        self.true=P.in_dll(self.cf,'kCFBooleanTrue').value
        self.false=P.in_dll(self.cf,'kCFBooleanFalse').value
        self.owned=[]

    def close(self):
        for p in reversed(self.owned):self.release(p)
        self.owned=[]

    def decode(self,p):
        kind=self.typeid(p)
        if kind==self.stringid:
            buf=C.create_string_buffer((self.strlen(p)+1)*4)
            return buf.value.decode('utf-8') if self.getstr(p,buf,len(buf),0x08000100) else ''
        if kind==self.boolid:return bool(self.boolean(p))
        if kind==self.numberid:
            n=C.c_longlong();self.number(p,4,C.byref(n));return n.value
        if kind==self.arrayid:
            return [self.decode_owned(self.retain(self.item(p,i))) for i in range(self.count(p))]
        return p

    def decode_owned(self,p):
        self.owned.append(p);return self.decode(p)

    def get(self,p,key,default=None):
        keyref=self.string(None,key.encode(),0x08000100);value=C.c_void_p()
        try:
            if self.copy(p,keyref,C.byref(value)) or not value.value:return default
            return self.decode_owned(value.value)
        finally:self.release(keyref)

    def action(self,p,name):
        key=self.string(None,name.encode(),0x08000100)
        try:return self.perform(p,key)==0
        finally:self.release(key)

    def set_bool(self,p,key,value):
        keyref=self.string(None,key.encode(),0x08000100)
        try:return self.setattr(p,keyref,self.true if value else self.false)==0
        finally:self.release(keyref)

    def press(self,p):
        key=self.string(None,b'AXPress',0x08000100)
        try:
            if not self.perform(p,key):return
        finally:self.release(key)
        # Sidebar cells and menu rows sometimes expose selection, not AXPress.
        node=p
        for _ in range(3):
            if self.get(node,'AXRole') in ('AXRow','AXCell','AXTab','AXRadioButton'):
                key=self.string(None,b'AXSelected',0x08000100)
                try:
                    if not self.setattr(node,key,self.true):return
                finally:self.release(key)
            node=self.get(node,'AXParent')
            if not node:break
        raise RoutingError('Zoom control does not expose a supported accessibility action.')

    def walk(self,p,depth=0):
        if depth>24:return
        yield p
        for child in self.get(p,'AXChildren',[]) or []:
            if isinstance(child,int):yield from self.walk(child,depth+1)

    def text(self,p):
        return ' '.join(dict.fromkeys(v for k in ('AXTitle','AXDescription','AXValue')
             if isinstance((v:=self.get(p,k)),str)))

    def find(self,p,predicate):
        return next((e for e in self.walk(p) if predicate(e)),None)


def zoom_pids():
    if sys.platform!='darwin':return set()
    result=subprocess.run(['/bin/ps','-axo','pid=,comm='],capture_output=True,text=True,timeout=5,check=True)
    found=set()
    for line in result.stdout.splitlines():
        values=line.strip().split(None,1)
        if len(values)==2 and values[1].endswith('/zoom.us.app/Contents/MacOS/zoom.us'):
            found.add(int(values[0]))
    return found


def new_client_pid(before,timeout=8):
    deadline=time.monotonic()+timeout
    while True:
        added=zoom_pids()-before
        if len(added)==1:return added.pop()
        if len(added)>1:raise RoutingError('Several Zoom processes appeared. Audio target is ambiguous; nothing was changed.')
        if time.monotonic()>=deadline:raise RoutingError('Zoom did not create a uniquely identifiable new client; audio was not changed.')
        time.sleep(.3)


def configure_audio(pid,device,channel,inspect_only=False):
    if sys.platform!='darwin':return dict(ok=False,message='Automatic input routing is currently available only on macOS.')
    if type(pid) is not int or type(channel) is not int or not 1<=channel<=256 or not isinstance(device,str) or not device:
        return dict(ok=False,message='Invalid process, input device or channel.')
    api=None
    try:
        if pid not in zoom_pids():raise RoutingError('The target Zoom client is no longer running.')
        api=AX()
        if not api.trusted():raise RoutingError('Accessibility access is required. Add Language Launcher in System Settings → Privacy & Security → Accessibility, then relaunch it.')
        app=api.create(pid);api.owned.append(app)
        def windows():return api.get(app,'AXWindows',[]) or []
        def settings():return next((w for w in windows() if api.get(w,'AXTitle')=='Settings'),None)
        window=settings();created=not bool(window)
        if not window:
            bar=api.get(app,'AXMenuBar')
            top=api.find(bar,lambda e:api.get(e,'AXTitle')=='Zoom Workplace') if bar else None
            if not top:raise RoutingError('Could not open Settings for the target Zoom process.')
            api.press(top);time.sleep(.2)
            item=api.find(bar,lambda e:api.get(e,'AXTitle') in ('Settings...','Settings…'))
            if not item:raise RoutingError('Zoom Settings menu item was not found.')
            api.press(item)
            for _ in range(20):
                window=settings()
                if window:break
                time.sleep(.15)
        if not window:raise RoutingError('Settings did not open in the target client.')
        audio=api.find(window,lambda e:api.get(e,'AXRole') in ('AXCell','AXStaticText') and api.text(e)=='Audio')
        if audio:api.press(audio);time.sleep(.2)
        def microphone():return api.find(window,lambda e:api.get(e,'AXRole') in ('AXMenuButton','AXPopUpButton') and api.text(e).startswith('Microphone'))
        mic=microphone()
        if not mic:raise RoutingError('Microphone control not found in this Zoom version.')
        if device not in api.text(mic):
            if inspect_only:raise RoutingError('Target client is not using '+device+'.')
            api.press(mic);time.sleep(.15)
            candidates=[]
            for w in windows():
                for e in api.walk(w):
                    if device in [api.get(e,k) for k in ('AXTitle','AXDescription','AXValue')]:candidates.append(e)
            if not candidates:raise RoutingError('The selected audio input was not found in Zoom.')
            api.press(candidates[-1]);time.sleep(.2)
            mic=microphone()
            if not mic or device not in api.text(mic):raise RoutingError('Zoom did not confirm the requested microphone device.')
        toggle=api.find(window,lambda e:api.get(e,'AXDescription')=='Use specific audio input channels' and api.get(e,'AXRole')!='AXStaticText')
        if not toggle:raise RoutingError('Zoom did not expose specific input channels for this device.')
        if not api.get(toggle,'AXValue'):
            if inspect_only:raise RoutingError('Specific input channels are disabled in the target client.')
            api.press(toggle);time.sleep(.2)
        def manage():return api.find(window,lambda e:api.get(e,'AXRole')=='AXButton' and 'Audio input channels' in api.text(e) and 'Manage' in api.text(e))
        button=manage()
        if not button:raise RoutingError('Zoom input-channel Manage button was not found.')
        api.press(button);time.sleep(.25)
        def channels():
            result={}
            for w in windows():
                for e in api.walk(w):
                    label=api.get(e,'AXDescription') or api.get(e,'AXTitle')
                    match=re.fullmatch(r'Channel (\d+)',label) if isinstance(label,str) else None
                    if match and api.get(e,'AXRole')=='AXCheckBox':result[int(match.group(1))]=e
            return result
        controls=channels()
        if channel not in controls:raise RoutingError('Channel '+str(channel)+' is not available in Zoom’s channel selector.')
        def selected():return sorted(n for n,e in channels().items() if api.get(e,'AXValue')==1)
        def dialog_button(title):
            return next((api.find(w,lambda e:api.get(e,'AXRole')=='AXButton' and api.get(e,'AXTitle')==title) for w in windows() if api.find(w,lambda e:api.get(e,'AXRole')=='AXButton' and api.get(e,'AXTitle')==title)),None)
        if not inspect_only:
            # Select the target first so the final enabled channel is never cleared.
            if not api.get(controls[channel],'AXValue'):
                api.press(controls[channel]);time.sleep(.05)
            for n in sorted(controls):
                fresh=channels()
                if n!=channel and n in fresh and api.get(fresh[n],'AXValue')==1:
                    api.press(fresh[n]);time.sleep(.05)
            if selected()!=[channel]:raise RoutingError('Zoom channel checkboxes did not match the requested single channel. Nothing was saved.')
            save=dialog_button('Save')
            if save and api.get(save,'AXEnabled')!=False:api.press(save);time.sleep(.2)
            else:
                cancel=dialog_button('Cancel')
                if cancel:api.press(cancel)
            button=manage()
            if not button:raise RoutingError('Could not reopen the channel selector to verify the saved selection.')
            api.press(button);time.sleep(.2)
        observed=selected()
        cancel=dialog_button('Cancel')
        if cancel:api.press(cancel)
        if observed!=[channel]:raise RoutingError('Saved Zoom selection is '+str(observed)+', expected only channel '+str(channel)+'.')
        if created:
            close=api.get(window,'AXCloseButton')
            if close:api.press(close)
        return dict(ok=True,pid=pid,device=device,channels=observed,message=device+' → input channel '+str(channel)+' verified in Zoom (PID '+str(pid)+').')
    except (OSError,RoutingError,subprocess.SubprocessError) as error:
        # Dismiss an unsaved channel dialog belonging only to this process.
        if api and 'app' in locals():
            try:
                for w in api.get(app,'AXWindows',[]) or []:
                    has_channels=api.find(w,lambda e:api.get(e,'AXRole')=='AXCheckBox' and str(api.get(e,'AXDescription','')).startswith('Channel '))
                    if has_channels:
                        cancel=api.find(w,lambda e:api.get(e,'AXRole')=='AXButton' and api.get(e,'AXTitle')=='Cancel')
                        if cancel:api.press(cancel)
            except (OSError,RoutingError):
                pass
        return dict(ok=False,pid=pid,message=str(error))
    finally:
        if api:api.close()


def accessibility_ready():
    if sys.platform!='darwin':return False
    api=AX()
    try:return bool(api.trusted())
    finally:api.close()


def close_idle_test_client(pid):
    """Quit only a client created for the offline test, and only while idle."""
    api=AX()
    try:
        if not api.trusted() or pid not in zoom_pids():return False
        app=api.create(pid);api.owned.append(app)
        windows=api.get(app,'AXWindows',[]) or []
        if any(re.search('meeting|webinar',str(api.get(w,'AXTitle','')),re.I) for w in windows):return False
        bar=api.get(app,'AXMenuBar')
        top=api.find(bar,lambda e:api.get(e,'AXTitle')=='Zoom Workplace') if bar else None
        if not top:return False
        api.press(top);time.sleep(.1)
        quit_item=api.find(bar,lambda e:api.get(e,'AXTitle')=='Quit Zoom Workplace')
        if not quit_item:return False
        api.press(quit_item)
        return True
    except (OSError,RoutingError):return False
    finally:api.close()
