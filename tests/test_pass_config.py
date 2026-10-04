import io
import json
import os
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


def configuration():
    return {'host': {'backend': 'linux'}, 'connection': {'Address': '192.168.50.201', 'User': 'gateway'},
            'network': {'address': '192.168.50.201/24', 'gateway': '192.168.50.1', 'dns': ['192.168.50.1']},
            'runtime': {'network': {'interface': 'enp1s0'}, 'vpn': {}, 'subscription': {},
                        'mihomo': {'external-controller': '192.168.50.201:9090', 'secret': 'PRIVATE-PANEL'}},
            'vpn_profiles_pass': 'vpn/profiles', 'vpn_profile': 'vpn2',
            'secrets': {'subscription_url': 'sub', 'controller': 'panel'}}


class PassConfigTests(unittest.TestCase):
    def test_default_pass_store_works_without_yadm_or_plaintext_config(self):
        with tempfile.TemporaryDirectory() as directory:
            store = pathlib.Path(directory) / 'store'
            marker = store / 'home-gateway/deployment.gpg'
            marker.parent.mkdir(parents=True)
            marker.touch()
            cache = pathlib.Path(directory) / 'missing.json'
            with patch.dict(os.environ, {'PASSWORD_STORE_DIR': str(store), 'HOME_GATEWAY_DEPLOYMENT': ''}), \
                    patch.object(common, 'OPERATOR_CACHE', cache), patch.object(common, 'secret', return_value=json.dumps(configuration())) as read:
                config = common.deployment()
            read.assert_called_once_with('home-gateway/deployment')
            self.assertEqual(config['host']['backend'], 'linux')
            self.assertFalse(cache.exists())

    def test_deployment_uses_fresh_pass_config_instead_of_stale_operator_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'metadata.json'
            path.write_text(json.dumps({'config_pass': 'home-gateway/deployment',
                                        'connection': {'Address': 'OLD-ADDRESS'}, 'runtime': {}}))
            with patch.object(common, 'secret', return_value=json.dumps(configuration())):
                config = common.deployment(path)
            self.assertEqual(config['connection']['Address'], '192.168.50.201')

    def test_local_metadata_excludes_credentials_and_does_not_require_unlock_for_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'metadata.json'
            config = configuration()
            config['_config_source'] = 'pass:home-gateway/deployment'
            config['runtime']['subscription']['url'] = 'PRIVATE-SUBSCRIPTION'
            config['connection']['Password'] = 'PRIVATE-SSH-PASSWORD'
            with patch.object(common, 'OPERATOR_CACHE', path):
                common.write_operator(config)
                with patch.object(common, 'secret', side_effect=RuntimeError('GPG locked')) as read:
                    metadata = common.operator_deployment('pass:home-gateway/deployment')
            body = path.read_text()
            self.assertFalse(any(value in body for value in ('PRIVATE-', 'vpn_profiles_pass', 'subscription_url')))
            self.assertEqual(metadata['connection']['Address'], config['connection']['Address'])
            read.assert_not_called()

    def test_profile_and_password_are_resolved_from_pass_without_dotfiles(self):
        config = configuration()
        values = {'vpn/profiles': json.dumps({'vpn2': {'server': 'vpn.example.invalid', 'username': 'operator', 'credential': 'shared-vpn'}}),
                  'shared-vpn': 'VPN-VALUE', 'sub': 'SUB-VALUE', 'panel': 'PANEL-VALUE'}
        with patch.object(deploy, 'secret', side_effect=values.__getitem__):
            bundle = deploy.materialize(config)
        self.assertEqual(bundle['vpn_password'], 'VPN-VALUE')
        self.assertEqual(bundle['runtime']['vpn']['server'], 'vpn.example.invalid')
        self.assertNotIn('server', config['runtime']['vpn'])
        self.assertNotIn('vpn_profiles_pass', bundle)

    def test_invalid_json_does_not_include_decrypted_contents_in_error(self):
        with patch.object(common, 'secret', return_value='PRIVATE-CONTENTS malformed'), self.assertRaises(ValueError) as error:
            common.deployment('pass:home-gateway/deployment')
        self.assertNotIn('PRIVATE-CONTENTS', str(error.exception))

    def test_native_manager_uses_default_ssh_paths_without_platform_configuration(self):
        with patch.object(common.pathlib.Path, 'exists', return_value=False):
            command = common.ssh_command(configuration())
        self.assertEqual(command[0], 'ssh')
        self.assertEqual(command[2], str(pathlib.Path.home() / '.ssh/home-gateway_ed25519'))
        self.assertIn('UserKnownHostsFile=' + str(pathlib.Path.home() / '.ssh/home-gateway_known_hosts'), command)

    def test_wsl_manager_can_reuse_windows_keys_without_private_transport_configuration(self):
        with patch.object(common.pathlib.Path, 'exists', autospec=True,
                          side_effect=lambda path: str(path) == '/proc/sys/kernel/osrelease'), \
                patch.object(common.pathlib.Path, 'read_text', return_value='6.6-microsoft-standard-WSL2'), \
                patch.object(common.shutil, 'which', side_effect=lambda name: '/windows/' + name), \
                patch.object(common.subprocess, 'run', return_value=types.SimpleNamespace(stdout='C:\\Users\\operator\r\n')) as run:
            command = common.ssh_command(configuration())
        self.assertEqual(command[0], '/windows/ssh.exe')
        self.assertEqual(command[2], 'C:/Users/operator/.ssh/home-gateway_ed25519')
        self.assertIn('UserKnownHostsFile=C:/Users/operator/.ssh/home-gateway_known_hosts', command)
        run.assert_called_once()

    def test_wsl_prefers_existing_linux_keys(self):
        with patch.object(common.pathlib.Path, 'exists', return_value=True), \
                patch.object(common.pathlib.Path, 'read_text', return_value='6.6-microsoft-standard-WSL2'), \
                patch.object(common.subprocess, 'run') as run:
            transport = common.ssh_settings(configuration())
        self.assertEqual(transport['SshExecutable'], 'ssh')
        run.assert_not_called()

    def test_render_replaces_legacy_symlink_without_changing_its_tracked_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            original = root / 'legacy.json'
            original.write_text('ORIGINAL')
            cache = root / 'deployment.json'
            cache.symlink_to(original)
            config = configuration()
            config['_config_source'] = 'pass:home-gateway/deployment'
            common.write_operator(config, cache)
            self.assertFalse(cache.is_symlink())
            self.assertEqual(original.read_text(), 'ORIGINAL')
            self.assertEqual(json.loads(cache.read_text())['config_pass'], 'home-gateway/deployment')


if __name__ == '__main__':
    unittest.main()
