"""Reproducible local build and source bundle; does not publish anything."""
import argparse
import gzip
import io
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
from target_platform import linux_platform


def git(source, *args):
    return subprocess.run(['git', '-C', str(source), *args], capture_output=True, check=True).stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, help='Clean local Mihomo Git checkout')
    parser.add_argument('--output', required=True, help='New build directory')
    parser.add_argument('--go', default=shutil.which('go'))
    parser.add_argument('--feature', action='append', default=[])
    parser.add_argument('--arch', choices=('amd64', 'arm64'), default='amd64')
    args = parser.parse_args()
    platform = linux_platform(args.arch)
    source, output = pathlib.Path(args.source).expanduser().resolve(), pathlib.Path(args.output).expanduser().resolve()
    if not args.go:
        raise ValueError('Select an installed Go toolchain with --go')
    if git(source, 'status', '--porcelain'):
        raise ValueError('Commit local source changes before building a pinned artifact')
    revision = git(source, 'rev-parse', 'HEAD').decode().strip()
    repository = git(source, 'remote', 'get-url', 'origin').decode().strip().removesuffix('.git')
    if not repository.startswith('https://') or '@' in repository or '?' in repository:
        raise ValueError('Use a public HTTPS origin without credentials')
    if not all(re.fullmatch('[a-z0-9-]{1,64}', v) for v in args.feature):
        raise ValueError('Invalid feature name')
    epoch = int(git(source, 'show', '-s', '--format=%ct', 'HEAD'))
    date = git(source, 'show', '-s', '--format=%cI', 'HEAD').decode().strip()
    base = git(source, 'describe', '--tags', '--abbrev=0').decode().strip()
    version = base + '+custom.' + revision[:12] if re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', base) else 'custom-' + revision[:12]
    output.mkdir(parents=True, exist_ok=False)
    archive = gzip.compress(git(source, 'archive', '--format=tar', 'HEAD'), mtime=0)
    (output / 'source.tar.gz').write_bytes(archive)
    license_body = git(source, 'show', 'HEAD:LICENSE')
    (output / 'LICENSE').write_bytes(license_body)
    binary = output / ('mihomo-linux-' + platform.arch)
    flags = (f'-X github.com/metacubex/mihomo/constant.Version={version} '
             f'-X github.com/metacubex/mihomo/constant.BuildTime={date} -s -w -buildid=')
    environment = {**os.environ, 'CGO_ENABLED': '0', 'GOOS': 'linux', 'GOARCH': platform.arch,
                   'GOAMD64': 'v1', 'SOURCE_DATE_EPOCH': str(epoch)}
    # Build the exact tracked source bundle, not ignored/untracked local inputs.
    with tempfile.TemporaryDirectory(prefix='mihomo-build-') as temporary:
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as package:
            package.extractall(temporary, filter='data')
        subprocess.run([args.go, 'build', '-p', '4', '-tags', 'with_gvisor', '-trimpath', '-buildvcs=false',
                        '-ldflags', flags, '-o', str(binary), '.'], cwd=temporary, env=environment, check=True)
    binary.chmod(0o755)
    with binary.open('rb') as stream:
        platform.verify_elf(stream.read(20))
    toolchain = subprocess.run([args.go, 'version'], capture_output=True, text=True, check=True).stdout.strip()
    manifest = {'format': 1, 'version': version, 'binary': binary.name,
                'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'license': 'LICENSE',
                'license_sha256': hashlib.sha256(license_body).hexdigest(),
                'features': args.feature, 'goos': 'linux', 'goarch': platform.arch, 'toolchain': toolchain,
                'build_tags': ['with_gvisor'], 'source': {'repository': repository, 'revision': revision,
                'archive': 'source.tar.gz', 'sha256': hashlib.sha256(archive).hexdigest()}}
    (output / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'manifest': str(output / 'build.json'), 'version': version,
                      'binary_sha256': manifest['binary_sha256']}))


if __name__ == '__main__':
    main()
