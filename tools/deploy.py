"""Resolve the explicit operator configuration and deploy only appliance inputs."""
import argparse
import base64
import copy
import io
import json
import pathlib
import subprocess
import tarfile
import uuid
from common import ROOT, deployment, remote, secret, ssh_command


def materialize(config):
    runtime = copy.deepcopy(config['runtime'])
    profiles = json.loads(pathlib.Path(config['vpn_profiles_file']).expanduser().read_text())
    vpn = profiles[config['vpn_profile']]
    runtime['vpn'].update(server=vpn['server'], username=vpn['username'], password_file='/config/secrets/vpn-password')
    runtime['subscription']['url'] = secret(config['secrets']['subscription_url'])
    runtime['mihomo']['secret'] = secret(config['secrets']['controller'])
    return {'runtime': runtime, 'vpn_password': secret(vpn['credential'])}


def source_archive():
    """Allowlist code and verified artifacts, never arbitrary operator files."""
    files = [p for p in ROOT.iterdir() if p.is_file() and p.suffix in ('.py', '.sh')]
    files += [ROOT / name for name in ('compose.yaml', 'versions.json')]
    files += list((ROOT / 'image').glob('*.py'))
    files += [ROOT / 'image/Dockerfile', ROOT / 'image/.dockerignore', ROOT / 'image/mihomo']
    files += list((ROOT / 'image/licenses').glob('*'))
    files += [ROOT / 'artifacts/dashboard.tgz', ROOT / 'tools/guest-install.py']
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for path in files:
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--restore', help='Encrypted runtime snapshot; configuration still comes from yadm/pass')
    args = parser.parse_args()
    config = deployment(args.config)
    bundle = materialize(config)
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
        # A new cloud image has Python but may lack Docker and PyYAML.
        bootstrap = ('if ! command -v docker >/dev/null; then '
                     'tar -xOf /tmp/' + name + '.tar.gz guest-bootstrap.sh | sudo bash; '
                     'elif ! python3 -c "import yaml" 2>/dev/null; then '
                     'sudo apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=20 update && '
                     'sudo apt-get -y install python3-yaml; fi')
        remote(config, bootstrap, timeout=420)
        result = remote(config, command, json.dumps(bundle).encode(), timeout=420)
        print(result.decode().strip())
    finally:
        remote(config, 'rm -f /tmp/' + name + '.tar.gz /tmp/' + name + '.py')


if __name__ == '__main__':
    main()
