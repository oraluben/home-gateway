"""Compatibility and ownership boundaries for shared gateway deployments."""
import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'image'))
import common
import deploy
import firewall
import host_policy
import network
import runtime
from configuration import render_mihomo
from topology import Topology, deployment_topology


def legacy():
    return {'network': {'interface': 'eth0', 'address': '192.168.50.201/24',
                        'clients': ['192.168.50.0/24'], 'tailscale_exit': True},
            'clash': {'enabled': True}, 'vpn': {'enabled': True, 'interface': 'vpn0'},
            'mihomo': {'external-controller': '192.168.50.201:9090', 'secret': 'test-key'}}


def router():
    return {'mode': 'router', 'lan': {'interface': 'lan0', 'address': '192.168.50.1/24'},
            'wan': {'interface': 'wan0', 'method': 'dhcp'}, 'tailscale_exit': False}


POLICY = {'up': True, 'prefixes': ['10.20.0.0/16'], 'dns': ['10.20.0.53'], 'domains': ['internal.invalid']}


class TopologyTests(unittest.TestCase):
    def test_existing_examples_use_the_same_model_as_generated_runtime(self):
        for name in ('deployment.example.json', 'deployment.linux.example.json'):
            with self.subTest(name=name):
                config = json.loads((ROOT / 'examples' / name).read_text())
                original = copy.deepcopy(config)
                model = deployment_topology(config)
                self.assertEqual(model, Topology.parse(model.as_dict()))
                self.assertEqual(model.lan.interface, config['runtime']['network']['interface'])
                self.assertEqual(model.wan.gateway, config['network']['gateway'])
                self.assertEqual(config, original)

    def test_canonical_deployment_has_one_network_source(self):
        config = {'network': router(), 'runtime': {}}
        self.assertEqual(deployment_topology(config).wan.method, 'dhcp')
        config['runtime']['network'] = {'interface': 'old0'}
        with self.assertRaisesRegex(ValueError, 'duplicated'):
            deployment_topology(config)

    def test_router_captures_on_lan_and_mihomo_exits_on_wan(self):
        config = legacy()
        config['network'] = router()
        config['mihomo']['external-controller'] = '192.168.50.1:9090'
        self.assertIn('iifname "lan0" ip saddr @clients jump proxy', firewall.render_proxy(config, POLICY))
        self.assertEqual(render_mihomo(config)['interface-name'], 'wan0')
        self.assertEqual(Topology.parse(config['network']).clients, ('192.168.50.0/24',))

    def test_unimplemented_router_setup_never_applies_one_arm_firewall(self):
        config = legacy()
        config['network'] = router()
        with patch.object(network, 'run') as command, self.assertRaisesRegex(ValueError, 'not implemented'):
            network.apply_base(config, POLICY)
        command.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'not implemented'):
            common.host_inputs({'network': router(), 'runtime': {}})

    def test_rejects_role_conflicts_and_static_dhcp_mix(self):
        cases = []
        value = router()
        value['wan']['interface'] = 'lan0'
        cases.append(value)
        value = router()
        value['wan']['address'] = '192.168.1.2/24'
        cases.append(value)
        value = router()
        value['wan'] = {'interface': 'wan0', 'method': 'static', 'address': '192.168.50.2/24', 'gateway': '192.168.50.254'}
        cases.append(value)
        value = router()
        value['interface'] = 'ambiguous0'
        cases.append(value)
        value = router()
        value['tailscale_exit'] = 'false'
        cases.append(value)
        value = router()
        value['wan']['interface'] = 'eth0;reboot'
        cases.append(value)
        value = {'mode': 'router', 'interface': 'eth0'}
        cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Topology.parse(value)

    def test_disconnected_vpn_is_protected_but_disabled_vpn_is_direct(self):
        config = legacy()
        self.assertIn('10.20.0.0/16', firewall.render_base(config, {**POLICY, 'up': False}))
        config['vpn'] = {'enabled': False}
        rules = firewall.render_base(config, POLICY)
        self.assertNotIn('10.20.0.0/16', rules)
        self.assertNotIn('vpn0', rules)
        self.assertIn('oifname { "eth0" } counter masquerade', rules)
        self.assertNotIn('10.20.0.0/16', firewall.render_proxy(config, POLICY))


