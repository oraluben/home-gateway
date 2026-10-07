import copy
import yaml
from network import DATA, controller_listener, tailscale_exit_enabled

SUBSCRIPTION_SECTIONS = ('proxies', 'proxy-groups', 'rules', 'proxy-providers', 'rule-providers')


def render_mihomo(config, subscription=None):
    tailscale_exit_enabled(config)
    mihomo = {}
    if config.get('subscription', {}).get('enabled', False):
        if subscription is None:
            subscription = yaml.safe_load((DATA / 'subscription/current.yaml').read_text())
        if not isinstance(subscription, dict):
            raise ValueError('The subscription is not a Clash configuration')
        for section in ('proxy-groups', 'rules'):
            if not isinstance(subscription.get(section), list) or not subscription[section]:
                raise ValueError('The subscription is missing groups or rules')
        if not subscription.get('proxies') and not subscription.get('proxy-providers'):
            raise ValueError('The subscription has no nodes')
        names = {group['name'] for group in subscription['proxy-groups']}
        if not set(config['subscription'].get('required_groups', [])).issubset(names):
            raise ValueError('The subscription renamed a required proxy group')
        mihomo = {key: copy.deepcopy(subscription[key]) for key in SUBSCRIPTION_SECTIONS if key in subscription}
    mihomo.update(copy.deepcopy(config['mihomo']))
    mihomo['tun'] = {'enable': False}
    mihomo['dns'] = {'enable': False}
    mihomo['ipv6'] = False
    mihomo['allow-lan'] = False
    mihomo['bind-address'] = '127.0.0.1'
    mihomo['mixed-port'] = 7897
    mihomo['tproxy-port'] = 7893
    address, port = controller_listener(config)
    mihomo['external-controller'] = f'{address}:{port}'
    mihomo['log-level'] = 'info'
    mihomo['interface-name'] = config['network']['interface']
    mihomo.setdefault('profile', {})['store-selected'] = True
    mihomo['sniffer'] = {'enable': True, 'parse-pure-ip': True, 'override-destination': True,
                         'sniff': {'HTTP': {'ports': [80, '8080-8880'], 'override-destination': True},
                                   'TLS': {'ports': [443, 8443], 'override-destination': True},
                                   'QUIC': {'ports': [443, 8443], 'override-destination': True}}}
    for key in ('external-controller-pipe', 'external-controller-unix', 'external-controller-tls',
                'redir-port', 'port', 'socks-port', 'listeners'):
        mihomo.pop(key, None)
    return mihomo
