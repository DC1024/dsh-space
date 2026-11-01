import json,subprocess,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import desktop

class DesktopTests(unittest.TestCase):
    def test_request_has_no_utf8_bom_or_ps7_module_path(self):
        with patch.object(desktop.subprocess,'run',return_value=SimpleNamespace(stdout='\ufeff{"ok":true}',returncode=0)) as run:
            self.assertTrue(desktop.call(Path('.'),'type',text='中文')['ok'])
            opts=run.call_args.kwargs
            self.assertEqual(opts['encoding'],'utf-8')
            self.assertEqual(json.loads(opts['input'])['text'],'中文')
            self.assertNotIn('codex',opts['env']['PSModulePath'])
            self.assertIn('WindowsPowerShell',opts['env']['PSModulePath'])
    def test_mutation_timeout_is_uncertain_and_never_retried(self):
        with patch.object(desktop.subprocess,'run',side_effect=subprocess.TimeoutExpired('guest',35)) as run:
            with self.assertRaises(desktop.DesktopError) as e:desktop.call(Path('.'),'click',x=0,y=0)
            self.assertTrue(e.exception.result['uncertain']);self.assertFalse(e.exception.result['retry_safe'])
            self.assertEqual(run.call_count,1)
    def test_readonly_failure_does_not_claim_uncertain_mutation(self):
        with patch.object(desktop.subprocess,'run',return_value=SimpleNamespace(stdout='{"ok":false,"error":"VM off"}',returncode=1)):
            with self.assertRaises(desktop.DesktopError) as e:desktop.call(Path('.'),'health')
            self.assertFalse(e.exception.result['uncertain'])

if __name__=='__main__':unittest.main()
