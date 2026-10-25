"""Pure nftables rendering. Kernel inspection/application lives in network.py."""
import ipaddress
from topology import Topology, component_enabled, interface_name
from runtime import effective_policy

MARK = '0x7001'
TAILSCALE_INTERFACE = 'tailscale0'
TAILSCALE_CLIENTS = '100.64.0.0/10'


def tailscale_exit_enabled(config):
    return Topology.parse(config['network']).tailscale_exit


def controller_listener(config):
    host, separator, port = config.get('mihomo', {}).get('external-controller', '127.0.0.1:9090').rpartition(':')
    address = ipaddress.IPv4Address(host)
    port = int(port)
    if not separator or not 1 <= port <= 65535 or address.is_unspecified or address.is_multicast:
        raise ValueError('The controller must bind to a specific IPv4 address and valid port')
    secret = config.get('mihomo', {}).get('secret')
    if not address.is_loopback and (not isinstance(secret, str) or not secret.strip()):
        raise ValueError('A LAN controller requires an access key')
    return str(address), port


def corporate_networks(config, policy):
    policy = effective_policy(config, policy)
    return sorted({str(ipaddress.IPv4Network(value, strict=False)) for value in
                   list(policy.get('prefixes', [])) + [value + '/32' for value in policy.get('dns', [])]})


def render_base(config, policy):
    topology = Topology.parse(config['network'])
    # Parsing a router topology is not a claim that DHCP and a WAN firewall
    # have been implemented. Never apply the permissive one-arm policy to it.
    if topology.mode != 'one-arm':
        raise ValueError('Router host networking/firewall is not implemented yet')
    lan, wan = topology.lan.interface, topology.wan.interface
    vpn = config.get('vpn', {}).get('interface') if component_enabled(config, 'vpn') else None
    if vpn:
        vpn = interface_name(vpn)
    corporate = ', '.join(corporate_networks(config, policy))
    if corporate and not vpn:
        raise ValueError('Enabled VPN protection requires a tunnel interface')
    clients = ', '.join(topology.clients)
    if not clients:
        raise ValueError('Base forwarding requires client networks')
    corporate_elements = f'elements = {{ {corporate} }};' if corporate else ''
    egress = ', '.join('"' + value + '"' for value in dict.fromkeys([wan] + ([vpn] if vpn else [])))
    guard = (f'    ip saddr @clients ip daddr @corporate oifname != "{vpn}" counter reject with icmp type admin-prohibited\n'
             if vpn else '')
    remote_guard = remote_nat = ipv6_guard = ''
    if topology.tailscale_exit:
        if vpn:
            remote_guard = f'    iifname "{TAILSCALE_INTERFACE}" ip saddr {TAILSCALE_CLIENTS} ip daddr @corporate oifname != "{vpn}" counter reject with icmp type admin-prohibited\n'
        remote_nat = f'    iifname "{TAILSCALE_INTERFACE}" ip saddr {TAILSCALE_CLIENTS} oifname {{ {egress} }} counter masquerade\n'
        ipv6_guard = f'    iifname "{TAILSCALE_INTERFACE}" meta nfproto ipv6 counter reject with icmpv6 type no-route\n'
    controller_guard = ''
    if component_enabled(config, 'clash'):
        controller_address, controller_port = controller_listener(config)
        if not ipaddress.IPv4Address(controller_address).is_loopback:
            controller_guard = f'''
  chain controller_guard {{
    type filter hook input priority -5; policy accept;
    ip daddr {controller_address} tcp dport {controller_port} iifname "lo" accept
    ip daddr {controller_address} tcp dport {controller_port} iifname "{lan}" ip saddr @clients accept
    ip daddr {controller_address} tcp dport {controller_port} counter reject with tcp reset
  }}'''
    return f'''table inet home_gateway_base {{
  set clients {{ type ipv4_addr; flags interval; auto-merge; elements = {{ {clients} }}; }}
  set corporate {{ type ipv4_addr; flags interval; auto-merge; {corporate_elements} }}
{controller_guard}
  chain forward_guard {{
    type filter hook forward priority -5; policy accept;
{guard}{remote_guard}{ipv6_guard}\
  }}
  chain source_nat {{
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr @clients oifname {{ {egress} }} counter masquerade
{remote_nat}\
  }}
}}\n'''


def render_proxy(config, policy):
    topology = Topology.parse(config['network'])
    clients = ', '.join(topology.clients)
    if not clients:
        raise ValueError('Transparent proxy requires client networks')
    corporate = ', '.join(corporate_networks(config, policy))
    corporate_elements = f'elements = {{ {corporate} }};' if corporate else ''
    remote_divert = (f'    iifname "{TAILSCALE_INTERFACE}" ip saddr {TAILSCALE_CLIENTS} counter jump proxy\n'
                     if topology.tailscale_exit else '')
    return f'''table inet home_gateway_proxy {{
  set clients {{ type ipv4_addr; flags interval; auto-merge; elements = {{ {clients} }}; }}
  set corporate {{ type ipv4_addr; flags interval; auto-merge; {corporate_elements} }}
  chain divert {{
    type filter hook prerouting priority -140; policy accept;
    iifname "{topology.lan.interface}" ip saddr @clients jump proxy
{remote_divert}\
  }}
  chain proxy {{
    fib daddr type local return
    ip daddr @corporate return
    ip daddr {{ 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 }} return
    meta l4proto {{ tcp, udp }} th dport 53 return
    meta l4proto {{ tcp, udp }} counter tproxy ip to 127.0.0.1:7893 meta mark set {MARK} accept
  }}
}}\n'''
