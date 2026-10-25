"""Deployment projection. Credential access is supplied by the operator edge."""
import copy
import pathlib
from common import json_object, deployment_topology
from topology import component_enabled
from vpn_auth import validate_totp


def render_bundle(config, resolve_secret):
    runtime = copy.deepcopy(config['runtime'])
    runtime['network'] = deployment_topology(config).as_dict()
    enabled = {name: component_enabled(runtime, name, default) for name, default in
               (('vpn', True), ('clash', True), ('subscription', False))}
    bundle = {'runtime': runtime}
    vpn = runtime.setdefault('vpn', {})
    for key in ('password_file', 'token_mode', 'token_file'):
        vpn.pop(key, None)
    if enabled['vpn']:
        profiles = (json_object(resolve_secret(config['vpn_profiles_pass'])) if config.get('vpn_profiles_pass')
                    else json_object(pathlib.Path(config['vpn_profiles_file']).expanduser().read_text()))
        if 'profiles_pass' in profiles:
            profiles = json_object(resolve_secret(profiles['profiles_pass']))
        profile = profiles[config['vpn_profile']]
        vpn.update(server=profile['server'], username=profile['username'])
        if profile.get('credential'):
            vpn['password_file'] = '/config/secrets/vpn-password'
            bundle['vpn_password'] = resolve_secret(profile['credential'])
        if profile.get('token'):
            if profile['token'].get('mode') != 'totp':
                raise ValueError('Only TOTP token profiles are supported')
            vpn.update(token_mode='totp', token_file='/config/secrets/vpn-token')
            bundle['vpn_token'] = validate_totp(resolve_secret(profile['token']['credential']), profile['token'].get('encoding'))
        if not profile.get('credential') and not profile.get('token'):
            raise ValueError('VPN profile requires a password or a TOTP token')
    else:
        runtime['vpn'] = {'enabled': False}
    subscription = runtime.setdefault('subscription', {})
    subscription.pop('url', None)
    if enabled['clash'] and enabled['subscription']:
        subscription['url'] = resolve_secret(config['secrets']['subscription_url'])
    mihomo = runtime.setdefault('mihomo', {})
    mihomo.pop('secret', None)
    if enabled['clash']:
        mihomo['secret'] = resolve_secret(config['secrets']['controller'])
    return bundle
