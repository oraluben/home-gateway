import importlib.util
import base64
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import common
import deploy
from cloud_init import write_seed

spec = importlib.util.spec_from_file_location('linux_host', ROOT / 'hosts/linux/prepare.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)

INPUT = {'interface': 'enp1s0', 'network': {'address': '192.168.50.201/24', 'gateway': '192.168.50.1',
                                          'dns': ['192.168.50.1']}, 'controller': '192.168.50.201:9090'}


class HostTests(unittest.TestCase):
    def fixtures(self, overrides=None, command_overrides=None):
        files = {'/etc/os-release': 'ID=ubuntu\nVERSION_ID="24.04"\n', '/proc/1/comm': 'systemd\n',
                 '/proc/sys/kernel/osrelease': '6.8.0-generic', str(host.DAEMON): json.dumps(host.docker_settings({})),
                 str(host.SYSCTL): host.sysctl_text('enp1s0')}
        files.update({'/proc/sys/' + key: value for key, value in host.sysctls('enp1s0').items()})
        files.update(overrides or {})
        commands = {('uname', '-m'): (0, 'x86_64'),
                    ('ip', '-j', '-4', 'address', 'show', 'dev', 'enp1s0'): (0, json.dumps([{
                        'addr_info': [{'local': '192.168.50.201', 'prefixlen': 24, 'valid_life_time': 4294967295}]}])),
                    ('ip', '-j', '-4', 'route', 'show', 'default'): (0, json.dumps([{
                        'dev': 'enp1s0', 'gateway': '192.168.50.1'}])),
                    ('nft', '-j', 'list', 'tables'): (0, '{"nftables": []}'),
                    ('systemctl', 'is-active', '--quiet', 'systemd-resolved'): (0, '')}
        commands.update(command_overrides or {})

        def execute(args, *_, **__):
            status, stdout = commands.get(tuple(args), (1, '') if args[0] == 'systemctl' else (0, ''))
            return types.SimpleNamespace(returncode=status, stdout=stdout)

        return (patch.object(host, 'read', side_effect=lambda path, default='': files.get(str(path), default)),
                patch.object(host, 'command', side_effect=execute),
                patch.object(host.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name),
                patch.object(host.pathlib.Path, 'exists', autospec=True,
                             side_effect=lambda path: str(path) == '/dev/net/tun'))

    def check(self, **kwargs):
        contexts = self.fixtures(**kwargs)
        with contexts[0], contexts[1], contexts[2], contexts[3]:
            return host.inspect(INPUT)

    def test_ready_target_is_read_only_and_apply_is_a_noop(self):
        contexts = self.fixtures()
        with contexts[0], contexts[1], contexts[2], contexts[3], patch.object(host, 'run') as run, \
                patch.object(host, 'write_owned') as write:
            self.assertTrue(host.inspect(INPUT)['ready'])
            self.assertTrue(host.apply(INPUT)['ready'])
            run.assert_not_called()
            write.assert_not_called()

    def test_rejects_unsupported_system_and_network_conflicts(self):
        cases = [({'/proc/sys/kernel/osrelease': '6.6-microsoft-standard-WSL2'}, {}, 'WSL'),
                 ({'/etc/os-release': 'ID=debian\nVERSION_ID=12'}, {}, 'Ubuntu'),
                 ({'/etc/ufw/ufw.conf': 'ENABLED=yes\n'}, {}, 'UFW'),
                 ({}, {('docker', 'ps', '-a', '--format', '{{.Names}}'): (0, 'other-app\n')}, 'Other Docker'),
                 ({}, {('nft', '-j', 'list', 'tables'): (0, '{"nftables":[{"table":{"name":"firewall"}}]}')}, 'Unmanaged'),
                 ({}, {('ip', '-j', '-4', 'address', 'show', 'dev', 'enp1s0'): (0, '[]')}, 'static LAN'),
                 ({}, {('ss', '-H', '-lntup'): (0, 'udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:(("dnsmasq"))')}, 'DNS service')]
        for files, commands, message in cases:
            with self.subTest(message=message):
                report = self.check(overrides=files, command_overrides=commands)
                self.assertFalse(report['ready'])
                self.assertTrue(any(message in item for item in report['blockers']))

    def test_conflicting_host_is_not_modified(self):
        contexts = self.fixtures(command_overrides={('docker', 'ps', '-a', '--format', '{{.Names}}'): (0, 'other-app')})
        with contexts[0], contexts[1], contexts[2], contexts[3], patch.object(host, 'run') as run, \
                patch.object(host, 'write_owned') as write:
            self.assertTrue(host.apply(INPUT)['blockers'])
            run.assert_not_called()
            write.assert_not_called()

    def test_docker_options_are_preserved_and_conflicting_settings_rejected(self):
        existing = {'log-driver': 'json-file', 'log-opts': {'max-size': '20m'},
                    'registry-mirrors': ['https://mirror.example.invalid']}
        merged = host.docker_settings(existing)
        self.assertEqual(merged['log-opts'], existing['log-opts'])
        self.assertEqual(merged['registry-mirrors'], existing['registry-mirrors'])
        self.assertNotIn('bridge', existing)
        with self.assertRaises(ValueError):
            host.docker_settings({'iptables': True})

    def test_first_install_disables_package_firewall_service_before_docker_install(self):
        initial = {'supported': True, 'ready': False, 'blockers': [], 'changes': ['install']}
        ready = {'supported': True, 'ready': True, 'blockers': [], 'changes': []}
        with patch.object(host, 'inspect', side_effect=[initial, ready]), \
                patch.object(host, 'read', return_value='{}'), patch.object(host, 'command',
                return_value=types.SimpleNamespace(returncode=0, stdout='')), \
                patch.object(host.shutil, 'which', side_effect=lambda name: None if name in ('docker', 'nft') else '/usr/bin/' + name), \
                patch.object(host, 'run') as execute, patch.object(host, 'write_owned') as write, \
                patch.object(host.pathlib.Path, 'chmod'):
            self.assertTrue(host.apply(INPUT)['ready'])
        commands = [call.args[0] for call in execute.call_args_list]
        disable = commands.index(['systemctl', 'disable', '--now', 'nftables'])
        install = next(i for i, command in enumerate(commands) if 'docker-ce' in command)
        self.assertLess(disable, install)
        daemon = next(call.args[1] for call in write.call_args_list if call.args[0] == host.DAEMON)
        self.assertEqual(json.loads(daemon)['bridge'], 'none')
        self.assertIn(['sysctl', '-p', str(host.SYSCTL)], commands)
        self.assertFalse(any(command[:2] == ['sysctl', '--system'] for command in commands))

    def test_prepare_does_not_restart_a_running_gateway_for_daemon_changes(self):
        with patch.object(host, 'inspect', return_value={'supported': True, 'ready': False, 'blockers': [], 'changes': ['daemon']}), \
                patch.object(host, 'read', return_value='{}'), patch.object(host.shutil, 'which', return_value='/usr/bin/docker'), \
                patch.object(host, 'command', return_value=types.SimpleNamespace(returncode=0, stdout='home-gateway')), \
                patch.object(host, 'run') as run, patch.object(host, 'write_owned') as write:
            report = host.apply(INPUT)
            self.assertTrue(report['blockers'])
        run.assert_not_called()
        write.assert_not_called()

    def test_invalid_interface_is_rejected_before_any_host_command(self):
        with patch.object(host, 'command') as execute, self.assertRaises(ValueError):
            host.inspect({**INPUT, 'interface': 'eth0;reboot'})
        execute.assert_not_called()

    def test_sysctl_uses_the_actual_interface_including_vlan_names(self):
        text = host.sysctl_text('enp1s0.10')
        self.assertIn('net/ipv4/conf/enp1s0.10/rp_filter=0', text)
        self.assertNotIn('eth0', text)

    def test_old_hyperv_and_native_linux_configs_load_without_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'deployment.json'
            path.write_text(json.dumps({'hyperv': {'VMName': 'ExistingVM'}}))
            self.assertEqual(common.deployment(path)['host'], {'backend': 'hyperv', 'hyperv': {'VMName': 'ExistingVM'}})
            self.assertNotIn('host', json.loads(path.read_text()))
            path.write_text(json.dumps({'host': {'backend': 'linux'}, 'connection': {}}))
            self.assertNotIn('hyperv', common.deployment(path))

    def test_cloud_init_is_independent_of_hypervisor_and_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            public_key = 'ssh-ed25519 ' + base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes(32)).decode()
            config = {'connection': {'User': 'gateway', 'PublicKey': public_key},
                      'runtime': {'network': {'interface': 'enp1s0'}}, 'network': INPUT['network'],
                      'secrets': {'subscription_url': 'PRIVATE-SENTINEL'}}
            write_seed(config, '00155D500001', directory)
            body = ''.join(path.read_text() for path in pathlib.Path(directory).iterdir())
            self.assertIn('enp1s0', body)
            self.assertIn('00:15:5d:50:00:01', body)
            self.assertNotIn('PRIVATE-SENTINEL', body)

    def test_ssh_uses_linux_paths_and_expands_home(self):
        config = {'connection': {'Address': '192.168.50.201', 'User': 'gateway',
                                 'KeyPath': '~/.ssh/key', 'KnownHostsPath': '~/.ssh/known'}}
        command = common.ssh_command(config)
        self.assertEqual(command[0], 'ssh')
        self.assertEqual(command[2], str(pathlib.Path.home() / '.ssh/key'))

    def test_blocked_preflight_does_not_decrypt_or_upload_appliance_inputs(self):
        import importlib
        adapter = importlib.import_module('prepare-host')
        with patch.object(deploy, 'deployment', return_value={}), \
                patch.object(adapter, 'prepare', return_value={'ready': False, 'blockers': ['conflict']}), \
                patch.object(deploy, 'materialize') as materialize, patch.object(deploy, 'remote') as remote, \
                patch.object(sys, 'argv', ['deploy', '--validate-only']), patch.object(sys, 'stdout', io.StringIO()), \
                self.assertRaises(RuntimeError):
            deploy.main()
        materialize.assert_not_called()
        remote.assert_not_called()


if __name__ == '__main__':
    unittest.main()
