"""Resolve the explicit operator configuration and deploy only appliance inputs."""
import argparse
import base64
import io
import importlib
import json
import pathlib
import subprocess
import sys
import tarfile
import uuid
from common import ROOT, deployment, remote, secret, write_operator
from ssh_access import deployment_access
from render import render_bundle
from mihomo_build import selected_build, configure_runtime
from target_platform import linux_platform
sys.path.insert(0, str(ROOT / 'image'))


def materialize(config):
    access = deployment_access(config.get('connection', {}))
    bundle = render_bundle(config, secret)
    if access is not None:
        bundle['ssh_access'] = access
    return bundle


def source_archive(build=None):
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
            if build and path == ROOT / 'versions.json':
                versions = json.loads(path.read_text())
                versions['mihomo'] = build['metadata']
                body = (json.dumps(versions, indent=2) + '\n').encode()
                entry = tarfile.TarInfo('versions.json')
                entry.size = len(body)
                archive.addfile(entry, io.BytesIO(body))
                continue
            if build and (path == ROOT / 'image/mihomo' or path == ROOT / 'image/licenses/mihomo-LICENSE'):
                continue
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
        if build:
            for key, name in (('binary', 'image/mihomo'), ('license', 'image/licenses/mihomo-LICENSE'),
                              ('source', 'image/licenses/mihomo-SOURCE.tar.gz')):
                archive.add(build[key], arcname=name, recursive=False)
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--restore', help='Encrypted runtime snapshot; current pass configuration remains authoritative')
    parser.add_argument('--rebuild', action='store_true', help='Rebuild even when the deployed image matches the recipe')
    parser.add_argument('--mihomo-build', help='Verified local build.json; default uses the pinned upstream release')
    args = parser.parse_args()
    config = deployment(args.config)
    arch = linux_platform(config.get('host', {}).get('arch', 'amd64')).arch
    build = selected_build(args.mihomo_build or config['mihomo_build'], expected_arch=arch) if args.mihomo_build or config.get('mihomo_build') else None
    if build is None and arch != 'amd64':
        raise ValueError('The pinned stock Mihomo artifact is amd64; select a matching custom build')
    # Host changes are explicit and separate from image/configuration deployment.
    report = importlib.import_module('prepare-host').prepare(config)
    if not report['ready']:
        print(json.dumps(report, indent=2))
        raise RuntimeError('Target is not ready; run tools/prepare-host.py --apply after resolving blockers')
    bundle = materialize(config)
    configure_runtime(bundle['runtime'], build)
    bundle['rebuild'] = args.rebuild
    if args.restore:
        from backup import decrypt
        bundle['restore'] = base64.b64encode(decrypt(pathlib.Path(args.restore).expanduser())).decode()
    versions = json.loads((ROOT / 'versions.json').read_text())
    bundle['image'] = versions['image']
    bundle['version'] = versions['version']
    name = 'home-gateway-deploy-' + uuid.uuid4().hex
    archive = source_archive(build)
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
