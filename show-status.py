import json
import pathlib
import subprocess

DATA = pathlib.Path('/opt/home-gateway/data')


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return result.stdout.strip() or result.stderr.strip()


def read(name):
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


print(json.dumps({
    'addresses': json.loads(command(['ip', '-j', '-4', 'address', 'show', 'dev', 'eth0'])),
    'services': {name: command(['systemctl', 'is-active', name]) for name in
                 ('home-gateway-base', 'home-gateway-dns', 'home-gateway', 'docker', 'systemd-resolved')},
    'controller': read('status.json'),
    'dns_policy': read('dns-status.json'),
    'container': command(['docker', 'inspect', 'home-gateway', '--format', '{{.State.Status}}']),
    'subscription': read('subscription-status.json'),
    'subscription_next_update': command(['systemctl', 'list-timers', 'home-gateway-subscription.timer',
                                         '--no-pager', '--no-legend']),
}, indent=2))