class DeploymentBoundaryTests(unittest.TestCase):
    def config(self):
        return {'host': {'backend': 'linux'}, 'connection': {'Address': '192.168.50.1', 'User': 'gateway'},
                'network': router(), 'runtime': {'vpn': {'enabled': False}, 'clash': {'enabled': True},
                'subscription': {'enabled': True}, 'mihomo': {'external-controller': '192.168.50.1:9090'}},
                'secrets': {'controller': 'panel', 'subscription_url': 'subscription'}}

    def test_company_without_vpn_has_no_dependency_on_vpn_profiles_or_secrets(self):
        config = self.config()
        original = copy.deepcopy(config)
        values = {'panel': 'panel-value', 'subscription': 'subscription-value'}
        with patch.object(deploy, 'secret', side_effect=values.__getitem__) as read:
            bundle = deploy.materialize(config)
        self.assertEqual({call.args[0] for call in read.call_args_list}, set(values))
        self.assertEqual(bundle['runtime']['network'], deployment_topology(config).as_dict())
        self.assertEqual(bundle['runtime']['vpn'], {'enabled': False})
        self.assertNotIn('vpn_password', bundle)
        self.assertEqual(config, original)

    def test_disabled_proxy_never_unlocks_subscription_or_controller(self):
        config = self.config()
        config['runtime']['clash']['enabled'] = False
        config['runtime']['subscription']['url'] = 'STALE-PRIVATE'
        config['runtime']['mihomo']['secret'] = 'STALE-PRIVATE'
        with patch.object(deploy, 'secret', side_effect=AssertionError('unexpected decrypt')) as read:
            bundle = deploy.materialize(config)
        read.assert_not_called()
        self.assertNotIn('STALE-PRIVATE', json.dumps(bundle))
        # The host must not validate a disabled component's obsolete bind IP.
        config = json.loads((ROOT / 'examples/deployment.linux.example.json').read_text())
        config['runtime']['clash']['enabled'] = False
        config['runtime']['mihomo']['external-controller'] = '192.168.99.1:9090'
        self.assertEqual(common.host_inputs(config)['controller'], '127.0.0.1:9090')

    def test_invalid_flags_and_conflicting_topology_fail_before_decryption(self):
        config = self.config()
        config['runtime']['vpn']['enabled'] = 'false'
        with patch.object(deploy, 'secret') as read, self.assertRaises(ValueError):
            deploy.materialize(config)
        read.assert_not_called()

    def test_management_caches_are_isolated_and_readable_with_locked_gpg(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(common, 'OPERATOR_CACHE', pathlib.Path(directory) / 'deployment.json'):
            home = self.config()
            home['_config_source'] = 'pass:home-gateway/deployment'
            home['connection']['Address'] = '192.168.1.201'
            office = self.config()
            office['_config_source'] = 'pass:home-gateway/office'
            home_path, office_path = common.write_operator(home), common.write_operator(office)
            self.assertNotEqual(home_path, office_path)
            with patch.object(common, 'secret', side_effect=AssertionError('unexpected decrypt')):
                self.assertEqual(common.operator_deployment(home['_config_source'])['connection']['Address'], '192.168.1.201')
                self.assertEqual(common.operator_deployment(office['_config_source'])['connection']['Address'], '192.168.50.1')
            self.assertEqual(deployment_topology(common.operator_deployment(office['_config_source'])).wan.interface, 'wan0')


class HostOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.data = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(host_policy, 'DATA', self.data))
        self.apply = self.enterContext(patch.object(host_policy, 'apply_base'))
        self.run = self.enterContext(patch.object(host_policy, 'run'))
        self.enterContext(patch('builtins.print'))

    def test_steady_state_does_not_rewrite_firewall_or_dns(self):
        reconciler = host_policy.HostPolicy(legacy())
        with patch.object(host_policy.pathlib.Path, 'exists', return_value=True):
            reconciler.reconcile(POLICY)
            count = self.run.call_count
            reconciler.reconcile(POLICY)
        self.assertEqual(self.run.call_count, count)
        self.assertEqual(self.apply.call_count, 1)

    def test_vpn_disconnection_reverts_dns_but_retains_corporate_protection(self):
        reconciler = host_policy.HostPolicy(legacy())
        with patch.object(host_policy.pathlib.Path, 'exists', return_value=True):
            reconciler.reconcile(POLICY)
            status = reconciler.reconcile({**POLICY, 'up': False})
        self.assertEqual(self.apply.call_count, 1)
        self.run.assert_any_call(['resolvectl', 'revert', 'vpn0'])
        self.assertFalse(status['vpn_dns_active'])
        # A newly started host watcher rebuilds protection from the retained file.
        host_policy.HostPolicy(legacy()).reconcile({**POLICY, 'up': False})
        self.assertEqual(self.apply.call_args.args[1]['prefixes'], POLICY['prefixes'])

    def test_disabled_vpn_ignores_stale_policy_and_requires_no_interface(self):
        config = legacy()
        config['vpn'] = {'enabled': False}
        result = host_policy.HostPolicy(config).reconcile(POLICY)
        self.assertFalse(result['vpn_dns_active'])
        self.assertEqual(self.apply.call_args.args[1]['prefixes'], [])
        self.assertFalse(any('vpn0' in call.args[0] for call in self.run.call_args_list))

    def test_new_prefixes_are_applied_before_resolver_changes(self):
        events = []
        self.apply.side_effect = lambda *_: events.append('firewall')
        self.run.side_effect = lambda *_: events.append('resolver')
        with patch.object(host_policy.pathlib.Path, 'exists', return_value=True):
            host_policy.HostPolicy(legacy()).reconcile(POLICY)
        self.assertEqual(events[0], 'firewall')

    def test_importing_supervisor_has_no_effect_and_stop_does_not_touch_base(self):
        spec = importlib.util.spec_from_file_location('appliance_supervisor', ROOT / 'image/gateway.py')
        module = importlib.util.module_from_spec(spec)
        with patch.object(network, 'run', side_effect=AssertionError('unexpected kernel write')), \
                patch.object(runtime, 'load_config', side_effect=AssertionError('unexpected config read')):
            spec.loader.exec_module(module)
        config = legacy()
        config.update(clash={'enabled': False}, vpn={'enabled': False})
        with patch.object(module, 'DATA', self.data), patch.object(module, 'LOGS', self.data / 'logs'), \
                patch.object(module, 'load_config', return_value=config), patch.object(module, 'read_policy', return_value=POLICY), \
                patch.object(module.signal, 'signal'), patch.object(module.time, 'sleep', side_effect=InterruptedError), \
                patch.object(module, 'proxy_off') as proxy_off, patch.object(network, 'apply_base') as base, \
                self.assertRaises(InterruptedError):
            module.main()
        base.assert_not_called()
        self.assertTrue(proxy_off.called)
        self.assertEqual(json.loads((self.data / 'vpn-policy.json').read_text())['prefixes'], [])


class PolicyStateTests(unittest.TestCase):
    def test_concurrent_publishers_never_expose_partial_json_or_share_a_tempfile(self):
        with tempfile.TemporaryDirectory() as directory:
            data = pathlib.Path(directory)
            path = data / 'vpn-policy.json'
            runtime.save_json(path, POLICY)
            errors = []

            def publish(index):
                try:
                    for _ in range(25):
                        runtime.save_json(path, {**POLICY, 'publisher': index})
                        self.assertEqual(runtime.read_policy(data)['prefixes'], POLICY['prefixes'])
                except Exception as error:
                    errors.append(error)

            threads = [threading.Thread(target=publish, args=(i,)) for i in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual({item.name for item in data.iterdir()}, {'vpn-policy.json'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_retained_policy_does_not_silently_disable_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            data = pathlib.Path(directory)
            (data / 'vpn-policy.json').write_text('{broken')
            with self.assertRaises(ValueError):
                runtime.read_policy(data)
            self.assertEqual(runtime.read_policy(data, config={'vpn': {'enabled': False}})['prefixes'], [])


if __name__ == '__main__':
    unittest.main()
