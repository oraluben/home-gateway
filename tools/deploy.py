"""Resolve the explicit operator configuration and deploy only appliance inputs."""
import argparse
import base64
import copy
import io
import importlib
import json
import pathlib
import subprocess
import sys
import tarfile
import uuid
from common import ROOT, deployment, json_object, remote, secret, ssh_command, write_operator
from ssh_access import deployment_access
sys.path.insert(0, str(ROOT / 'image'))
from vpn_auth import validate_totp


def materialize(config):
    access = deployment_access(config.get('connection', {}))
    runtime = copy.deepcopy(config['runtime'])
    if config.get('network', {}).get('address'):
        runtime['network']['address'] = config['network']['address']
    profiles = (json_object(secret(config['vpn_profiles_pass'])) if config.get('vpn_profiles_pass')
                else json_object(pathlib.Path(config['vpn_profiles_file']).expanduser().read_text()))
    if 'profiles_pass' in profiles:
        profiles = json_object(secret(profiles['profiles_pass']))
    vpn = profiles[config['vpn_profile']]
    runtime['vpn'].update(server=vpn['server'], username=vpn['username'])
    for key in ('password_file', 'token_mode', 'token_file'):
        runtime['vpn'].pop(key, None)
    bundle = {'runtime': runtime}
    if access is not None:
        bundle['ssh_access'] = access
    if vpn.get('credential'):
        runtime['vpn']['password_file'] = '/config/secrets/vpn-password'
        bundle['vpn_password'] = secret(vpn['credential'])
    if vpn.get('token'):
        if vpn['token'].get('mode') != 'totp':
            raise ValueError('Only TOTP token profiles are supported')
        runtime['vpn'].update(token_mode='totp', token_file='/config/secrets/vpn-token')
        bundle['vpn_token'] = validate_totp(secret(vpn['token']['credential']), vpn['token'].get('encoding'))
    if not vpn.get('credential') and not vpn.get('token'):
        raise ValueError('VPN profile requires a password or a TOTP token')
    runtime['subscription']['url'] = secret(config['secrets']['subscription_url'])
    runtime['mihomo']['secret'] = secret(config['secrets']['controller'])
    return bundle


def source_archive():
    """Allowlist code and verified artifacts, never arbitrary operator files."""
    files = [p for p in ROOT.iterdir() if p.is_file() and p.suffix in ('.py', '.sh')]
    files += [ROOT / name for name in ('compose.yaml', 'versions.json')]
    files += list((ROOT / 'image').glob('*.py'))
    files += [ROOT / 'image/Dockerfile', ROOT / 'image/.dockerignore', ROOT / 'image/mihomo']
    files += list((ROOT / 'image/licenses').glob('*'))
    files += [ROOT / 'artifacts/dashboard.tgz', ROOT / 'tools/guest-install.py', ROOT / 'tools/ssh_access.py']
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for path in files:
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--restore', help='Encrypted runtime snapshot; current pass configuration remains authoritative')
    parser.add_argument('--rebuild', action='store_true', help='Rebuild even when the deployed image matches the recipe')
    args = parser.parse_args()
    config = deployment(args.config)
    # Host changes are explicit and separate from image/configuration deployment.
    report = importlib.import_module('prepare-host').prepare(config)
    if not report['ready']:
        print(json.dumps(report, indent=2))
        raise RuntimeError('Target is not ready; run tools/prepare-host.py --apply after resolving blockers')
    bundle = materialize(config)
    bundle['rebuild'] = args.rebuild
    if args.restore:
        from backup import decrypt
        bundle['restore'] = base64.b64encode(decrypt(pathlib.Path(args.restore).expanduser())).decode()
    versions = json.loads((ROOT / 'versions.json').read_text())
    bundle['image'] = versions['image']
    bundle['version'] = versions['version']
    name = 'home-gateway-deploy-' + uuid.uuid4().hex
    archive = source_archive()
    remote(config, 'umask 077; cat > /tmp/' + name + '.tar.gz', archive)
    remote(config, 'umask 077; tar -xOf /tmp/' + name + '.tar.gz tools/guest-install.py > /tmp/' + name + '.py')
    command = 'sudo python3 /tmp/' + name + '.py --source /tmp/' + name + '.tar.gz'
    if args.validate_only:
        command += ' --validate-only'
    try:
        result = remote(config, command, json.dumps(bundle).encode(), timeout=420)
        if not args.validate_only:
            write_operator(config)
        print(result.decode().strip())
    finally:
        remote(config, 'rm -f /tmp/' + name + '.tar.gz /tmp/' + name + '.py')


if __name__ == '__main__':
    main()
