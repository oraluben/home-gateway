"""Boot entry point for host-owned forwarding and resolver setup."""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'image'))
os.environ['HOME_GATEWAY_DATA'] = str(ROOT / 'data')
os.environ.setdefault('HOME_GATEWAY_CONFIG', str(ROOT / 'config/gateway.yaml'))
from host_policy import initialize
from runtime import load_config

if __name__ == '__main__':
    initialize(load_config())
