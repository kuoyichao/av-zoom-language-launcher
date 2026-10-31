"""Process-scoped local-view actions. Audio setup never gates these actions."""
import re
import time
from zoom_audio_routing import AX, RoutingError, zoom_pids


def is_own_label(text,name):
    return name in text and bool(re.search(r'\([^)]*\bme\b[^)]*\)',text,re.I))


def apply_mac_window_actions(name,settings,pid=None):
    api=AX()
    results=[]
    try:
        if not api.trusted():
            return dict(ok=False,retry=False,message='Window actions need Accessibility permission for the process running the launcher. Use the updated start-command file to open the bundled Language Launcher app, allow that app in System Settings → Privacy & Security → Accessibility, then reopen it.')
        def app_for(pid):
            app=api.create(pid);api.owned.append(app);return app
        def meeting(app):
            for window in api.get(app,'AXWindows',[]) or []:
                if api.get(window,'AXIdentifier')=='zm.meeting.window.main':return window
                if re.search(r'meeting|webinar',str(api.get(window,'AXTitle','')),re.I):
                    if api.find(window,lambda e:api.get(e,'AXRole') in ('AXButton','AXMenuButton') and re.search(r'participants|mute|audio',api.text(e),re.I)):
                        return window
            return None
        if pid is None:
            matches=[]
            for candidate in zoom_pids():
                app=app_for(candidate);window=meeting(app)
                if window and api.find(window,lambda e:is_own_label(api.text(e),name)):
                    matches.append((candidate,app,window))
            if len(matches)!=1:
                return dict(ok=False,retry=True,message='Could not uniquely identify this joined client. Relaunch it with the updated launcher, or open its Participants panel so its name is marked (me).')
            pid,app,window=matches[0]
        else:
            if pid not in zoom_pids():return dict(ok=False,retry=False,message='The tracked Zoom client has exited.')
            app=app_for(pid);window=meeting(app)
            if not window:return dict(ok=False,retry=True,message='The tracked client has no joined meeting window yet. Complete admission or joining first.')
        def exact_button(root,label):
            return api.find(root,lambda e:api.get(e,'AXRole') in ('AXButton','AXMenuButton') and (api.get(e,'AXTitle')==label or api.get(e,'AXDescription')==label))
        def menu_item(labels):
            # Exact local menu labels only; never click Spotlight or host actions.
            return api.find(app,lambda e:api.get(e,'AXRole')=='AXMenuItem' and str(api.get(e,'AXTitle','')).casefold() in labels)
        def cancel_menu():
            menu=api.find(app,lambda e:api.get(e,'AXRole')=='AXMenu')
            if menu:api.action(menu,'AXCancel')
        pin_ok=not settings.get('pin_self')
        if settings.get('pin_self'):
            try:
                own=api.find(window,lambda e:is_own_label(api.text(e),name))
                if not own:
                    participants=api.find(window,lambda e:api.get(e,'AXRole') in ('AXButton','AXMenuButton') and bool(re.match(r'(Show )?Participants\b',api.text(e),re.I)))
                    if participants:api.press(participants);time.sleep(.3)
                    own=api.find(window,lambda e:is_own_label(api.text(e),name))
                if not own:raise RoutingError('Own profile could not be identified by its (me) label. Pin was not attempted.')
                node=own;trigger=None
                for _ in range(4):
                    choices=[e for e in api.walk(node) if api.get(e,'AXRole') in ('AXButton','AXMenuButton') and 'more' in api.text(e).lower() and name in api.text(e)]
                    if len(choices)==1:trigger=choices[0];break
                    node=api.get(node,'AXParent')
                    if not node:break
                if trigger:api.press(trigger);time.sleep(.2)
                remove=menu_item({'remove pin','unpin'})
                pin=menu_item({'pin','pin video','add pin','pin for myself'})
                if not pin and not remove:
                    cancel_menu()
                    tiles=[e for e in api.walk(window) if api.text(e).startswith('Video render '+name)]
                    if len(tiles)==1 and api.action(tiles[0],'AXShowMenu'):
                        time.sleep(.2)
                        remove=menu_item({'remove pin','unpin'});pin=menu_item({'pin','pin video','add pin','pin for myself'})
                if remove:
                    pin_ok=True;cancel_menu();results.append('Own profile is already pinned.')
                elif pin:
                    api.press(pin);time.sleep(.3)
                    indicator=api.find(window,lambda e:re.fullmatch(r'remove pin|unpin',api.text(e),re.I) is not None)
                    if not indicator and trigger:
                        api.press(trigger);time.sleep(.2);indicator=menu_item({'remove pin','unpin'});cancel_menu()
                    if not indicator:raise RoutingError('Pin was requested, but Zoom did not expose a verified pinned state.')
                    pin_ok=True;results.append('Own profile pin verified.')
                else:raise RoutingError('Zoom did not expose a Pin action for this self profile. Camera remains off.')
            except RoutingError as error:
                cancel_menu();results.append(str(error))
        mini_ok=not settings.get('mini_window')
        if settings.get('mini_window'):
            # Always try the requested minimize action even if self-pin failed.
            try:
                button=api.get(window,'AXMinimizeButton') or exact_button(window,'Minimize')
                if button:api.press(button)
                elif not api.set_bool(window,'AXMinimized',True):raise RoutingError('Zoom did not expose a working minimize control.')
                time.sleep(.5)
                indicator=api.find(app,lambda e:bool(re.search(r'show full meeting window|exit minimized video|expand meeting|exit mini',api.text(e),re.I)))
                if indicator:
                    mini_ok=True;results.append('Floating mini window verified.')
                elif api.get(window,'AXMinimized'):
                    results.append('Zoom window minimized; floating mini view could not be confirmed. Check Zoom’s mini-window setting.')
                else:results.append('Minimize requested; floating mini window could not be confirmed.')
            except RoutingError as error:results.append(str(error))
        return dict(ok=pin_ok and mini_ok,retry=False,pin_ok=pin_ok,mini_ok=mini_ok,pid=pid,message=' '.join(results))
    except (OSError,RoutingError) as error:
        return dict(ok=False,retry=False,message='Window action error: '+str(error))
    finally:api.close()
