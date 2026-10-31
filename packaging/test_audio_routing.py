import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest
from unittest.mock import patch
import zoom_audio_routing as z

class FakeAX:
    def __init__(self):
        self.owned=[];self.saved=set(range(1,33));self.settings={'AXTitle':'Settings','AXChildren':[]};self.app={'AXWindows':[self.settings]}
        self.manage={'AXRole':'AXButton','AXTitle':'Manage','AXDescription':'Audio input channels'}
        self.settings['AXChildren']=[{'AXRole':'AXMenuButton','AXDescription':'Microphone Current microphone, Loopback Audio'},{'AXRole':'AXCheckBox','AXDescription':'Use specific audio input channels','AXValue':1},self.manage]
    def trusted(self):return True
    def create(self,pid):return self.app
    def get(self,node,key,default=None):return node.get(key,default)
    def text(self,node):return ' '.join(str(node.get(k,'')) for k in ['AXTitle','AXDescription']).strip()
    def walk(self,node):
        yield node
        for child in node.get('AXChildren',[]):yield from self.walk(child)
    def find(self,node,predicate):return next((n for n in self.walk(node) if predicate(n)),None)
    def press(self,node):
        if node is self.manage:
            boxes=[{'AXRole':'AXCheckBox','AXDescription':f'Channel {i}','AXValue':int(i in self.saved)} for i in range(1,33)]
            self.dialog={'AXChildren':boxes+[{'AXRole':'AXButton','AXTitle':'Save','AXEnabled':True},{'AXRole':'AXButton','AXTitle':'Cancel'}]}
            self.app['AXWindows'].append(self.dialog)
        elif node.get('AXRole')=='AXCheckBox':node['AXValue']=1-node['AXValue']
        elif node.get('AXTitle')=='Save':
            self.saved={int(n['AXDescription'].split()[1]) for n in self.dialog['AXChildren'] if n.get('AXRole')=='AXCheckBox' and n['AXValue']==1}
            self.app['AXWindows'].remove(self.dialog)
        elif node.get('AXTitle')=='Cancel':self.app['AXWindows'].remove(self.dialog)
    def close(self):pass

class RoutingTests(unittest.TestCase):
    def test_selects_one_channel_saves_and_reopens_to_verify(self):
        fake=FakeAX()
        with patch.object(z,'AX',return_value=fake),patch.object(z,'zoom_pids',return_value={123}),patch.object(z.time,'sleep'):
            result=z.configure_audio(123,'Loopback Audio',18)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['channels'],[18])
        self.assertEqual(fake.saved,{18})
        self.assertEqual(len(fake.app['AXWindows']),1)
    def test_readback_detects_combined_channels_without_saving(self):
        fake=FakeAX()
        with patch.object(z,'AX',return_value=fake),patch.object(z,'zoom_pids',return_value={123}),patch.object(z.time,'sleep'):
            result=z.configure_audio(123,'Loopback Audio',18,inspect_only=True)
        self.assertFalse(result['ok'])
        self.assertEqual(fake.saved,set(range(1,33)))
    def test_invalid_or_exited_client_is_rejected(self):
        self.assertFalse(z.configure_audio('123','Loopback Audio',18)['ok'])
        with patch.object(z,'zoom_pids',return_value=set()):self.assertFalse(z.configure_audio(123,'Loopback Audio',18)['ok'])

if __name__=='__main__':unittest.main(verbosity=2)
