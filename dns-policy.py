"""Compatibility entry point for host forwarding and VPN DNS reconciliation."""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'image'))
os.environ['HOME_GATEWAY_DATA'] = str(ROOT / 'data')
os.environ.setdefault('HOME_GATEWAY_CONFIG', str(ROOT / 'config/gateway.yaml'))
from host_policy import main

if __name__ == '__main__':
    main()
