"""Validated network intent, independent of Linux commands and credentials.

Legacy one-arm inputs are translated here once. Consumers use LAN/WAN roles;
support for configuring physical links belongs to the host adapter.
"""
from dataclasses import dataclass
import ipaddress
import re


def interface_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', value):
        raise ValueError('Invalid network interface name')
    return value


def boolean(value, name):
    if not isinstance(value, bool):
        raise ValueError(name + ' must be a boolean')
    return value


def component_enabled(config, name, default=True):
    return boolean(config.get(name, {}).get('enabled', default), name + '.enabled')


def networks(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError('Client networks must be a list')
    return tuple(sorted({str(ipaddress.IPv4Network(value, strict=False)) for value in values}))


@dataclass(frozen=True)
class Link:
    interface: str
    address: str | None = None


@dataclass(frozen=True)
class Uplink(Link):
    method: str = 'static'
    gateway: str | None = None
    dns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Topology:
    mode: str
    lan: Link
    wan: Uplink
    clients: tuple[str, ...]
    tailscale_exit: bool = False

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict):
            raise ValueError('Network configuration must be a mapping')
        mode = value.get('mode', 'one-arm')
        if mode not in ('one-arm', 'router'):
            raise ValueError('Supported network modes: one-arm, router')
        canonical = 'lan' in value or 'wan' in value
        if canonical and any(key in value for key in ('interface', 'address', 'gateway', 'dns')):
            raise ValueError('Do not mix legacy network fields with LAN/WAN roles')
        if canonical:
            lan = value.get('lan', {})
            wan = value.get('wan', {})
            if not isinstance(lan, dict) or not isinstance(wan, dict):
                raise ValueError('LAN and WAN must be mappings')
        else:
            if mode != 'one-arm':
                raise ValueError('Router mode requires explicit LAN and WAN roles')
            lan = {key: value[key] for key in ('interface', 'address') if key in value}
            wan = {key: value[key] for key in ('interface', 'address', 'gateway', 'dns') if key in value}
        lan_interface = interface_name(lan.get('interface'))
        wan_interface = interface_name(wan.get('interface', lan_interface if mode == 'one-arm' else None))
        address = str(ipaddress.IPv4Interface(lan['address'])) if lan.get('address') else None
        wan_address = wan.get('address', address if mode == 'one-arm' else None)
        wan_address = str(ipaddress.IPv4Interface(wan_address)) if wan_address else None
        method = wan.get('method', 'static')
        if method not in ('static', 'dhcp'):
            raise ValueError('WAN method must be static or dhcp')
        gateway = str(ipaddress.IPv4Address(wan['gateway'])) if wan.get('gateway') else None
        dns = wan.get('dns', [])
        if not isinstance(dns, (list, tuple)):
            raise ValueError('WAN DNS must be a list')
        dns = tuple(str(ipaddress.IPv4Address(item)) for item in dns)
        if method == 'dhcp' and (wan_address or gateway or dns):
            raise ValueError('DHCP WAN cannot also specify static address, gateway or DNS')
        if mode == 'one-arm':
            if lan_interface != wan_interface or method != 'static' or address != wan_address:
                raise ValueError('One-arm LAN and WAN must share the same static link')
        else:
            if lan_interface == wan_interface or not address:
                raise ValueError('Router mode requires distinct interfaces and a static LAN address')
            if method == 'static' and (not wan_address or not gateway):
                raise ValueError('Static router WAN requires an address and gateway')
            if wan_address and ipaddress.IPv4Interface(address).network.overlaps(ipaddress.IPv4Interface(wan_address).network):
                raise ValueError('LAN and WAN networks overlap')
        if gateway and wan_address:
            uplink = ipaddress.IPv4Interface(wan_address)
            if ipaddress.IPv4Address(gateway) not in uplink.network or gateway == str(uplink.ip):
                raise ValueError('Upstream gateway must be a different address on the uplink')
        clients = networks(value.get('clients', [str(ipaddress.IPv4Interface(address).network)] if address else []))
        if mode == 'router' and wan_address and any(ipaddress.IPv4Network(item).overlaps(ipaddress.IPv4Interface(wan_address).network) for item in clients):
            raise ValueError('Client networks overlap the WAN')
        return cls(mode, Link(lan_interface, address), Uplink(wan_interface, wan_address, method, gateway, dns),
                   clients, boolean(value.get('tailscale_exit', False), 'network.tailscale_exit'))

    def as_dict(self):
        lan = {'interface': self.lan.interface}
        if self.lan.address:
            lan['address'] = self.lan.address
        wan = {'interface': self.wan.interface, 'method': self.wan.method}
        for key in ('address', 'gateway'):
            if getattr(self.wan, key):
                wan[key] = getattr(self.wan, key)
        if self.wan.dns:
            wan['dns'] = list(self.wan.dns)
        return {'mode': self.mode, 'lan': lan, 'wan': wan, 'clients': list(self.clients),
                'tailscale_exit': self.tailscale_exit}


def deployment_topology(config):
    """One authoritative network block, with an adapter for old split inputs."""
    network = config.get('network', {})
    runtime = config.get('runtime', {}).get('network', {})
    if any(key in network for key in ('mode', 'lan', 'wan')):
        if runtime:
            raise ValueError('Canonical deployment network must not be duplicated in runtime.network')
        return Topology.parse(network)
    legacy = dict(runtime)
    for key in ('address', 'gateway', 'dns'):
        if key in network:
            if key in legacy and legacy[key] != network[key]:
                raise ValueError('Conflicting legacy network.' + key)
            legacy[key] = network[key]
    return Topology.parse(legacy)
