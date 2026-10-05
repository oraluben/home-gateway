import json
import pathlib
import subprocess
import sys
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / 'image'))
from network import proxy_routing_status

DATA = pathlib.Path('/opt/home-gateway/data')
CONFIG = pathlib.Path('/opt/home-gateway/config/gateway.yaml')


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return result.stdout.strip() or result.stderr.strip()


def read(name):
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


interface = yaml.safe_load(CONFIG.read_text())['network']['interface']
print(json.dumps({
    'addresses': json.loads(command(['ip', '-j', '-4', 'address', 'show', 'dev', interface])),
    'services': {name: command(['systemctl', 'is-active', name]) for name in
                 ('home-gateway-base', 'home-gateway-dns', 'home-gateway', 'docker', 'systemd-resolved')},
    'controller': read('status.json'),
    'proxy_routing': proxy_routing_status(),
    'dns_policy': read('dns-status.json'),
    'container': command(['docker', 'inspect', 'home-gateway', '--format', '{{.State.Status}}']),
    'subscription': read('subscription-status.json'),
    'subscription_next_update': command(['systemctl', 'list-timers', 'home-gateway-subscription.timer',
                                         '--no-pager', '--no-legend']),
}, indent=2))
