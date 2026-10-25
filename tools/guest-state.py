"""Snapshot durable state without copying a live database or any logs."""
import importlib
import io
import json
import pathlib
import sys
import tarfile
import time

PROJECT = pathlib.Path('/opt/home-gateway')
sys.path.insert(0, str(PROJECT))
updater = importlib.import_module('update-subscription')
config = updater.load_config()
# This transient snapshot helper also runs against appliances from before the
# topology module existed. Keep its two optional flags self-contained.
components = {name: config.get(name, {}).get('enabled', default) for name, default in
              (('clash', True), ('subscription', False))}
if any(not isinstance(value, bool) for value in components.values()):
    raise ValueError('Snapshot component flags must be booleans')
choices = updater.selections(config) if components['clash'] else {}
names = ['config/gateway.yaml', 'config/secrets/vpn-password', 'config/secrets/vpn-token', 'versions.json',
         'data/subscription/current.yaml', 'data/subscription/previous.yaml']
names += ['data/mihomo/' + name for name in ('geoip.dat', 'geosite.dat', 'Country.mmdb', 'ASN.mmdb')]
stream = io.BytesIO()
with tarfile.open(fileobj=stream, mode='w:gz') as archive:
    for name in names:
        path = PROJECT / name
        if path.is_file():
            archive.add(path, arcname=name, recursive=False)
    body = json.dumps({'format': 1, 'created_at': time.time(), 'selected': choices, 'components': components}).encode()
    entry = tarfile.TarInfo('snapshot.json')
    entry.size, entry.mode = len(body), 0o600
    archive.addfile(entry, io.BytesIO(body))
sys.stdout.buffer.write(stream.getvalue())
