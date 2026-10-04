import base64
import copy
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import common
import deploy
import ssh_access
from cloud_init import write_seed


def public_key(byte, comment):
    # Synthetic public keys with no corresponding private keys in the repository.
    blob = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes([byte]) * 32
    return 'ssh-ed25519 ' + base64.b64encode(blob).decode() + ' ' + comment


MANAGEMENT = public_key(0, 'management')
LAPTOP = public_key(1, 'laptop')
OTHER = public_key(2, 'other')


class SshAccessTests(unittest.TestCase):
    def test_file_is_read_on_each_deployment_and_primary_key_is_always_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'keys'
            path.write_text('# my machines\n\n' + LAPTOP + '\n' + MANAGEMENT + '\n' + LAPTOP.replace('laptop', 'duplicate'))
            connection = {'User': 'gateway', 'PublicKey': MANAGEMENT, 'AuthorizedKeysFile': str(path)}
            self.assertEqual(ssh_access.deployment_access(connection)['keys'], [MANAGEMENT, LAPTOP])
            path.write_text(OTHER)
            self.assertEqual(ssh_access.deployment_access(connection)['keys'], [MANAGEMENT, OTHER])

    def test_missing_empty_private_and_malformed_sources_fail_before_credentials_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'keys'
            config = {'connection': {'User': 'gateway', 'PublicKey': MANAGEMENT, 'AuthorizedKeysFile': str(path)}}
            with patch.object(deploy, 'secret') as secret, self.assertRaises(FileNotFoundError):
                deploy.materialize(config)
            secret.assert_not_called()
            malformed = 'ssh-ed25519 ' + base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519X').decode()
            for body in ('# empty', '-----BEGIN OPENSSH PRIVATE KEY-----', 'ssh-ed25519 INVALID',
                         'command="restricted" ' + LAPTOP, MANAGEMENT + '\n' + malformed):
                path.write_text(body)
                with self.subTest(body=body), patch.object(deploy, 'secret') as secret, self.assertRaises(ValueError):
                    deploy.materialize(config)
                secret.assert_not_called()

    def test_existing_deployments_do_not_manage_ssh_without_an_explicit_file(self):
        self.assertIsNone(ssh_access.deployment_access({'User': 'gateway', 'PublicKey': MANAGEMENT}))
        self.assertIsNone(ssh_access.deployment_access({}))

    def test_cloud_init_and_deployment_use_identical_keys_without_runtime_or_image_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'keys'
            path.write_text(LAPTOP)
            config = {'connection': {'User': 'gateway', 'PublicKey': MANAGEMENT, 'AuthorizedKeysFile': str(path)},
                      'network': {'address': '192.168.50.201/24', 'gateway': '192.168.50.1', 'dns': ['192.168.50.1']},
                      'runtime': {'network': {'interface': 'eth0'}, 'vpn': {}, 'subscription': {}, 'mihomo': {}},
                      'vpn_profiles_pass': 'profiles', 'vpn_profile': 'vpn2',
                      'secrets': {'subscription_url': 'sub', 'controller': 'panel'}}
            before = copy.deepcopy(config)
            values = {'profiles': json.dumps({'vpn2': {'server': 'vpn.example.invalid', 'username': 'operator', 'credential': 'pw'}}),
                      'pw': 'PASSWORD', 'sub': 'SUBSCRIPTION', 'panel': 'PANEL'}
            with patch.object(deploy, 'secret', side_effect=values.__getitem__):
                bundle = deploy.materialize(config)
            seed = pathlib.Path(directory) / 'seed'
            write_seed(config, '00155D500001', seed)
            users = yaml.safe_load((seed / 'user-data').read_text())['users']
            self.assertEqual(users[0]['ssh_authorized_keys'], bundle['ssh_access']['keys'])
            self.assertEqual(bundle['ssh_access']['user'], 'gateway')
            self.assertNotIn(str(path), json.dumps(bundle))
            self.assertNotIn(LAPTOP, json.dumps(bundle['runtime']))
            self.assertNotIn(MANAGEMENT, json.dumps(bundle['runtime']))
            self.assertEqual(config, before)

    def test_metadata_retains_only_source_reference_and_not_file_contents(self):
        config = {'connection': {'PublicKey': MANAGEMENT, 'AuthorizedKeysFile': '~/.dotfiles/ssh/authorized_keys'},
                  'runtime': {'network': {'interface': 'eth0'}, 'mihomo': {'external-controller': '127.0.0.1:9090'}}}
        metadata = common.operator_values(config)
        self.assertEqual(metadata['connection']['AuthorizedKeysFile'], '~/.dotfiles/ssh/authorized_keys')
        self.assertNotIn('ssh_access', metadata)

    @unittest.skipIf(os.name == 'nt', 'Guest authorization ownership requires Linux')
    def test_authorization_replacement_removes_revoked_keys_and_can_be_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / '.ssh/authorized_keys'
            path.parent.mkdir()
            path.write_text(MANAGEMENT + '\n' + LAPTOP + '\n')
            path.chmod(0o600)
            target = {'path': path, 'uid': os.getuid(), 'gid': os.getgid(), 'body': (MANAGEMENT + '\n' + OTHER + '\n').encode()}
            snapshot = ssh_access.snapshot_access(target)
            ssh_access.write_access(target, target['body'])
            self.assertNotIn(LAPTOP, path.read_text())
            self.assertIn(MANAGEMENT, path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_uid, os.getuid())
            ssh_access.restore_access(target, snapshot)
            self.assertEqual(path.read_bytes(), snapshot['body'])

    @unittest.skipIf(os.name == 'nt', 'Guest authorization ownership requires Linux')
    def test_failed_atomic_replace_retains_existing_access_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / '.ssh/authorized_keys'
            path.parent.mkdir()
            path.write_text(MANAGEMENT + '\n')
            target = {'path': path, 'uid': os.getuid(), 'gid': os.getgid()}
            with patch.object(pathlib.Path, 'replace', side_effect=OSError('disk error')), self.assertRaises(OSError):
                ssh_access.write_access(target, (OTHER + '\n').encode())
            self.assertEqual(path.read_text(), MANAGEMENT + '\n')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_symlinked_authorization_path_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            outside = root / 'untouched'
            outside.write_text('ORIGINAL')
            path = root / 'authorized_keys'
            path.symlink_to(outside)
            with self.assertRaises(ValueError):
                ssh_access.write_access({'path': path}, b'REPLACEMENT')
            self.assertEqual(outside.read_text(), 'ORIGINAL')


if __name__ == '__main__':
    unittest.main()
