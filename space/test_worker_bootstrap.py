import copy
import unittest
from space import Store
from worker import initial_observation_commit_pending


class InitialCommitTests(unittest.TestCase):
    def setUp(self):
        self.observe = {'code': 'MODEL_UNKNOWN', 'registered': True,
                        'activeBinding': True, 'transport': 'connected',
                        'expectedModel': 'deepseek-flash', 'pendingModel': 'deepseek-flash',
                        'pendingProvider': 'deepseek-official', 'requiresPause': True,
                        'canObserve': False,
                        'freshness': {'pendingFrames': 1, 'backlogAgeMs': 360, 'ready': False}}

    def test_observed_first_commit_race_waits_without_authorizing_or_mutating(self):
        before = copy.deepcopy(self.observe)
        self.assertTrue(initial_observation_commit_pending(Store, self.observe))
        self.assertFalse(Store.initial_model_observation(self.observe))
        self.assertFalse(Store.observation_fresh(self.observe))
        self.assertEqual(before, self.observe)

    def test_five_second_or_unknown_queue_does_not_wait(self):
        for pending, age in [(1, 5000), (1, 5001), (0, 0), (None, 360), (1, None), (1, -1)]:
            with self.subTest(pending=pending, age=age):
                self.observe['freshness'].update(pendingFrames=pending, backlogAgeMs=age)
                self.assertFalse(initial_observation_commit_pending(Store, self.observe))

    def test_unknown_binding_model_or_transport_is_still_blocked(self):
        for key, value in [('code', 'MODEL_MISMATCH'), ('registered', False),
                           ('activeBinding', False), ('transport', 'disconnected'),
                           ('pendingModel', None), ('pendingProvider', 'other'),
                           ('expectedModel', 'other')]:
            with self.subTest(key=key):
                changed = dict(self.observe, **{key: value})
                self.assertFalse(initial_observation_commit_pending(Store, changed))


if __name__ == '__main__':
    unittest.main()
