import hashlib
import importlib.util
import io
import json
import pathlib
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import deploy
from mihomo_build import configure_runtime, selected_build


class MihomoBuildTests(unittest.TestCase):
    def setUp(self):
        self.folder = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        binary = b'\x7fELF\x02\x01' + bytes(12) + b'\x3e\x00' + b'public-binary'
        (self.folder / 'mihomo').write_bytes(binary)
        (self.folder / 'LICENSE').write_text('GPL-3.0')
        (self.folder / 'source.tar.gz').write_bytes(b'public-source')
        self.value = {'format': 1, 'version': 'custom-test', 'goos': 'linux', 'goarch': 'amd64',
                      'binary': 'mihomo', 'binary_sha256': hashlib.sha256(binary).hexdigest(),
                      'features': ['connection-history-v1'], 'license': 'LICENSE',
                      'license_sha256': hashlib.sha256(b'GPL-3.0').hexdigest(),
                      'source': {'repository': 'https://github.com/example/mihomo', 'revision': 'a' * 40,
                                 'archive': 'source.tar.gz', 'sha256': hashlib.sha256(b'public-source').hexdigest()}}
        self.path = self.folder / 'build.json'
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.value))

    def test_hash_architecture_and_source_are_verified(self):
        result = selected_build(self.path)
        self.assertEqual(result['metadata']['version'], 'custom-test')
        self.assertNotIn(str(self.folder), json.dumps(result['metadata']))
        (self.folder / 'mihomo').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            selected_build(self.path)

    def test_source_checksum_and_path_escape_are_rejected(self):
        (self.folder / 'source.tar.gz').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            selected_build(self.path)
        self.value['binary'] = '../outside'
        self.write()
        with self.assertRaises(ValueError):
            selected_build(self.path)

    def test_invalid_platform_revision_and_credential_urls_are_rejected(self):
        for key, value in [('goarch', 'arm64'), ('version', 'unsafe;command')]:
            previous = self.value[key]
            self.value[key] = value
            self.write()
            with self.assertRaises(ValueError):
                selected_build(self.path)
            self.value[key] = previous
        self.value['source']['repository'] = 'https://user:private@example.invalid/repo'
        self.write()
        with self.assertRaises(ValueError):
            selected_build(self.path)

    def test_history_is_enabled_only_for_the_explicit_feature(self):
        runtime = {'mihomo': {}}
        configure_runtime(runtime, None)
        self.assertEqual(runtime, {'mihomo': {}})
        configure_runtime(runtime, selected_build(self.path))
        self.assertEqual(runtime['mihomo']['connection-history'], {'capacity': 4096, 'log': False})
        self.assertTrue(runtime['usage']['require_history'])

    def test_arm64_artifact_is_verified_without_claiming_host_installation_support(self):
        binary = b'\x7fELF\x02\x01' + bytes(12) + b'\xb7\x00' + b'public-arm64-binary'
        (self.folder / 'mihomo').write_bytes(binary)
        self.value.update(goarch='arm64', binary_sha256=hashlib.sha256(binary).hexdigest())
        self.write()
        self.assertEqual(selected_build(self.path, expected_arch='aarch64')['metadata']['goarch'], 'arm64')
        with self.assertRaisesRegex(ValueError, 'differs'):
            selected_build(self.path, expected_arch='amd64')

    def test_archive_pins_selected_binary_and_includes_corresponding_sources(self):
        build = selected_build(self.path)
        project = self.folder / 'project'
        for name, data in {'compose.yaml': b'', 'versions.json': b'{"version":"test","mihomo":{"version":"stock"}}',
                           'image/Dockerfile': b'', 'image/.dockerignore': b'', 'image/mihomo': b'stock-binary',
                           'image/licenses/mihomo-LICENSE': b'stock-license', 'artifacts/dashboard.tgz': b'ui',
                           'tools/guest-install.py': b'', 'tools/ssh_access.py': b''}.items():
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        with patch.object(deploy, 'ROOT', project):
            body = deploy.source_archive(build)
        with tarfile.open(fileobj=io.BytesIO(body)) as archive:
            names = archive.getnames()
            self.assertEqual(names.count('image/mihomo'), 1)
            self.assertEqual(names.count('image/licenses/mihomo-LICENSE'), 1)
            self.assertEqual(archive.extractfile('image/mihomo').read(), build['binary'].read_bytes())
            self.assertEqual(archive.extractfile('image/licenses/mihomo-SOURCE.tar.gz').read(), b'public-source')
            versions = json.load(archive.extractfile('versions.json'))
            self.assertEqual(versions['mihomo']['binary_sha256'], build['metadata']['binary_sha256'])
            self.assertEqual(versions['mihomo']['source']['revision'], 'a' * 40)


if __name__ == '__main__':
    unittest.main()
