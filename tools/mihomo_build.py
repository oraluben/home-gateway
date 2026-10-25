"""Verify an explicitly selected local build; paths never enter runtime metadata."""
import hashlib
import json
import pathlib
import re
from target_platform import linux_platform


def verified_file(folder, name, digest=None):
    if not isinstance(name, str) or not name:
        raise ValueError('Missing build artifact')
    path = (folder / name).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ValueError('Build artifacts must be regular files inside the manifest directory')
    if digest and (not re.fullmatch('[0-9a-f]{64}', digest) or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
        raise ValueError('Custom Mihomo artifact checksum mismatch')
    return path


def selected_build(filename, expected_arch=None):
    manifest = pathlib.Path(filename).expanduser().resolve()
    value = json.loads(manifest.read_text())
    if value.get('format') != 1 or value.get('goos') != 'linux' or value.get('goarch') not in ('amd64', 'arm64'):
        raise ValueError('Expected a format-1 Linux amd64/arm64 Mihomo build manifest')
    platform = linux_platform(value['goarch'])
    if expected_arch is not None and platform != linux_platform(expected_arch):
        raise ValueError('Mihomo build architecture differs from the deployment target')
    version, source = value.get('version'), value.get('source') or {}
    if not isinstance(version, str) or not re.fullmatch('[A-Za-z0-9._+-]{1,128}', version):
        raise ValueError('Invalid custom Mihomo version')
    if not re.fullmatch('[0-9a-f]{40}', source.get('revision', '')):
        raise ValueError('Custom Mihomo source must have an exact Git revision')
    repository = source.get('repository', '')
    if not repository.startswith('https://') or '@' in repository or '?' in repository or '#' in repository:
        raise ValueError('Source repository must be a public HTTPS URL without credentials')
    features = value.get('features', [])
    if not isinstance(features, list) or not all(isinstance(v, str) and re.fullmatch('[a-z0-9-]{1,64}', v) for v in features):
        raise ValueError('Invalid custom Mihomo feature list')
    for digest in (value.get('binary_sha256'), value.get('license_sha256'), source.get('sha256')):
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Every custom build artifact requires a SHA-256 digest')
    folder = manifest.parent
    binary = verified_file(folder, value.get('binary'), value.get('binary_sha256'))
    header = binary.read_bytes()[:20]
    platform.verify_elf(header)
    license_path = verified_file(folder, value.get('license'), value['license_sha256'])
    archive = verified_file(folder, source.get('archive'), source.get('sha256'))
    metadata = {'version': version, 'binary_sha256': value['binary_sha256'], 'features': features,
                'source': {key: source[key] for key in ('repository', 'revision', 'sha256')},
                'goos': 'linux', 'goarch': platform.arch, 'toolchain': value.get('toolchain'),
                'build_tags': value.get('build_tags', [])}
    return {'metadata': metadata, 'binary': binary, 'license': license_path, 'source': archive}


def configure_runtime(runtime, build):
    if build and 'connection-history-v1' in build['metadata']['features']:
        runtime['mihomo']['connection-history'] = {'capacity': 4096, 'log': False}
        runtime.setdefault('usage', {})['require_history'] = True
