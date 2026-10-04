import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'image'))
import deploy
from vpn_auth import openconnect_command, validate_totp

# Public RFC 6238 test seed; not a deployed credential.
SEED = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'


class VpnAuthTests(unittest.TestCase):
    def configuration(self):
        return {'runtime': {'vpn': {}, 'subscription': {}, 'mihomo': {}, 'network': {}},
                'vpn_profiles_pass': 'vpn/profiles', 'vpn_profile': 'vpn1',
                'secrets': {'subscription_url': 'sub', 'controller': 'panel'}}

    def test_password_and_totp_are_rendered_as_separate_files_without_secrets_in_runtime(self):
        config = self.configuration()
        original = copy.deepcopy(config)
        profiles = {'vpn1': {'server': 'vpn.example.invalid', 'username': 'operator', 'credential': 'password',
                             'token': {'mode': 'totp', 'encoding': 'base32', 'credential': 'totp'}}}
        values = {'vpn/profiles': json.dumps(profiles), 'password': 'PASSWORD-VALUE', 'totp': SEED,
                  'sub': 'SUB-VALUE', 'panel': 'PANEL-VALUE'}
        with patch.object(deploy, 'secret', side_effect=values.__getitem__):
            bundle = deploy.materialize(config)
        self.assertEqual(bundle['vpn_password'], values['password'])
        self.assertEqual(bundle['vpn_token'], 'base32:' + SEED)
        self.assertEqual(bundle['runtime']['vpn']['token_file'], '/config/secrets/vpn-token')
        self.assertNotIn(SEED, json.dumps(bundle['runtime']))
        self.assertNotIn('PASSWORD-VALUE', json.dumps(bundle['runtime']))
        self.assertEqual(config, original)

    def test_inactive_token_is_not_decrypted_and_password_profile_clears_stale_token_settings(self):
        config = self.configuration()
        config['vpn_profile'] = 'vpn2'
        config['runtime']['vpn'] = {'token_mode': 'totp', 'token_file': '/OLD-FILE'}
        profiles = {'vpn1': {'token': {'mode': 'totp', 'credential': 'MISSING-TOKEN'}},
                    'vpn2': {'server': 'vpn.example.invalid', 'username': 'operator', 'credential': 'password'}}
        values = {'vpn/profiles': json.dumps(profiles), 'password': 'PASSWORD', 'sub': 'SUB', 'panel': 'PANEL'}
        with patch.object(deploy, 'secret', side_effect=values.__getitem__) as read:
            bundle = deploy.materialize(config)
        self.assertNotIn('vpn_token', bundle)
        self.assertNotIn('token_mode', bundle['runtime']['vpn'])
        self.assertNotIn('MISSING-TOKEN', [call.args[0] for call in read.call_args_list])

    def test_token_only_profile_has_no_password_file(self):
        config = self.configuration()
        config['runtime']['vpn']['password_file'] = '/OLD-PASSWORD'
        profiles = {'vpn1': {'server': 'vpn.example.invalid', 'username': 'operator',
                             'token': {'mode': 'totp', 'credential': 'totp'}}}
        values = {'vpn/profiles': json.dumps(profiles), 'totp': 'base32:' + SEED, 'sub': 'SUB', 'panel': 'PANEL'}
        with patch.object(deploy, 'secret', side_effect=values.__getitem__):
            bundle = deploy.materialize(config)
        self.assertNotIn('vpn_password', bundle)
        self.assertNotIn('password_file', bundle['runtime']['vpn'])

    def test_invalid_seed_and_unsupported_modes_fail_without_echoing_credentials(self):
        for value in ('PRIVATE_INVALID_SEED!', 'base32:abc\nPRIVATE', 'base32:A', 'ABC'):
            with self.subTest(value=value), self.assertRaises(ValueError) as error:
                validate_totp(value)
            self.assertNotIn(value, str(error.exception))
        with self.assertRaises(ValueError):
            validate_totp(SEED, 'hotp')

    def test_openconnect_receives_password_on_stdin_and_token_as_a_file_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'password').write_text('PASSWORD-VALUE')
            (root / 'token').write_text('base32:' + SEED)
            vpn = {'server': 'vpn.example.invalid', 'username': 'operator', 'interface': 'vpn0',
                   'password_file': 'password', 'token_mode': 'totp', 'token_file': 'token'}
            command, password = openconnect_command(vpn, root)
            self.assertEqual(password, root / 'password')
            self.assertIn('--passwd-on-stdin', command)
            self.assertIn('--token-mode=totp', command)
            self.assertIn('--token-secret=@' + str(root / 'token'), command)
            self.assertFalse(any(SEED in value or 'PASSWORD-VALUE' in value for value in command))
            self.assertIn('--non-inter', command)
            self.assertIn('--reconnect-timeout=30', command)
            (root / 'token').unlink()
            with self.assertRaises(ValueError):
                openconnect_command(vpn, root)

    def test_hexadecimal_seed_remains_compatible(self):
        self.assertEqual(validate_totp('3132333435363738393031323334353637383930'),
                         '3132333435363738393031323334353637383930')


if __name__ == '__main__':
    unittest.main()
