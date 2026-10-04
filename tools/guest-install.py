"""Root-side transaction: build/validate first, then replace code and configuration."""
import argparse
import base64
import datetime
import hashlib
import importlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import yaml

PROJECT = pathlib.Path('/opt/home-gateway')
PHASE = 'unpack'


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, timeout=kwargs.pop('timeout', 40), **kwargs)
    if result.returncode:
        raise RuntimeError('Operation failed: ' + command[0])
    return result.stdout


def write(path, body, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(body)
    path.chmod(mode)


def safe_extract(archive, target):
    with tarfile.open(archive, 'r:gz') as package:
        for entry in package.getmembers():
            if not entry.isfile() or not (target / entry.name).resolve().is_relative_to(target.resolve()):
                raise ValueError('Invalid source archive')
        package.extractall(target, filter='data')


def install_ui(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as package:
        for entry in package.getmembers():
            if entry.isdir():
                continue
            if not entry.isfile() or not (destination / entry.name).resolve().is_relative_to(destination.resolve()):
                raise ValueError('Invalid dashboard archive')
            target = destination / entry.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(package.extractfile(entry).read())
    if not (destination / 'index.html').is_file():
        raise ValueError('Dashboard archive has no index.html')
    (destination / 'config.js').write_text("window.__METACUBEXD_CONFIG__ = {defaultBackendURL: window.location.origin, githubToken: ''};\n")


def main():
    global PHASE
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    bundle = json.load(sys.stdin)
    PROJECT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.stage-', dir=PROJECT) as temporary:
        stage = pathlib.Path(temporary)
        safe_extract(args.source, stage)
        versions = json.loads((stage / 'versions.json').read_text())
        if bundle['image'] != versions['image'] or bundle['version'] != versions['version']:
            raise ValueError('Deployment version mismatch')
        if hashlib.sha256((stage / 'image/mihomo').read_bytes()).hexdigest() != versions['mihomo']['binary_sha256']:
            raise ValueError('Mihomo checksum mismatch')
        if hashlib.sha256((stage / 'artifacts/dashboard.tgz').read_bytes()).hexdigest() != versions['dashboard']['sha256']:
            raise ValueError('Dashboard checksum mismatch')
        config = bundle['runtime']
        write(stage / 'config/gateway.yaml', yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode())
        write(stage / 'config/secrets/vpn-password', (bundle['vpn_password'] + '\n').encode())
        data = PROJECT / 'data'
        data.mkdir(mode=0o700, exist_ok=True)
        restored = []
        selected = None
        candidate_data = data
        if bundle.get('restore'):
            # Only durable data is applied; operator configuration remains authoritative.
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(bundle['restore'])), mode='r:gz') as archive:
                allowed = {'data/subscription/current.yaml', 'data/subscription/previous.yaml'}
                allowed.update('data/mihomo/' + name for name in ('geoip.dat', 'geosite.dat', 'Country.mmdb', 'ASN.mmdb'))
                selected = json.load(archive.extractfile('snapshot.json'))['selected']
                for entry in archive.getmembers():
                    if not entry.isfile():
                        raise ValueError('Invalid restore entry')
                    if entry.name in allowed:
                        write(stage / 'restore' / entry.name, archive.extractfile(entry).read())
                        restored.append(entry.name)
            candidate_data = stage / 'restore/data'
        if not (candidate_data / 'subscription/current.yaml').is_file():
            sys.path.insert(0, str(stage))
            sys.path.insert(0, str(stage / 'image'))
            os.environ['HOME_GATEWAY_DATA'] = str(candidate_data)
            module = importlib.import_module('update-subscription')
            write(candidate_data / 'subscription/current.yaml', module.download(config['subscription']))
        os.environ['HOME_GATEWAY_DATA'] = str(candidate_data)
        sys.path.insert(0, str(stage / 'image'))
        render = importlib.import_module('configuration').render_mihomo
        generated = render(config, yaml.safe_load((candidate_data / 'subscription/current.yaml').read_bytes()))
        write(stage / 'validation.yaml', yaml.safe_dump(generated, allow_unicode=True, sort_keys=False).encode())
        PHASE = 'build'
        run(['docker', 'build', '--network=host', '--build-arg', 'BASE_IMAGE=' + versions['base_image'],
             '-t', bundle['image'], str(stage / 'image')], timeout=300)
        PHASE = 'validate'
        run(['docker', 'run', '--rm', '--network=none', '--entrypoint', 'mihomo',
             '-v', str(candidate_data) + ':/data:ro', '-v', str(stage / 'validation.yaml') + ':/validate.yaml:ro',
             bundle['image'], '-t', '-d', '/data/mihomo', '-f', '/validate.yaml'])
        install_ui(stage / 'artifacts/dashboard.tgz', stage / 'ui')
        if args.validate_only:
            print(json.dumps({'state': 'validated', 'version': versions['version'], 'running_gateway_unchanged': True}))
            return
        backup = data / 'backups' / ('deploy-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(parents=True, mode=0o700)
        files = [p for p in stage.iterdir() if p.is_file() and p.suffix in ('.py', '.sh')]
        files += [stage / 'compose.yaml', stage / 'versions.json']
        for directory in ('image', 'config'):
            if (PROJECT / directory).exists():
                shutil.copytree(PROJECT / directory, backup / directory)
        for path in files:
            original = PROJECT / path.name
            if original.exists():
                shutil.copy2(original, backup / path.name)
        if (PROJECT / '.env').exists():
            shutil.copy2(PROJECT / '.env', backup / '.env')
        ui = data / 'mihomo/ui'
        if ui.exists():
            shutil.copytree(ui, backup / 'ui')
        for name in restored:
            if (PROJECT / name).exists():
                target = backup / 'runtime-data' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(PROJECT / name, target)
        PHASE = 'apply'
        subprocess.run(['systemctl', 'stop', 'home-gateway'], capture_output=True, timeout=45)
        try:
            for path in files:
                shutil.copy2(path, PROJECT / path.name)
            for directory in ('image', 'config'):
                target = PROJECT / directory
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(stage / directory, target)
            write(PROJECT / '.env', ('GATEWAY_IMAGE=' + bundle['image'] + '\nGATEWAY_BASE_IMAGE=' + versions['base_image'] + '\n').encode())
            if ui.exists():
                shutil.rmtree(ui)
            shutil.copytree(stage / 'ui', ui)
            for name in restored:
                write(PROJECT / name, (stage / 'restore' / name).read_bytes())
            run(['bash', str(PROJECT / 'install-guest.sh')])
            run(['bash', str(PROJECT / 'install-subscription.sh')])
            run(['systemctl', 'restart', 'home-gateway-base', 'home-gateway-dns'])
            run(['systemctl', 'reset-failed', 'home-gateway'])
            run(['systemctl', 'start', 'home-gateway'])
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                status = json.loads((data / 'status.json').read_text()) if (data / 'status.json').exists() else {}
                recent = (data / 'status.json').exists() and time.time() - (data / 'status.json').stat().st_mtime < 3
                proxy_ready = not config['clash'].get('enabled', True) or status.get('transparent_proxy')
                vpn_ready = not config['vpn'].get('enabled', True) or status.get('vpn_connected')
                if recent and proxy_ready and vpn_ready:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('Gateway did not recover after deployment')
            if selected is not None:
                sys.path.insert(0, str(PROJECT))
                updater = importlib.import_module('update-subscription')
                updater.restore_selections(config, selected)
        except Exception:
            PHASE = 'rollback'
            subprocess.run(['systemctl', 'stop', 'home-gateway'], capture_output=True, timeout=45)
            for path in files:
                old = backup / path.name
                if old.exists():
                    shutil.copy2(old, PROJECT / path.name)
            for directory in ('image', 'config'):
                target = PROJECT / directory
                if target.exists():
                    shutil.rmtree(target)
                if (backup / directory).exists():
                    shutil.copytree(backup / directory, target)
            if (backup / '.env').exists():
                shutil.copy2(backup / '.env', PROJECT / '.env')
            else:
                (PROJECT / '.env').unlink(missing_ok=True)
            if ui.exists():
                shutil.rmtree(ui)
            if (backup / 'ui').exists():
                shutil.copytree(backup / 'ui', ui)
            for name in restored:
                original = backup / 'runtime-data' / name
                if original.exists():
                    write(PROJECT / name, original.read_bytes())
                else:
                    (PROJECT / name).unlink(missing_ok=True)
            run(['bash', str(PROJECT / 'install-guest.sh')])
            if (backup / 'install-subscription.sh').exists():
                run(['bash', str(PROJECT / 'install-subscription.sh')])
            run(['systemctl', 'reset-failed', 'home-gateway', 'home-gateway-dns'])
            run(['systemctl', 'restart', 'home-gateway-base', 'home-gateway-dns', 'home-gateway'])
            raise RuntimeError('Deployment failed; the previous release was restored') from None
        # Retain legacy credentials only in the private rollback directory.
        for old in (data / 'gateway.yaml', data / 'secrets/vpn-password'):
            if old.exists():
                saved = backup / 'legacy' / old.relative_to(data)
                saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(old, saved)
                old.unlink()
        receipt = {'state': 'success', 'version': versions['version'], 'image': bundle['image'],
                   'image_id': run(['docker', 'image', 'inspect', bundle['image'], '--format', '{{.Id}}']).decode().strip(),
                   'backup': str(backup), 'updated_at': datetime.datetime.now().astimezone().isoformat()}
        write(data / 'deployment.json', (json.dumps(receipt, indent=2) + '\n').encode())
        print(json.dumps(receipt))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'state': 'failed', 'phase': PHASE, 'error': type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
