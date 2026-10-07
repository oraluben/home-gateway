import importlib.util
import json
import types
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tailscale_host', ROOT / 'hosts/linux/tailscale.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
sys.path.insert(0, str(ROOT / 'tools'))
spec = importlib.util.spec_from_file_location('tailscale_operator', ROOT / 'tools/tailscale.py')
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)
sys.path.insert(0, str(ROOT / 'image'))
from tailscale_policy import advertised_prefixes, desired_routes
spec = importlib.util.spec_from_file_location('tailscale_policy_adapter', ROOT / 'tailscale-policy.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class ExitAdvertisementTests(unittest.TestCase):
    def test_cannot_advertise_without_the_appliance_adapter(self):
        with patch.object(operator, 'remote', return_value=json.dumps({'tailscale_exit': {'configured': False}})) as remote:
            with self.assertRaisesRegex(ValueError, 'Deploy'):
                operator.advertise_exit({}, True)
            self.assertEqual(remote.call_count, 1)

    def test_cannot_advertise_while_signed_out(self):
        with patch.object(operator, 'remote', side_effect=[json.dumps({'tailscale_exit': {'configured': True}}),
                                                        json.dumps({'BackendState': 'NeedsLogin'})]) as remote:
            with self.assertRaisesRegex(ValueError, 'Log in'):
                operator.advertise_exit({}, True)
            self.assertEqual(remote.call_count, 2)

    def test_emergency_withdrawal_does_not_require_a_running_appliance(self):
        with patch.object(operator, 'remote', return_value=b'') as remote, patch('builtins.print'):
            operator.advertise_exit({}, False)
        remote.assert_called_once_with({}, 'sudo tailscale set --advertise-exit-node=false --advertise-routes=')


class PushedRouteTests(unittest.TestCase):
    def test_publication_stops_after_four_failures(self):
        with patch.object(adapter, 'load_config', return_value={'network': {'tailscale_exit': True}}), \
                patch.object(adapter, 'read_policy', return_value={'prefixes': ['10.2.0.0/16']}), \
                patch.object(adapter, 'preferences', return_value={'AdvertiseRoutes': ['0.0.0.0/0','::/0']}), \
                patch.object(adapter.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1)) as run, \
                patch.object(adapter.time, 'monotonic', side_effect=iter(range(0,20000,100))), \
                patch.object(adapter.time, 'sleep', side_effect=[None]*6+[InterruptedError('end test')]), \
                patch.object(adapter, 'save_json') as save, patch('builtins.print'):
            with self.assertRaises(InterruptedError):
                adapter.main()
            self.assertEqual(run.call_count, 4)
            self.assertEqual(save.call_args.args[1]['state'], 'exhausted')

    def test_corporate_destinations_are_retained_while_vpn_is_down(self):
        policy = {'up': False, 'prefixes': ['10.2.0.0/16', '10.2.1.1/32'], 'dns': ['10.2.1.2', '223.5.5.5']}
        self.assertEqual(advertised_prefixes(policy), ['10.2.0.0/16', '223.5.5.5/32'])

    def test_exit_withdrawal_also_withdraws_pushed_routes(self):
        policy = {'prefixes': ['10.2.0.0/16']}
        self.assertEqual(desired_routes(policy, {'AdvertiseRoutes': ['0.0.0.0/0','::/0']}), ['10.2.0.0/16'])
        self.assertEqual(desired_routes(policy, {'AdvertiseRoutes': ['10.2.0.0/16']}), [])

    def test_changed_vpn_policy_replaces_old_prefixes_without_manual_configuration(self):
        prefs = {'AdvertiseRoutes': ['0.0.0.0/0','::/0','10.2.0.0/16']}
        self.assertEqual(desired_routes({'prefixes': ['172.20.1.0/24']}, prefs), ['172.20.1.0/24'])

    def test_default_and_tailnet_overlaps_are_rejected(self):
        for value in ('0.0.0.0/0', '100.64.0.0/10', '100.125.250.37/32', '100.0.0.0/8'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                advertised_prefixes({'prefixes': [value]})


class TailscaleHostTests(unittest.TestCase):
    def test_existing_unmanaged_installation_is_not_reconfigured(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host, 'RECEIPT', pathlib.Path(directory) / 'receipt'), \
                patch.object(host.os, 'geteuid', return_value=0), \
                patch.object(host.pathlib.Path, 'read_text', return_value='ID=ubuntu\nVERSION_ID="24.04"\n'), \
                patch.object(host.shutil, 'which', return_value='/usr/bin/tailscale'), \
                patch.object(host, 'run', return_value='x86_64') as run:
            with self.assertRaisesRegex(ValueError, 'not managed'):
                host.install('1.102.5')
        run.assert_called_once_with(['uname', '-m'])

    def test_existing_owned_install_preserves_login_routes_and_dns_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            receipt, dropin, sysctl = (root / name for name in ('receipt', 'service', 'sysctl'))
            receipt.write_text('{"schema":1,"firewall":"nftables","version":"1.102.5"}')
            dropin.write_text(host.SERVICE)
            sysctl.write_text(host.SYSCTL_BODY)
            read = pathlib.Path.read_text

            def contents(path, *args, **kwargs):
                return 'ID=ubuntu\nVERSION_ID="24.04"\n' if str(path) == '/etc/os-release' else read(path, *args, **kwargs)

            def execute(args, **kwargs):
                return 'x86_64' if args == ['uname', '-m'] else '1.102.5'

            with patch.object(host, 'RECEIPT', receipt), patch.object(host, 'DROPIN', dropin), \
                    patch.object(host, 'SYSCTL', sysctl), patch.object(host.os, 'geteuid', return_value=0), \
                    patch.object(host.pathlib.Path, 'read_text', contents), \
                    patch.object(host.shutil, 'which', return_value='/usr/bin/tailscale'), \
                    patch.object(host, 'run', side_effect=execute) as run:
                self.assertFalse(host.install('1.102.5')['changed'])
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ['uname', '-m'], ['tailscale', 'version'], ['tailscale', 'set', '--auto-update=false']])

    def test_compatibility_refuses_to_overwrite_unrelated_sysctl_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / 'sysctl'
            path.write_text('unrelated configuration\n')
            with patch.object(host, 'SYSCTL', path), patch.object(host, 'DROPIN', root / 'service'), \
                    patch.object(host, 'run') as run, self.assertRaisesRegex(ValueError, 'differs'):
                host.install_compatibility()
            run.assert_not_called()
            self.assertEqual(path.read_text(), 'unrelated configuration\n')
            self.assertFalse((root / 'service').exists())


if __name__ == '__main__':
    unittest.main()
