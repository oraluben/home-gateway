"""Derive Tailnet destinations from the existing VPN policy, never credentials."""
import ipaddress


def advertised_prefixes(policy):
    values = list(policy.get('prefixes', [])) + [value + '/32' for value in policy.get('dns', [])]
    networks = [ipaddress.IPv4Network(value, strict=False) for value in values]
    reserved = ipaddress.IPv4Network('100.64.0.0/10')
    if any(network.prefixlen == 0 or network.overlaps(reserved) for network in networks):
        raise ValueError('VPN routes overlap the Tailnet or contain a default route')
    # Keep cached destinations while VPN is down: the base firewall still rejects
    # them instead of sending corporate traffic toward the public uplink.
    return [str(network) for network in ipaddress.collapse_addresses(networks)]


def desired_routes(policy, preferences):
    published = preferences.get('AdvertiseRoutes') or []
    return advertised_prefixes(policy) if '0.0.0.0/0' in published else []
