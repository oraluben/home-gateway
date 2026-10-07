#!/usr/bin/python3
import ipaddress
import os
import re
import subprocess
import sys
from network import DATA, read_policy, run, save_json
from runtime import load_config
from tailscale_policy import advertised_prefixes

reason = os.environ.get('reason', '')
interface = os.environ.get('TUNDEV', 'vpn0')
policy = read_policy()
dns = []
for value in os.environ.get('INTERNAL_IP4_DNS', '').split():
    dns.append(str(ipaddress.IPv4Address(value)))
environment = os.environ.copy()
prefixes = []
if reason in ('connect', 'reconnect'):
    for index in range(int(os.environ.get('CISCO_SPLIT_INC', '0'))):
        base = f'CISCO_SPLIT_INC_{index}_'
        address = os.environ[base + 'ADDR']
        mask = os.environ.get(base + 'MASKLEN') or os.environ[base + 'MASK']
        network = ipaddress.IPv4Network(f'{address}/{mask}', strict=False)
        if network.prefixlen == 0:
            raise RuntimeError('Refusing an unexpected full-tunnel route')
        prefixes.append(str(network))
    if not prefixes:
        raise RuntimeError('VPN supplied no IPv4 split routes; refusing an unexpected full tunnel')
    if load_config()['network'].get('tailscale_exit', False):
        advertised_prefixes({'prefixes': prefixes, 'dns': dns})
# DNS is managed through the VM's systemd-resolved, not /etc/resolv.conf.
for key in ('INTERNAL_IP4_DNS', 'INTERNAL_IP6_DNS', 'CISCO_DEF_DOMAIN', 'CISCO_SPLIT_DNS'):
    environment.pop(key, None)
result = subprocess.run(['/bin/sh', '/usr/share/vpnc-scripts/vpnc-script'], env=environment, timeout=25)
if result.returncode:
    sys.exit(result.returncode)
if reason in ('connect', 'reconnect'):
    domains = [d.strip().lstrip('~') for d in re.split(r'[,;\s]+', os.environ.get('CISCO_SPLIT_DNS', '')) if d.strip()]
    for address in dns:
        run(['ip', '-4', 'route', 'replace', address + '/32', 'dev', interface])
    policy = {'up': True, 'prefixes': prefixes, 'dns': dns, 'domains': domains}
    save_json(DATA / 'vpn-policy.json', policy)
    print(f'VPN policy applied: {len(prefixes)} prefixes, {len(dns)} DNS servers, {len(domains)} split-DNS domains', flush=True)
elif reason in ('disconnect', 'attempt-reconnect'):
    for address in policy.get('dns', []):
        run(['ip', '-4', 'route', 'del', address + '/32', 'dev', interface], check=False)
    policy['up'] = False
    save_json(DATA / 'vpn-policy.json', policy)
