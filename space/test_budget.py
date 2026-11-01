from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from budget import BudgetAuthority, BudgetDenied


class BudgetTests(unittest.TestCase):
    def test_cancel_requires_literal_confirmation_and_cannot_cancel_settled(self):
        with tempfile.TemporaryDirectory() as tmp:
            authority=BudgetAuthority(Path(tmp)/'budget.sqlite', stop_at='1')
            authority.reserve('cancel-check',.4,'fixture')
            for unknown in (False,None,'true',1):
                with self.assertRaises(BudgetDenied):
                    authority.cancel_unstarted('cancel-check',unknown)
            self.assertEqual(authority.cancel_unstarted('cancel-check',True)['state'],'cancelled')
            self.assertTrue(authority.cancel_unstarted('cancel-check',True)['duplicate'])
            authority.reserve('settled-check',.4,'fixture')
            authority.settle('settled-check',.3)
            with self.assertRaises(BudgetDenied):authority.cancel_unstarted('settled-check',True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'budget.sqlite'
        self.authority = BudgetAuthority(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_consumers_cannot_overreserve(self):
        def attempt(i):
            try:
                BudgetAuthority(self.path).reserve(str(i), 1, 'consumer-' + str(i))
                return True
            except BudgetDenied:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(30)))
        self.assertEqual(sum(results), 19)

    def test_unknown_result_holds_reservation_and_idempotent_settlement(self):
        a = self.authority
        a.reserve('r1', 19, 'phone')
        with self.assertRaises(BudgetDenied):
            a.cancel_unstarted('r1')
        with self.assertRaises(BudgetDenied):
            a.reserve('r2', 1, 'desktop')
        a.settle('r1', 2)
        self.assertTrue(a.settle('r1', 2)['duplicate'])
        with self.assertRaises(BudgetDenied):
            a.settle('r1', 1)
        a.reserve('r2', 1, 'desktop')

    def test_legacy_import_preserves_amount_and_rejects_changed_file(self):
        source = self.root / 'legacy.json'
        original = json.dumps({'days': {'2026-10-20': {'estimatedCny': 19.5048624}}}).encode()
        source.write_bytes(original)
        a = self.authority
        a.import_legacy_snapshot(source)
        self.assertTrue(a.import_legacy_snapshot(source)['duplicate'])
        self.assertEqual(source.read_bytes(), original)
        with self.assertRaises(BudgetDenied):
            a.reserve('old-day', .01, 'test', '2026-10-20')
        a.reserve('new-day', 1, 'test', '2026-10-21')
        source.write_text('{"days":{}}')
        with self.assertRaises(BudgetDenied):
            a.import_legacy_snapshot(source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
