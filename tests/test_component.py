import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'image'))
import component


class Process:
    pid = 123456
    returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.returncode = -15


class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(component, 'DATA', pathlib.Path(self.directory)))
        self.now = 0
        self.enterContext(patch.object(component.time, 'monotonic', side_effect=lambda: self.now))
        self.enterContext(patch.object(component, 'read_policy', return_value={'up': True}))
        self.save = self.enterContext(patch.object(component, 'save_json'))
        self.enterContext(patch.object(component.os, 'killpg'))
        self.enterContext(patch('builtins.print'))
        self.spawn = self.enterContext(patch.object(component.subprocess, 'Popen', side_effect=lambda *_, **__: Process()))
        self.vpn = component.Component('vpn', ['fake-openconnect'], 4, retry_delay=15,
                                       retry_max_delay=60, stable_reset_seconds=600)
        self.addCleanup(lambda: self.vpn.log.close() if self.vpn.log and not self.vpn.log.closed else None)

    def fail(self):
        self.vpn.process.returncode = 1
        self.vpn.poll()

    def reconnect(self, delay):
        self.fail()
        self.assertEqual(self.vpn.status()['retry_in_seconds'], delay)
        self.now += delay
        self.vpn.poll()

    def test_failures_back_off_then_stop_after_three_retries(self):
        self.vpn.poll()
        for delay in (15, 30, 60):
            self.reconnect(delay)
        self.fail()
        self.assertEqual(self.vpn.status()['state'], 'stopped')
        self.assertEqual(self.vpn.attempts, 4)
        self.now += 10000
        self.vpn.poll(healthy=True)
        self.assertEqual(self.spawn.call_count, 4)
        self.assertEqual(self.save.call_count, 4)
        self.assertFalse(self.save.call_args.args[1]['up'])

    def test_retry_waits_for_the_deadline_and_reports_countdown(self):
        self.vpn.poll()
        self.fail()
        self.now = 14
        self.vpn.poll()
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(self.vpn.status()['retry_in_seconds'], 1)
        self.now = 15
        self.vpn.poll()
        self.assertEqual(self.spawn.call_count, 2)

    def test_stable_connected_session_replenishes_budget(self):
        self.vpn.poll()
        self.reconnect(15)
        self.vpn.poll(healthy=True)
        self.now += 599
        self.vpn.poll(healthy=True)
        self.assertEqual(self.vpn.attempts, 2)
        self.now += 1
        self.vpn.poll(healthy=True)
        self.assertEqual(self.vpn.attempts, 1)
        self.fail()
        self.assertEqual(self.vpn.status()['retry_in_seconds'], 15)

    def test_unhealthy_running_process_does_not_replenish_budget(self):
        self.vpn.poll()
        self.reconnect(15)
        self.now += 3600
        self.vpn.poll(healthy=False)
        self.assertEqual(self.vpn.attempts, 2)

    def test_uninterrupted_health_is_required_to_reset(self):
        self.vpn.poll()
        self.reconnect(15)
        self.vpn.poll(healthy=True)
        self.now += 599
        self.vpn.poll(healthy=False)
        self.now += 1
        self.vpn.poll(healthy=True)
        self.now += 599
        self.vpn.poll(healthy=True)
        self.assertEqual(self.vpn.attempts, 2)
        self.now += 1
        self.vpn.poll(healthy=True)
        self.assertEqual(self.vpn.attempts, 1)

    def test_manual_stop_remains_stopped_and_manual_retry_restores_budget(self):
        self.vpn.poll()
        self.vpn.disable()
        self.now += 10000
        self.vpn.poll(healthy=True)
        self.assertEqual(self.vpn.status()['state'], 'stopped')
        self.assertEqual(self.spawn.call_count, 1)
        self.vpn.retry()
        self.vpn.poll()
        self.assertEqual(self.vpn.status()['state'], 'running')
        self.assertEqual(self.vpn.attempts, 1)
        self.assertEqual(self.spawn.call_count, 2)

    def test_legacy_one_attempt_configuration_stays_finite(self):
        self.vpn.max_attempts = 1
        self.vpn.poll()
        self.fail()
        self.now += 10000
        self.vpn.poll()
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(self.vpn.status()['state'], 'stopped')

    def test_retry_policy_rejects_invalid_budgets_and_timing(self):
        for attempts in (0, -1, 21, '4'):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                component.Component('vpn', [], attempts)
        for timing in ({'retry_delay': 0}, {'retry_delay': 10, 'retry_max_delay': 5},
                       {'stable_reset_seconds': -1}):
            with self.subTest(timing=timing), self.assertRaises(ValueError):
                component.Component('vpn', [], 4, **timing)


if __name__ == '__main__':
    unittest.main()
