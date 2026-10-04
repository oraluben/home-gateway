"""Explicit appliance paths; dotfiles and credential stores are deployment-only."""
import os
import pathlib
import yaml

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
