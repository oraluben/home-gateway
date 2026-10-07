import copy
import json
import pathlib
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'image'))
import network

RULE = {'priority': 17001, 'src': 'all', 'fwmark': '0x7001', 'table': '17001'}
ROUTE = {'type': 'local', 'dst': 'default', 'dev': 'lo', 'table': 17001, 'scope': 'host'}
READ_RULES = ['ip', '-j', '-4', 'rule', 'show']
READ_ROUTES = ['ip', '-j', '-4', 'route', 'show', 'table', 'all']
ADD_RULE = ['ip', '-4', 'rule', 'add', 'priority', '17001', 'fwmark', '0x7001', 'lookup', '17001']
ADD_ROUTE = ['ip', '-4', 'route', 'replace', 'local', '0.0.0.0/0', 'dev', 'lo', 'table', '17001']


class ProxyRoutingTests(unittest.TestCase):
    def test_proxy_mark_and_cleanup_do_not_touch_tailscale_reserved_bits(self):
        self.assertEqual(int(network.MARK, 0) & 0xff0000, 0)
        with patch.object(network, 'run') as run:
            network.proxy_off()
        for mark in (network.MARK, '0x17001'):
            run.assert_any_call(['ip', '-4', 'rule', 'del', 'priority', '17001', 'fwmark', mark,
                                 'lookup', '17001'], check=False)

    def inspect(self, rules=None, routes=None, repair=False):
        values = {tuple(READ_RULES): [copy.deepcopy(RULE)] if rules is None else rules,
                  tuple(READ_ROUTES): [copy.deepcopy(ROUTE)] if routes is None else routes}
        mutations = []

        def execute(args, **_):
            if tuple(args) in values:
                return types.SimpleNamespace(stdout=json.dumps(values[tuple(args)]), returncode=0)
            mutations.append(args)
            return types.SimpleNamespace(stdout='', returncode=0)

        with patch.object(network, 'run', side_effect=execute):
            result = network.ensure_proxy_routing() if repair else network.proxy_routing_status()
        return result, mutations

    def test_healthy_routing_is_a_read_only_noop(self):
        changed, mutations = self.inspect(repair=True)
        self.assertFalse(changed)
        self.assertEqual(mutations, [])
        status, _ = self.inspect()
        self.assertTrue(status['ready'])

    def test_network_service_deleting_rule_is_detected_and_repaired(self):
        status, _ = self.inspect(rules=[])
        self.assertFalse(status['ready'])
        self.assertFalse(status['rule_present'])
        self.assertTrue(status['local_route_present'])
        changed, mutations = self.inspect(rules=[], repair=True)
        self.assertTrue(changed)
        self.assertEqual(mutations, [ADD_RULE])

    def test_missing_table_and_rule_restore_route_before_rule(self):
        changed, mutations = self.inspect(rules=[], routes=[], repair=True)
        self.assertTrue(changed)
        self.assertEqual(mutations, [ADD_ROUTE, ADD_RULE])

    def test_missing_route_does_not_duplicate_existing_rule(self):
        changed, mutations = self.inspect(routes=[], repair=True)
        self.assertTrue(changed)
        self.assertEqual(mutations, [ADD_ROUTE])

    def test_unrelated_or_incomplete_routes_do_not_count_as_ready(self):
        cases = [{'table': 'local'}, {'dev': 'eth0'}, {'type': 'unicast'},
                 {'dst': '8.8.8.8/32'}]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                status, _ = self.inspect(routes=[{**ROUTE, **overrides}])
                self.assertFalse(status['ready'])

    def test_decimal_mark_numeric_table_and_full_mask_are_accepted(self):
        status, _ = self.inspect(rules=[{**RULE, 'fwmark': int(network.MARK, 0),
                                        'fwmask': '0xffffffff', 'table': 17001}])
        self.assertTrue(status['ready'])

    def test_priority_collision_is_reported_without_overwriting_other_rules(self):
        cases = [{'fwmark': '0x42'}, {'table': 'main'}, {'src': '192.168.1.2'},
                 {'fwmask': '0xff'}, {'iif': 'eth0'}, {'not': True}]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                rules = [{**RULE, **overrides}]
                status, mutations = self.inspect(rules=rules)
                self.assertFalse(status['ready'])
                self.assertTrue(status['priority_conflict'])
                self.assertEqual(mutations, [])
                with self.assertRaisesRegex(RuntimeError, 'occupied'):
                    self.inspect(rules=rules, repair=True)
        status, _ = self.inspect(rules=[RULE, {**RULE, 'fwmark': '0x42'}])
        self.assertFalse(status['ready'])
        self.assertTrue(status['priority_conflict'])

    def test_failed_kernel_inspection_never_reports_healthy(self):
        with patch.object(network, 'run', side_effect=subprocess.CalledProcessError(1, READ_RULES)):
            with self.assertRaises(subprocess.CalledProcessError):
                network.proxy_routing_status()


if __name__ == '__main__':
    unittest.main()
