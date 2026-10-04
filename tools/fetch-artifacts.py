"""Download public, versioned inputs; refuse a checksum mismatch."""
import argparse
import gzip
import hashlib
import json
import pathlib
import urllib.request
from common import ROOT


def fetch(url, proxy=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy, 'http': proxy} if proxy else {}))
    with opener.open(url, timeout=45) as response:
        return response.read()


def ensure(path, entry, proxy=None):
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256']:
        return
    body = fetch(entry['url'], proxy)
    if hashlib.sha256(body).hexdigest() != entry['sha256']:
        raise ValueError('Artifact checksum mismatch')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--proxy')
    parser.add_argument('--ubuntu', action='store_true')
    args = parser.parse_args()
    versions = json.loads((ROOT / 'versions.json').read_text())
    artifact = ROOT / 'artifacts'
    ensure(artifact / 'mihomo.gz', versions['mihomo'], args.proxy)
    binary = gzip.decompress((artifact / 'mihomo.gz').read_bytes())
    if hashlib.sha256(binary).hexdigest() != versions['mihomo']['binary_sha256']:
        raise ValueError('Mihomo binary checksum mismatch')
    (ROOT / 'image/mihomo').write_bytes(binary)
    (ROOT / 'image/mihomo').chmod(0o755)
    ensure(artifact / 'dashboard.tgz', versions['dashboard'], args.proxy)
    licenses = ROOT / 'image/licenses'
    licenses.mkdir(exist_ok=True)
    for name in ('mihomo', 'dashboard'):
        (licenses / (name + '-LICENSE')).write_bytes(fetch(versions[name]['license_url'], args.proxy))
    (licenses / 'NOTICE').write_bytes((ROOT / 'THIRD_PARTY.md').read_bytes())
    if args.ubuntu:
        ensure(artifact / 'ubuntu.img', versions['ubuntu'], args.proxy)
    print('Versioned artifacts verified: ' + versions['version'])


if __name__ == '__main__':
    main()
