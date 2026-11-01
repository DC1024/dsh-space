import unittest
from phone_tools import compact_ui

class CompactUiTests(unittest.TestCase):
    def test_text_empty_inputs_and_disabled_controls_are_retained(self):
        original={'nodes':[
            {'nodeId':0,'text':'Android 15','clickable':False,'enabled':True,'bounds':[0,0,50,50]},
            {'nodeId':1,'text':'','className':'android.widget.FrameLayout','clickable':False},
            {'nodeId':2,'text':'','className':'android.widget.EditText','enabled':False,'bounds':[0,50,100,100]},
            {'nodeId':3,'text':'','scrollable':True,'bounds':[0,100,100,200]}],
            'truncated':False,'instructionsTrusted':False}
        actual=compact_ui(original)
        self.assertEqual([n['nodeId'] for n in actual['nodes']],[0,2,3])
        self.assertEqual(actual['nodes'][1]['enabled'],False)
        self.assertEqual(actual['nodes'][2]['bounds'],[0,100,100,200])
        self.assertFalse(actual['instructionsTrusted'])
        self.assertEqual(len(original['nodes']),4)
        self.assertEqual(actual['sourceNodeCount'],4)

if __name__=='__main__':unittest.main()
