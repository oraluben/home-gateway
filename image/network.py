"""Linux kernel operations; network intent and rule rendering are side-effect free."""
import json
import subprocess
from firewall import (MARK, TAILSCALE_INTERFACE,
                      tailscale_exit_enabled, render_base, render_proxy)

TABLE = '17001'
RULE_PRIORITY = '17001'


def run(args, check=True, input=None):
    return subprocess.run(args, input=input, text=True, capture_output=True,
                          timeout=25, check=check)


def replace_table(name, definition):
    exists = run(['nft', 'list', 'table', 'inet', name], check=False).returncode == 0
    commands = (f'delete table inet {name}\n' if exists else '') + definition
    try:
        run(['nft', '-f', '-'], input=commands)
    except subprocess.CalledProcessError as error:
        raise RuntimeError('nft rules rejected: ' + error.stderr.strip()) from error


def apply_base(config, policy):
    replace_table('home_gateway_base', render_base(config, policy))


def proxy_off():
    run(['nft', 'delete', 'table', 'inet', 'home_gateway_proxy'], check=False)
    for mark in (MARK, '0x17001'):
        # Also remove the exact owned rule from releases before v0.6.1.
        run(['ip', '-4', 'rule', 'del', 'priority', RULE_PRIORITY, 'fwmark', mark,
             'lookup', TABLE], check=False)
    run(['ip', '-4', 'route', 'flush', 'table', TABLE], check=False)


def proxy_routing_status():
    """Check kernel routing, independently of the proxy's listening socket."""
    rules = json.loads(run(['ip', '-j', '-4', 'rule', 'show']).stdout)
    reserved = [rule for rule in rules if rule.get('priority') == int(RULE_PRIORITY)]

    def number(value):
        return int(str(value), 0) if str(value).startswith('0x') else int(value)

    def matches(rule):
        try:
            return (rule.get('src', 'all') == 'all' and rule.get('dst', 'all') == 'all'
                    and number(rule.get('fwmark', 0)) == number(MARK)
                    and number(rule.get('fwmask', '0xffffffff')) == 0xffffffff
                    and str(rule.get('table')) == TABLE
                    and not any(key in rule for key in ('iif', 'oif', 'uidrange', 'ipproto',
                                                        'sport', 'dport', 'not', 'goto',
                                                        'suppress_prefixlength', 'suppress_ifgroup')))
        except (ValueError, TypeError):
            return False

    rule_present = any(matches(rule) for rule in reserved)
    conflict = any(not matches(rule) for rule in reserved)
    # Query all tables so a missing reserved table produces a valid empty result,
    # while command failures still propagate instead of claiming healthy routing.
    routes = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all']).stdout)
    local_route = any(route.get('type') == 'local' and route.get('dev') == 'lo'
                      and str(route.get('table')) == TABLE
                      and route.get('dst') in ('default', '0.0.0.0/0') for route in routes)
    return {'ready': rule_present and local_route and not conflict,
            'rule_present': rule_present, 'local_route_present': local_route,
            'priority_conflict': conflict}


def ensure_proxy_routing():
    """Restore deleted gateway routes without reloading nftables or reconnecting VPN."""
    status = proxy_routing_status()
    if status['priority_conflict']:
        raise RuntimeError('Routing rule priority 17001 is occupied by another rule')
    if not status['local_route_present']:
        run(['ip', '-4', 'route', 'replace', 'local', '0.0.0.0/0', 'dev', 'lo', 'table', TABLE])
    if not status['rule_present']:
        run(['ip', '-4', 'rule', 'add', 'priority', RULE_PRIORITY, 'fwmark', MARK, 'lookup', TABLE])
    return not status['ready']


def proxy_on(config, policy):
    definition = render_proxy(config, policy)
    ensure_proxy_routing()
    replace_table('home_gateway_proxy', definition)
