import json
import os
import pathlib
import sys
import time
sys.path.insert(0, str(pathlib.Path(__file__).parent / 'image'))
os.environ['HOME_GATEWAY_DATA'] = str(pathlib.Path(__file__).parent / 'data')
os.environ.setdefault('HOME_GATEWAY_CONFIG', str(pathlib.Path(__file__).parent / 'config/gateway.yaml'))
from network import DATA, read_policy, run, save_json
from runtime import load_config
import yaml

config = load_config()
interface = config['vpn']['interface']
previous = None
while True:
    policy = read_policy()
    exists = pathlib.Path('/sys/class/net', interface).exists()
    active = bool(policy.get('up') and exists and policy.get('dns'))
    key = json.dumps([active, policy.get('dns'), policy.get('domains')], sort_keys=True)
    if key != previous:
        try:
            if active:
                run(['resolvectl', 'dns', interface] + policy['dns'])
                domains = ['~' + d.lstrip('*.') for d in policy.get('domains', [])] or ['~.']
                run(['resolvectl', 'domain', interface] + domains)
                run(['resolvectl', 'default-route', interface, 'no'])
            elif exists:
                run(['resolvectl', 'revert', interface])
            run(['resolvectl', 'flush-caches'])
            save_json(DATA / 'dns-status.json', {'vpn_dns_active': active, 'error': None,
                                               'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
            print('VPN DNS active: ' + str(active), flush=True)
            previous = key
        except Exception as error:
            save_json(DATA / 'dns-status.json', {'vpn_dns_active': False, 'error': str(error),
                                               'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
            raise
    time.sleep(1)
