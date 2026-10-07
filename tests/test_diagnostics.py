import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'image'))
import diagnostics


def healthy_status():
    return {'version': 'test', 'controller': {'vpn_connected': True, 'transparent_proxy': True,
            'components': {'vpn': {'state': 'running', 'attempts': 2, 'max_attempts': 4, 'retry_in_seconds': None}}},
            'supervisor_status_fresh': True, 'proxy_routing': {'ready': True},
            'proxy_rules_present': True, 'base_rules_present': True, 'forwarding_enabled': True,
            'source_validation': {'ready': True},
            'dns_policy': {'vpn_dns_active': True, 'error': None}, 'subscription': {}}


class DiagnosticsTests(unittest.TestCase):
    def test_status_never_emits_credentials_or_subscription_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name, value in {'status.json': healthy_status()['controller'],
                                'deployment.json': {'version': 'test', 'private_field': 'PRIVATE-CONTENT'},
                                'subscription-status.json': {'state': 'success', 'url': 'PRIVATE-URL', 'secret': 'PRIVATE-CONTENT'}}.items():
                (root / name).write_text(json.dumps(value))
            config = {'network': {'interface': 'eth0'}, 'mihomo': {'secret': 'PRIVATE-CONTENT'},
                      'vpn': {'username': 'PRIVATE-USER'}, 'subscription': {'url': 'PRIVATE-URL'}}
            read_text = pathlib.Path.read_text

            def read(path, *args, **kwargs):
                if str(path) == '/proc/sys/net/ipv4/ip_forward':
                    return '1\n'
                return '0\n' if str(path).endswith('/src_valid_mark') else read_text(path, *args, **kwargs)

            with patch.object(diagnostics, 'DATA', root), patch.object(diagnostics, 'load_config', return_value=config), \
                 patch.object(diagnostics, 'proxy_routing_status', return_value={'ready': True}), \
                 patch.object(diagnostics, 'command', return_value=types.SimpleNamespace(returncode=0, stdout='[]')), \
                 patch.object(pathlib.Path, 'read_text', read):
                value = diagnostics.status()
                self.assertTrue(value['supervisor_status_fresh'])
                for secret in ('PRIVATE-CONTENT', 'PRIVATE-URL', 'PRIVATE-USER'):
                    self.assertNotIn(secret, json.dumps(value))
                os.utime(root / 'status.json', (0, 0))
                self.assertFalse(diagnostics.status()['supervisor_status_fresh'])

    def test_unhealthy_vpn_dns_or_routing_produces_nonzero_status(self):
        cases = [('vpn', {'controller': {'vpn_connected': False, 'components': {'vpn': {}}}}),
                 ('DNS', {'dns_policy': {'vpn_dns_active': False}}),
                 ('supervisor', {'supervisor_status_fresh': False}),
                 ('base', {'base_rules_present': False}),
                 ('proxy', {'controller': {'transparent_proxy': True, 'components': {'clash': {}}}, 'proxy_routing': {'ready': False}}),
                 ('source-validation', {'controller': {'transparent_proxy': True, 'components': {'clash': {}}},
                                        'source_validation': {'ready': False}})]
        for name, overrides in cases:
            with self.subTest(name=name), patch.object(diagnostics, 'status', return_value={**healthy_status(), **overrides}), \
                 patch('sys.stdout', io.StringIO()):
                self.assertEqual(diagnostics.main(['status', '--json']), 1)

    def test_check_distinguishes_protected_site_from_network_failure(self):
        for code, expected in (('200', True), ('204', True), ('401', True), ('403', True), ('000', False), ('503', False)):
            with self.subTest(code=code), patch.object(diagnostics.subprocess, 'run',
                    return_value=types.SimpleNamespace(returncode=0, stdout=code, stderr='PRIVATE-URL')):
                result = diagnostics.http_check('corporate_web', 'https://private.invalid/path')
                self.assertEqual(result['reachable'], expected)
                self.assertNotIn('private.invalid', json.dumps(result))
        with patch.object(diagnostics.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['curl'], 15)):
            self.assertFalse(diagnostics.http_check('corporate_web', 'https://private.invalid')['reachable'])

    def test_proxy_check_uses_the_mixed_proxy_and_ignores_inherited_bypass(self):
        with patch.object(diagnostics.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout='204')) as run:
            self.assertTrue(diagnostics.http_check('proxy_web', 'https://example.invalid', True)['reachable'])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--proxy') + 1], 'http://127.0.0.1:7897')
        self.assertEqual(command[command.index('--noproxy') + 1], '')

    def test_manual_actions_request_only_the_target_component(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(diagnostics, 'DATA', pathlib.Path(directory)), \
             patch('sys.stdout', io.StringIO()):
            self.assertEqual(diagnostics.main(['retry-vpn']), 0)
            self.assertEqual({item.name for item in pathlib.Path(directory).iterdir()}, {'retry-vpn'})

    def test_logs_are_bounded_without_loading_the_whole_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'logs').mkdir()
            (root / 'logs/vpn.log').write_text('first\nsecond\nthird\n')
            output = io.StringIO()
            with patch.object(diagnostics, 'DATA', root), patch('sys.stdout', output):
                self.assertEqual(diagnostics.main(['logs-vpn', '--lines', '2']), 0)
            self.assertEqual(output.getvalue(), 'second\nthird\n')

    def test_ssh_wrapper_handles_offline_container_and_forwards_running_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            commands = {'id': '#!/bin/sh\nprintf "0\\n"\n',
                        'docker': '#!/bin/sh\nif [ "$1" = inspect ]; then printf "%s\\n" "$FAKE_CONTAINER"; else printf "%s\\n" "$@"; fi\n',
                        'systemctl': '#!/bin/sh\nprintf "service states\\n"\n',
                        'tailscale': '#!/bin/sh\nprintf "%s\\n" "$@"\n',
                        'journalctl': '#!/bin/sh\nprintf "journal output\\n"\n'}
            for name, body in commands.items():
                path = root / name
                path.write_text(body)
                path.chmod(0o755)
            environment = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'], 'FAKE_CONTAINER': 'false'}
            offline = subprocess.run(['sh', str(ROOT / 'gatewayctl.sh'), 'status'], env=environment, capture_output=True, text=True)
            self.assertEqual(offline.returncode, 1)
            self.assertIn('container is not running', offline.stderr)
            self.assertIn('service states', offline.stdout)
            for action, expected in [('tailscale-status', 'status'), ('tailscale-netcheck', 'netcheck')]:
                result = subprocess.run(['sh', str(ROOT / 'gatewayctl.sh'), action, '--json'],
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.splitlines(), [expected, '--json'])
            environment['FAKE_CONTAINER'] = 'true'
            online = subprocess.run(['sh', str(ROOT / 'gatewayctl.sh'), 'status', '--json'], env=environment, capture_output=True, text=True)
            self.assertEqual(online.returncode, 0)
            self.assertEqual(online.stdout.splitlines(), ['exec', 'home-gateway', 'gatewayctl', 'status', '--json'])
            logs = subprocess.run(['sh', str(ROOT / 'gatewayctl.sh'), 'logs-system'], env=environment, capture_output=True, text=True)
            self.assertEqual(logs.stdout, 'journal output\n')


if __name__ == '__main__':
    unittest.main()
