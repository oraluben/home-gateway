"""Explicit appliance paths; dotfiles and credential stores are deployment-only."""
import os
import pathlib
import json
import tempfile
import yaml
from topology import component_enabled

DATA = pathlib.Path(os.environ.get('HOME_GATEWAY_DATA', '/data'))
CONFIG = pathlib.Path(os.environ.get('HOME_GATEWAY_CONFIG', '/config/gateway.yaml'))


def load_config():
    value = yaml.safe_load(CONFIG.read_text())
    if not isinstance(value, dict):
        raise ValueError('Gateway configuration must be a mapping')
    return value


def password_path(config):
    value = pathlib.Path(config['vpn']['password_file'])
    return value if value.is_absolute() else CONFIG.parent / value


def read_policy(data=None, *, config=None):
    if config is not None and not component_enabled(config, 'vpn'):
        return {'up': False, 'prefixes': [], 'dns': [], 'domains': []}
    try:
        value = json.loads(((data or DATA) / 'vpn-policy.json').read_text())
    except FileNotFoundError:
        return {'up': False, 'prefixes': [], 'dns': [], 'domains': []}
    if not isinstance(value, dict):
        raise ValueError('VPN policy must be a mapping')
    return value


def effective_policy(config, policy):
    """Disabled VPN has no routes; disconnected VPN retains its protection."""
    if not component_enabled(config, 'vpn'):
        return {'up': False, 'prefixes': [], 'dns': [], 'domains': []}
    return dict(policy)


def save_json(path, value):
    # VPN hooks and the supervisor may publish concurrently. Each writer needs
    # its own temporary file; readers see an entire old or new document.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.' + path.name + '-', delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write('\n')
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
