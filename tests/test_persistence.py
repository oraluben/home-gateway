import importlib.util
from contextlib import closing
import hashlib
import io
import json
import pathlib
import sqlite3
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import backup
import deploy

spec = importlib.util.spec_from_file_location('installer', ROOT / 'tools/guest-install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def snapshot(extra=None, components=None, include_subscription=True):
    stream = io.BytesIO()
    entries = {'snapshot.json': json.dumps({'format': 1, 'selected': {'AI': 'US-1'}}).encode(),
               'versions.json': b'{}', 'config/gateway.yaml': b'private',
               'data/subscription/current.yaml': b'cached'}
    if components is not None:
        entries['snapshot.json'] = json.dumps({'format': 1, 'selected': {}, 'components': components}).encode()
    if not include_subscription:
        entries.pop('data/subscription/current.yaml')
    if extra:
        entries.update(extra)
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, body in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
    return stream.getvalue()


class PersistenceTests(unittest.TestCase):
    def test_snapshot_without_subscription_is_valid_only_when_explicitly_disabled(self):
        metadata, count = backup.inspect_snapshot(snapshot(components={'clash': False, 'subscription': False}, include_subscription=False))
        self.assertEqual(count, 3)
        self.assertEqual(metadata['selected'], {})
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            backup.inspect_snapshot(snapshot(include_subscription=False))
        with self.assertRaisesRegex(ValueError, 'Invalid snapshot components'):
            backup.inspect_snapshot(snapshot(components={'clash': 'false', 'subscription': False}, include_subscription=False))

    def test_disabled_proxy_preparation_has_no_subscription_or_renderer_dependency(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(installer.importlib, 'import_module') as imported:
            root = pathlib.Path(directory)
            self.assertFalse(installer.prepare_mihomo({'clash': {'enabled': False}}, root / 'data', root))
        imported.assert_not_called()

    def test_usage_database_backup_and_rollback_restore_the_original_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            data = pathlib.Path(directory) / 'data'
            backup = pathlib.Path(directory) / 'backup'
            data.mkdir()
            backup.mkdir()
            with closing(sqlite3.connect(data / 'usage.sqlite3')) as db:
                db.executescript('CREATE TABLE counter(value INTEGER); INSERT INTO counter VALUES(42); PRAGMA user_version=1;')
            installer.snapshot_usage(data, backup)
            with closing(sqlite3.connect(data / 'usage.sqlite3')) as db:
                db.executescript('UPDATE counter SET value=99; PRAGMA user_version=2;')
            installer.restore_usage(data, backup, True)
            with closing(sqlite3.connect(data / 'usage.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT value FROM counter').fetchone()[0], 42)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_restore_rejects_traversal_and_unexpected_credentials(self):
        for name in ('../outside', '/etc/shadow', 'config/other-secret', 'data/logs/vpn.log'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                backup.inspect_snapshot(snapshot({name: b'should-not-restore'}))

    def test_snapshot_keeps_selections_without_requiring_a_live_database(self):
        metadata, count = backup.inspect_snapshot(snapshot())
        self.assertEqual(metadata['selected'], {'AI': 'US-1'})
        self.assertEqual(count, 4)

    def test_snapshot_accepts_totp_file_and_rendered_secrets_have_restricted_permissions(self):
        _, count = backup.inspect_snapshot(snapshot({'config/secrets/vpn-token': b'test-token'}))
        self.assertEqual(count, 5)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            installer.write_vpn_secrets(root, {'vpn_token': 'test-token'})
            token = root / 'config/secrets/vpn-token'
            self.assertEqual(token.read_text(), 'test-token\n')
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)
            self.assertEqual(token.parent.stat().st_mode & 0o777, 0o700)
            self.assertFalse((token.parent / 'vpn-password').exists())

    def test_source_archive_excludes_operator_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            required = ('compose.yaml', 'versions.json', 'image/Dockerfile', 'image/.dockerignore',
                        'image/mihomo', 'artifacts/dashboard.tgz', 'tools/guest-install.py', 'tools/ssh_access.py', 'image/runtime.py')
            for name in required:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'public input')
            for name in ('config/gateway.yaml', 'config/secrets/vpn-token', '.env', 'data/subscription/current.yaml', 'access/id_ed25519',
                         'deployment.json', 'tools/authorized_keys'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'PRIVATE SENTINEL')
            with patch.object(deploy, 'ROOT', root):
                body = deploy.source_archive()
            with tarfile.open(fileobj=io.BytesIO(body)) as archive:
                self.assertEqual({entry.name for entry in archive.getmembers()}, set(required))
                self.assertTrue(all(b'PRIVATE SENTINEL' not in archive.extractfile(entry).read()
                                    for entry in archive.getmembers()))

    def test_materialize_uses_shared_profile_and_does_not_mutate_operator_config(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = pathlib.Path(directory) / 'profiles.json'
            profile.write_text(json.dumps({'vpn2': {'server': 'vpn.example.invalid', 'username': 'operator',
                                                   'credential': 'existing-vpn'}}))
            config = {'runtime': {'vpn': {'attempts': 1}, 'subscription': {}, 'mihomo': {}, 'network': {'interface': 'eth0'}},
                      'network': {'address': '192.168.50.201/24'},
                      'vpn_profiles_file': str(profile), 'vpn_profile': 'vpn2',
                      'secrets': {'subscription_url': 'sub', 'controller': 'panel'}}
            with patch.object(deploy, 'secret', side_effect=lambda entry: 'resolved-' + entry):
                bundle = deploy.materialize(config)
            self.assertEqual(bundle['vpn_password'], 'resolved-existing-vpn')
            self.assertEqual(bundle['runtime']['vpn']['server'], 'vpn.example.invalid')
            self.assertEqual(bundle['runtime']['vpn']['password_file'], '/config/secrets/vpn-password')
            self.assertEqual(config['runtime']['subscription'], {})
            self.assertEqual(bundle['runtime']['network']['lan']['address'], '192.168.50.201/24')
            self.assertEqual(config['runtime']['network'], {'interface': 'eth0'})

    def test_guest_rejects_symlink_in_source_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / 'source.tgz'
            with tarfile.open(archive, 'w:gz') as package:
                entry = tarfile.TarInfo('image/runtime.py')
                entry.type = tarfile.SYMTYPE
                entry.linkname = '/etc/shadow'
                package.addfile(entry)
            with self.assertRaises(ValueError):
                installer.safe_extract(archive, root / 'stage')

    def test_dashboard_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / 'ui.tgz'
            with tarfile.open(archive, 'w:gz') as package:
                entry = tarfile.TarInfo('../outside')
                entry.size = 1
                package.addfile(entry, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                installer.install_ui(archive, root / 'ui')
            self.assertFalse((root / 'outside').exists())

    def test_preflight_never_overwrites_a_boot_image_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory)
            (project / 'data/subscription').mkdir(parents=True)
            (project / 'data/subscription/current.yaml').write_text('proxies: []')
            image = 'home-gateway:test-release'
            versions = {'version': 'test', 'image': image, 'base_image': 'base-digest',
                        'mihomo': {'binary_sha256': hashlib.sha256(b'binary').hexdigest()},
                        'dashboard': {'sha256': hashlib.sha256(b'ui').hexdigest()}}
            bundle = {'version': 'test', 'image': image, 'runtime': {
                'network': {'interface': 'eth0', 'clients': ['192.168.50.0/24']},
                'clash': {'enabled': True}, 'vpn': {'enabled': False}}, 'vpn_password': 'fake-test-value'}

            def unpack(_, stage):
                for name, body in {'versions.json': json.dumps(versions).encode(),
                                   'image/mihomo': b'binary', 'artifacts/dashboard.tgz': b'ui'}.items():
                    path = stage / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(body)

            commands = []

            def execute(command, **_):
                commands.append(command)
                return b'sha256:test-candidate\n' if command[:3] == ['docker', 'image', 'inspect'] else b''

            with patch.object(installer, 'PROJECT', project), patch.object(installer, 'safe_extract', side_effect=unpack), \
                 patch.object(installer, 'image_recipe', return_value='test-recipe'), \
                 patch.object(installer, 'run', side_effect=execute), patch.object(installer, 'install_ui'), \
                 patch('firewall.render_base', return_value='validated'), \
                 patch.object(installer.importlib, 'import_module', return_value=types.SimpleNamespace(render_mihomo=lambda *_: {})), \
                 patch.object(sys, 'argv', ['installer', '--source', 'fixture.tgz', '--validate-only']), \
                 patch.object(sys, 'stdin', io.StringIO(json.dumps(bundle))), patch.object(sys, 'stdout', io.StringIO()):
                installer.main()
            build = next(command for command in commands if command[:2] == ['docker', 'build'])
            candidate = build[build.index('-t') + 1]
            self.assertTrue(candidate.startswith('home-gateway:build-'))
            self.assertNotIn(image, [arg for command in commands for arg in command])
            self.assertFalse(any(command[0] == 'systemctl' or command[:2] == ['docker', 'tag'] for command in commands))
            self.assertFalse((project / '.env').exists())


if __name__ == '__main__':
    unittest.main()
