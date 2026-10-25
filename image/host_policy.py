"""Host-owned forwarding and DNS reconciliation, outside the business container."""
import ipaddress
import json
import pathlib
import time
from firewall import corporate_networks, render_base
from network import apply_base, proxy_off, run
from runtime import DATA, effective_policy, load_config, read_policy, save_json
from topology import Topology, component_enabled


def initialize(config):
    topology = Topology.parse(config['network'])
    # Validate before cleanup, including rejection of unsupported router mode.
    policy = read_policy(config=config)
    render_base(config, policy)
    proxy_off()
    apply_base(config, policy)
    addresses = json.loads(run(['ip', '-j', '-4', 'address', 'show', 'dev', topology.lan.interface]).stdout)
    expected = str(ipaddress.IPv4Interface(topology.lan.address).ip) if topology.lan.address else None
    address = next(a['local'] for entry in addresses for a in entry['addr_info']
                   if a['scope'] == 'global' and (not expected or a['local'] == expected))
    resolved = pathlib.Path('/etc/systemd/resolved.conf.d/90-home-gateway.conf')
    resolved.parent.mkdir(exist_ok=True)
    resolved.write_text('[Resolve]\nDNSStubListenerExtra=' + address + '\nLLMNR=no\nMulticastDNS=no\n')
    run(['systemctl', 'restart', 'systemd-resolved'])


class HostPolicy:
    def __init__(self, config):
        self.config = config
        self.interface = config.get('vpn', {}).get('interface')
        self.base_key = self.dns_key = None

    def reconcile(self, policy):
        policy = effective_policy(self.config, policy)
        base_key = tuple(corporate_networks(self.config, policy))
        if base_key != self.base_key:
            apply_base(self.config, policy)
            self.base_key = base_key
        exists = bool(self.interface and pathlib.Path('/sys/class/net', self.interface).exists())
        active = bool(component_enabled(self.config, 'vpn') and policy.get('up') and exists and policy.get('dns'))
        dns_key = (active, tuple(policy.get('dns', [])), tuple(policy.get('domains', [])), exists)
        if dns_key != self.dns_key:
            if active:
                run(['resolvectl', 'dns', self.interface] + policy['dns'])
                domains = ['~' + d.lstrip('*.') for d in policy.get('domains', [])] or ['~.']
                run(['resolvectl', 'domain', self.interface] + domains)
                run(['resolvectl', 'default-route', self.interface, 'no'])
            elif exists:
                run(['resolvectl', 'revert', self.interface])
            run(['resolvectl', 'flush-caches'])
            save_json(DATA / 'dns-status.json', {'vpn_dns_active': active, 'error': None,
                                               'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
            self.dns_key = dns_key
            print('Host VPN DNS active: ' + str(active), flush=True)
        return {'state': 'ready', 'base_ready': True, 'vpn_dns_active': active, 'error': None}


def main():
    reconciler = HostPolicy(load_config())
    last_heartbeat = 0
    while True:
        try:
            status = reconciler.reconcile(read_policy(config=reconciler.config))
        except Exception as error:
            save_json(DATA / 'network-status.json', {'state': 'failed', 'error': type(error).__name__,
                                                   'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
            raise
        if time.monotonic() - last_heartbeat >= 5:
            save_json(DATA / 'network-status.json', {**status, 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
            last_heartbeat = time.monotonic()
        time.sleep(1)
