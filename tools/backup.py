"""Encrypt a live snapshot, or verify it without writing decrypted credentials."""
import argparse
import datetime
import io
import json
import pathlib
import subprocess
import tarfile
import uuid
from common import ROOT, deployment, remote

STATE_FILES = {'snapshot.json', 'versions.json', 'config/gateway.yaml', 'config/secrets/vpn-password',
               'data/subscription/current.yaml', 'data/subscription/previous.yaml'}
STATE_FILES.update('data/mihomo/' + name for name in ('geoip.dat', 'geosite.dat', 'Country.mmdb', 'ASN.mmdb'))


def inspect_snapshot(body):
    with tarfile.open(fileobj=io.BytesIO(body), mode='r:gz') as archive:
        entries = archive.getmembers()
        if len({entry.name for entry in entries}) != len(entries):
            raise ValueError('Duplicate snapshot entries')
        if any(not entry.isfile() or entry.name not in STATE_FILES for entry in entries):
            raise ValueError('Unexpected snapshot contents')
        names = {entry.name for entry in entries}
        if not {'snapshot.json', 'versions.json', 'config/gateway.yaml', 'data/subscription/current.yaml'} <= names:
            raise ValueError('Incomplete snapshot')
        metadata = json.load(archive.extractfile('snapshot.json'))
        if metadata.get('format') != 1:
            raise ValueError('Unsupported snapshot format')
        return metadata, len(entries)


def decrypt(path):
    result = subprocess.run(['gpg', '--batch', '--decrypt', str(path)], capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Backup decryption failed; unlock GPG on the operator machine')
    inspect_snapshot(result.stdout)
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('create', 'verify'))
    parser.add_argument('--config')
    parser.add_argument('--file')
    parser.add_argument('--recipient', action='append')
    args = parser.parse_args()
    if args.action == 'verify':
        if not args.file:
            parser.error('--file is required for verification')
        metadata, count = inspect_snapshot(decrypt(pathlib.Path(args.file)))
        print(json.dumps({'verified': True, 'files': count, 'selected_groups': len(metadata['selected'])}))
        return
    config = deployment(args.config)
    temporary = '/tmp/home-gateway-snapshot-' + uuid.uuid4().hex + '.py'
    remote(config, 'umask 077; cat > ' + temporary, (ROOT / 'tools/guest-state.py').read_bytes())
    try:
        body = remote(config, 'sudo python3 ' + temporary, timeout=90)
    finally:
        remote(config, 'rm -f ' + temporary)
    inspect_snapshot(body)
    recipients = args.recipient or (pathlib.Path.home() / '.password-store/.gpg-id').read_text().splitlines()
    command = ['gpg', '--batch', '--trust-model', 'always', '--encrypt']
    for recipient in recipients:
        if recipient.strip():
            command += ['--recipient', recipient.strip()]
    encrypted = subprocess.run(command, input=body, capture_output=True, timeout=60)
    if encrypted.returncode:
        raise RuntimeError('Backup encryption failed; check the recipients in your password store')
    output = pathlib.Path(args.file).expanduser() if args.file else pathlib.Path.home() / '.local/state/home-gateway/backups' / (
        'gateway-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '.tar.gz.gpg')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        stream.write(encrypted.stdout)
    output.chmod(0o600)
    print(json.dumps({'encrypted_backup': str(output), 'verified': True}))


if __name__ == '__main__':
    main()
