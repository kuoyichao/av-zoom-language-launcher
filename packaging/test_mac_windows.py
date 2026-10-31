import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import zoom_mac_windows as w

class FakeAX:
    def __init__(self):self.owned=[];self.pressed=[]
    def trusted(self):return True
    def create(self,pid):return 'app'
    def get(self,node,key,default=None):
        return {('app','AXWindows'):['meeting'],('meeting','AXIdentifier'):'zm.meeting.window.main',('meeting','AXMinimizeButton'):'minimize'}.get((node,key),default)
    def text(self,node):return 'Show full meeting window' if node=='indicator' else ''
    def find(self,root,predicate):
        return 'indicator' if root=='app' and predicate('indicator') else None
    def press(self,node):self.pressed.append(node)
    def close(self):pass

class WindowTests(unittest.TestCase):
    def test_pin_failure_still_minimizes_tracked_client(self):
        api=FakeAX()
        with patch.object(w,'AX',return_value=api),patch.object(w,'zoom_pids',return_value={42}),patch.object(w.time,'sleep'):
            result=w.apply_mac_window_actions('*Z N Chinese',dict(pin_self=True,mini_window=True),pid=42)
        self.assertFalse(result['pin_ok'])
        self.assertTrue(result['mini_ok'])
        self.assertEqual(api.pressed,['minimize'])
        self.assertEqual(result['pid'],42)
    def test_permission_failure_does_not_claim_success(self):
        api=FakeAX();api.trusted=lambda:False
        with patch.object(w,'AX',return_value=api):result=w.apply_mac_window_actions('name',dict(pin_self=True,mini_window=True))
        self.assertFalse(result['ok']);self.assertIn('Accessibility',result['message']);self.assertEqual(api.pressed,[])
