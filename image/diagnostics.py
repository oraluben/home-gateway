#!/usr/bin/env python3
"""Appliance-local diagnostics and component controls; no pass or operator files."""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from network import proxy_routing_status
from runtime import DATA, load_config

ACTIONS = ('status', 'check', 'logs-vpn', 'logs-clash', 'retry-vpn', 'retry-clash', 'stop-vpn', 'stop-clash')


def read_json(name):
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=10)


def status():
    config = load_config()
    controller = read_json('status.json')
    path = DATA / 'status.json'
    age = round(time.time() - path.stat().st_mtime, 1) if path.exists() else None
    routing = proxy_routing_status()
    forwarding = pathlib.Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() == '1'
    source_marks = {interface: int(pathlib.Path('/proc/sys/net/ipv4/conf/' + interface + '/src_valid_mark').read_text())
                    for interface in ('all', config['network']['interface'])}
    source_validation = {'ready': not any(source_marks.values()), 'src_valid_mark': source_marks}
    proxy_rules = command(['nft', 'list', 'table', 'inet', 'home_gateway_proxy']).returncode == 0
    base_rules = command(['nft', 'list', 'table', 'inet', 'home_gateway_base']).returncode == 0
    receipt = read_json('deployment.json') or {}
    addresses = command(['ip', '-j', '-4', 'address', 'show', 'dev', config['network']['interface']])
    subscription = read_json('subscription-status.json') or {}
    # Subscription URLs, configuration, passwords and controller keys are never returned.
    return {'version': receipt.get('version'), 'container': 'running',
            'addresses': json.loads(addresses.stdout) if addresses.returncode == 0 else [],
            'controller': controller, 'status_age_seconds': age,
            'supervisor_status_fresh': age is not None and -5 <= age < 10,
            'proxy_routing': routing, 'proxy_rules_present': proxy_rules,
            'base_rules_present': base_rules, 'forwarding_enabled': forwarding,
            'source_validation': source_validation,
            'dns_policy': read_json('dns-status.json'),
            'subscription': {key: subscription[key] for key in ('state', 'last_attempt', 'last_success', 'error', 'nodes', 'groups', 'rules') if key in subscription}}


def print_status(value):
    controller = value.get('controller') or {}
    print(f'Gateway: {value.get("version") or "unknown version"}; supervisor status: '
          + ('fresh' if value['supervisor_status_fresh'] else 'missing or stale'))
    for name in ('vpn', 'clash'):
        item = controller.get('components', {}).get(name)
        if item is None:
            print(f'{name}: disabled')
            continue
        state = item['state']
        if name == 'vpn' and state == 'running':
            state = 'connected' if controller.get('vpn_connected') else 'connecting'
        delay = item.get('retry_in_seconds')
        waiting = f'; retry in {delay}s' if delay is not None else ''
        print(f'{name}: {state}; attempts {item["attempts"]}/{item["max_attempts"]}{waiting}')
    proxy = bool(controller.get('transparent_proxy') and value['proxy_routing']['ready'] and value['proxy_rules_present']
                 and value['source_validation']['ready'])
    print('Transparent proxy: ' + ('ready' if proxy else 'inactive or incomplete'))
    if not value['source_validation']['ready']:
        print('Source validation: src_valid_mark conflicts with transparent routing; inspect host services')
    print('Base forwarding: ' + ('ready' if value['forwarding_enabled'] and value['base_rules_present'] else 'incomplete'))
    dns = value.get('dns_policy') or {}
    print('Corporate DNS: ' + ('active' if dns.get('vpn_dns_active') else 'inactive')
          + (f'; error: {dns["error"]}' if dns.get('error') else ''))
    subscription = value.get('subscription') or {}
    print(f'Subscription: {subscription.get("state", "unknown")}; last success: {subscription.get("last_success", "unknown")}')


def http_check(label, url, proxy=False):
    args = ['curl', '-4', '--silent', '--show-error', '--connect-timeout', '5', '--max-time', '12',
            '--output', '/dev/null', '--write-out', '%{http_code}', '--noproxy', '' if proxy else '*']
    if proxy:
        args += ['--proxy', 'http://127.0.0.1:7897']
    try:
        result = subprocess.run(args + [url], capture_output=True, text=True, timeout=15)
        code = int(result.stdout) if result.stdout.isdecimal() else 0
        # A protected corporate site may legitimately return 401/403. A TLS HTTP
        # response proves reachability; application login is a separate step.
        reachable = result.returncode == 0 and 100 <= code < 500
        return {'name': label, 'reachable': reachable, 'http_status': code, 'exit': result.returncode}
    except (OSError, subprocess.TimeoutExpired):
        return {'name': label, 'reachable': False, 'http_status': 0, 'error': 'check could not complete'}


def checks(corporate_url=None):
    config = load_config()
    targets = [('direct_web', 'https://www.baidu.com', False),
               ('proxy_web', 'https://www.google.com/generate_204', True)]
    corporate_url = corporate_url or config.get('diagnostics', {}).get('corporate_url')
    if corporate_url:
        targets.append(('corporate_web', corporate_url, False))
    with ThreadPoolExecutor(max_workers=len(targets)) as executor:
        return list(executor.map(lambda target: http_check(*target), targets))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='status', choices=ACTIONS)
    parser.add_argument('--json', action='store_true', help='Machine-readable status or checks')
    parser.add_argument('--lines', type=int, default=60, help='Log lines (1 to 500)')
    parser.add_argument('--corporate-url', help='Optional internal HTTPS address for a reachability check')
    args = parser.parse_args(argv)
    if not 1 <= args.lines <= 500:
        parser.error('--lines must be between 1 and 500')
    if args.action in ('status', 'check'):
        value = status()
        if args.action == 'check':
            value['checks'] = checks(args.corporate_url)
        if args.json:
            print(json.dumps(value, indent=2))
        else:
            print_status(value)
            for check in value.get('checks', []):
                print(f'{check["name"]}: {"reachable" if check["reachable"] else "FAILED"}; HTTP {check["http_status"]}')
            if args.action == 'check' and not any(check['name'] == 'corporate_web' for check in value['checks']):
                print('Corporate reachability: no URL configured; use --corporate-url or diagnostics.corporate_url')
        if not value['supervisor_status_fresh'] or not value['base_rules_present'] or not value['forwarding_enabled']:
            return 1
        controller = value['controller'] or {}
        if 'vpn' in controller.get('components', {}) and not controller.get('vpn_connected'):
            return 1
        dns = value.get('dns_policy') or {}
        if controller.get('vpn_connected') and (not dns.get('vpn_dns_active') or dns.get('error')):
            return 1
        if 'clash' in controller.get('components', {}) and not (controller.get('transparent_proxy') and value['proxy_routing']['ready'] and value['proxy_rules_present'] and value['source_validation']['ready']):
            return 1
        return 0 if all(check['reachable'] for check in value.get('checks', [])) else 1
    if args.action.startswith('logs-'):
        path = DATA / 'logs' / (args.action.removeprefix('logs-') + '.log')
        if not path.exists():
            print('No log file yet')
            return 0
        with path.open(errors='replace') as stream:
            print(''.join(deque(stream, maxlen=args.lines)), end='')
        return 0
    (DATA / args.action).touch(mode=0o600)
    print(args.action + ' requested; use gatewayctl status to inspect the result')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        print('Gateway diagnostic failed; inspect gatewayctl logs-system for service errors.', file=sys.stderr)
        raise SystemExit(1)
