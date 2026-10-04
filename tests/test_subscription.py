import copy
import importlib.util
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse
import yaml

root = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('updater', root / 'update-subscription.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = pathlib.Path(self.temporary.name)
        (self.directory / 'subscription').mkdir()
        (self.directory / 'mihomo').mkdir()
        self.cache = self.directory / 'subscription/current.yaml'
        self.runtime = self.directory / 'mihomo/generated.yaml'
        self.cache.write_bytes(b'previous-cache')
        self.runtime.write_bytes(b'previous-runtime')
        self.config = {'network': {'interface': 'eth0'},
                       'mihomo': {'external-controller': '192.168.1.201:9090', 'secret': 'test-key'},
                       'subscription': {'enabled': True, 'url': 'https://example.invalid/subscription',
                                        'required_groups': ['AI', 'Main'],
                                        'fallback_filters': {'AI': 'US', 'Main': 'HK'}}}
        self.subscription = {'proxies': [{'name': n, 'type': 'http', 'server': '127.0.0.1', 'port': 1234}
                                        for n in ['US-1', 'US-2', 'HK-1', 'HK-2']],
                             'proxy-groups': [{'name': n, 'type': 'select',
                                               'proxies': ['US-1', 'US-2', 'HK-1', 'HK-2']} for n in ['AI', 'Main']],
                             'rules': ['MATCH,Main'], 'external-controller': '0.0.0.0:1', 'secret': ''}
        self.selected = {'AI': 'US-2', 'Main': 'HK-2'}
        self.options = ['US-1', 'US-2', 'HK-1', 'HK-2']
        self.reloads = 0
        self.fail_first_reload = False

    def tearDown(self):
        self.temporary.cleanup()

    def api(self, config, path, method='GET', payload=None):
        if path == '/configs':
            return {'mode': 'rule'}
        if path == '/proxies':
            return {'proxies': {name: {'type': 'Selector', 'now': selected, 'all': self.options}
                                for name, selected in self.selected.items()}}
        if path == '/configs?force=true':
            self.reloads += 1
            if self.fail_first_reload and self.reloads == 1:
                raise ConnectionError('simulated reload failure')
            self.selected = {'AI': self.options[-1], 'Main': self.options[0]}
        elif path.startswith('/proxies/'):
            self.selected[urllib.parse.unquote(path.rsplit('/', 1)[1])] = payload['name']

    def refresh(self, body=None, valid=True):
        body = body if body is not None else yaml.safe_dump(self.subscription).encode()
        with patch.object(updater, 'DATA', self.directory), patch.object(updater, 'download', return_value=body), \
             patch.object(updater, 'api', side_effect=self.api), \
             patch.object(updater.subprocess, 'run') as validation:
            validation.return_value.returncode = 0 if valid else 1
            return updater.refresh(self.config)

    def test_success_preserves_selection_and_gateway_settings(self):
        result = self.refresh()
        self.assertTrue(result['changed'])
        self.assertEqual(self.selected, {'AI': 'US-2', 'Main': 'HK-2'})
        runtime = yaml.safe_load(self.runtime.read_bytes())
        self.assertEqual(runtime['external-controller'], '192.168.1.201:9090')
        self.assertEqual(runtime['secret'], 'test-key')
        self.assertEqual(runtime['tun'], {'enable': False})
        self.assertEqual((self.directory / 'subscription/previous.yaml').read_bytes(), b'previous-cache')

    def test_invalid_config_never_changes_working_files(self):
        with self.assertRaises(ValueError):
            self.refresh(b'proxies: []\n')
        self.assertEqual(self.cache.read_bytes(), b'previous-cache')
        self.assertEqual(self.runtime.read_bytes(), b'previous-runtime')
        self.assertEqual(self.reloads, 0)

    def test_mihomo_rejection_keeps_working_configuration(self):
        with self.assertRaises(ValueError):
            self.refresh(valid=False)
        self.assertEqual(self.cache.read_bytes(), b'previous-cache')
        self.assertEqual(self.runtime.read_bytes(), b'previous-runtime')
        self.assertEqual(self.reloads, 0)

    def test_failed_reload_restores_cache_runtime_and_choices(self):
        self.fail_first_reload = True
        with self.assertRaises(RuntimeError):
            self.refresh()
        self.assertEqual(self.cache.read_bytes(), b'previous-cache')
        self.assertEqual(self.runtime.read_bytes(), b'previous-runtime')
        self.assertEqual(self.selected, {'AI': 'US-2', 'Main': 'HK-2'})
        self.assertEqual(self.reloads, 2)

    def test_unchanged_subscription_does_not_reload(self):
        body = yaml.safe_dump(self.subscription).encode()
        self.cache.write_bytes(body)
        self.assertFalse(self.refresh(body)['changed'])
        self.assertEqual(self.reloads, 0)

    def test_removed_nodes_fall_back_to_the_required_region(self):
        self.options = ['US-3', 'HK-3']
        self.refresh()
        self.assertEqual(self.selected, {'AI': 'US-3', 'Main': 'HK-3'})

    def test_renamed_required_group_is_rejected(self):
        self.subscription['proxy-groups'][0]['name'] = 'Renamed'
        with self.assertRaises(ValueError):
            self.refresh()
        self.assertEqual(self.reloads, 0)
        self.assertEqual(self.cache.read_bytes(), b'previous-cache')


if __name__ == '__main__':
    unittest.main()
