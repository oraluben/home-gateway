"""OpenConnect authentication inputs. Secret values never enter argv."""
import base64
import binascii
import pathlib
import re


def validate_totp(value, encoding=None):
    value = value.strip()
    if encoding not in (None, 'base32'):
        raise ValueError('Unsupported TOTP seed encoding')
    if encoding == 'base32' and not value.startswith('base32:'):
        value = 'base32:' + value
    try:
        if value.startswith('base32:'):
            seed = value[7:].rstrip('=').upper()
            if not re.fullmatch('[A-Z2-7]+', seed):
                raise ValueError()
            base64.b32decode(seed + '=' * (-len(seed) % 8))
            return 'base32:' + seed
        if not re.fullmatch('[0-9a-fA-F]+', value):
            raise ValueError()
        bytes.fromhex(value)
        return value.upper()
    except (ValueError, binascii.Error):
        raise ValueError('Invalid TOTP seed; use base32: followed by a Base32 seed, or hexadecimal data') from None


def openconnect_command(vpn, config_directory):
    def file_path(name):
        path = pathlib.Path(vpn[name])
        path = path if path.is_absolute() else pathlib.Path(config_directory) / path
        if not path.is_file():
            raise ValueError('VPN credential file is missing')
        return path

    command = ['openconnect', '--non-inter',
               '--reconnect-timeout=' + str(vpn.get('reconnect_timeout', 30)),
               '--interface=' + vpn['interface'], '--user=' + vpn['username'],
               '--script=/app/vpn-hook.py']
    password = file_path('password_file') if vpn.get('password_file') else None
    if password:
        command.append('--passwd-on-stdin')
    if vpn.get('token_mode') or vpn.get('token_file'):
        if vpn.get('token_mode') != 'totp' or not vpn.get('token_file'):
            raise ValueError('Only TOTP token files are supported')
        token = file_path('token_file')
        validate_totp(token.read_text())
        command += ['--token-mode=totp', '--token-secret=@' + str(token)]
    elif not password:
        raise ValueError('VPN requires a password or a TOTP token')
    command.append(vpn['server'])
    return command, password
