import ipaddress
import json
import os
import pathlib
import re
import subprocess

from runtime import DATA
MARK = '0x17001'
TABLE = '17001'
RULE_PRIORITY = '17001'


def run(args, check=True, input=None):
    return subprocess.run(args, input=input, text=True, capture_output=True,
                          timeout=25, check=check)


def read_policy():
    try:
        return json.loads((DATA / 'vpn-policy.json').read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {'up': False, 'prefixes': [], 'dns': [], 'domains': []}


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def validated_networks(values):
    return sorted({str(ipaddress.IPv4Network(v, strict=False)) for v in values})


def interface_name(value):
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', value):
        raise ValueError('Invalid network interface name')
    return value


def controller_listener(config):
    host, separator, port = config['mihomo'].get('external-controller', '127.0.0.1:9090').rpartition(':')
    address = ipaddress.IPv4Address(host)
    port = int(port)
    if not separator or not 1 <= port <= 65535 or address.is_unspecified or address.is_multicast:
        raise ValueError('The controller must bind to a specific IPv4 address and valid port')
    secret = config['mihomo'].get('secret')
    if not address.is_loopback and (not isinstance(secret, str) or not secret.strip()):
        raise ValueError('A LAN controller requires an access key')
    return str(address), port


def replace_table(name, definition):
    exists = run(['nft', 'list', 'table', 'inet', name], check=False).returncode == 0
    commands = (f'delete table inet {name}\n' if exists else '') + definition
    try:
        run(['nft', '-f', '-'], input=commands)
    except subprocess.CalledProcessError as error:
        raise RuntimeError('nft rules rejected: ' + error.stderr.strip()) from error


def apply_base(config, policy):
    lan = interface_name(config['network']['interface'])
    vpn = interface_name(config['vpn']['interface'])
    clients = ', '.join(validated_networks(config['network']['clients']))
    corporate = ', '.join(validated_networks(policy.get('prefixes', []) +
                                            [x + '/32' for x in policy.get('dns', [])]))
    corporate_elements = f'elements = {{ {corporate} }};' if corporate else ''
    controller_address, controller_port = controller_listener(config)
    controller_guard = '' if ipaddress.IPv4Address(controller_address).is_loopback else f'''
  chain controller_guard {{
    type filter hook input priority -5; policy accept;
    ip daddr {controller_address} tcp dport {controller_port} iifname "lo" accept
    ip daddr {controller_address} tcp dport {controller_port} iifname "{lan}" ip saddr @clients accept
    ip daddr {controller_address} tcp dport {controller_port} counter reject with tcp reset
  }}'''
    replace_table('home_gateway_base', f'''table inet home_gateway_base {{
  set clients {{ type ipv4_addr; flags interval; auto-merge; elements = {{ {clients} }}; }}
  set corporate {{ type ipv4_addr; flags interval; auto-merge; {corporate_elements} }}
{controller_guard}
  chain forward_guard {{
    type filter hook forward priority -5; policy accept;
    ip saddr @clients ip daddr @corporate oifname != "{vpn}" counter reject with icmp type admin-prohibited
  }}
  chain source_nat {{
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr @clients oifname {{ "{lan}", "{vpn}" }} counter masquerade
  }}
}}\n''')


def proxy_off():
    run(['nft', 'delete', 'table', 'inet', 'home_gateway_proxy'], check=False)
    run(['ip', '-4', 'rule', 'del', 'priority', RULE_PRIORITY, 'fwmark', MARK,
         'lookup', TABLE], check=False)
    run(['ip', '-4', 'route', 'flush', 'table', TABLE], check=False)


def proxy_on(config, policy):
    lan = interface_name(config['network']['interface'])
    clients = ', '.join(validated_networks(config['network']['clients']))
    corporate = ', '.join(validated_networks(policy.get('prefixes', []) +
                                            [x + '/32' for x in policy.get('dns', [])]))
    corporate_elements = f'elements = {{ {corporate} }};' if corporate else ''
    run(['ip', '-4', 'route', 'replace', 'local', '0.0.0.0/0', 'dev', 'lo', 'table', TABLE])
    rules = run(['ip', '-4', 'rule', 'show']).stdout
    if f'{RULE_PRIORITY}:' not in rules:
        run(['ip', '-4', 'rule', 'add', 'priority', RULE_PRIORITY, 'fwmark', MARK, 'lookup', TABLE])
    replace_table('home_gateway_proxy', f'''table inet home_gateway_proxy {{
  set clients {{ type ipv4_addr; flags interval; auto-merge; elements = {{ {clients} }}; }}
  set corporate {{ type ipv4_addr; flags interval; auto-merge; {corporate_elements} }}
  chain divert {{
    type filter hook prerouting priority mangle; policy accept;
    iifname "{lan}" ip saddr @clients jump proxy
  }}
  chain proxy {{
    fib daddr type local return
    ip daddr @corporate return
    ip daddr {{ 0.0.0.0/8, 10.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 }} return
    meta l4proto {{ tcp, udp }} th dport 53 return
    meta l4proto {{ tcp, udp }} counter tproxy ip to 127.0.0.1:7893 meta mark set {MARK} accept
  }}
}}\n''')
