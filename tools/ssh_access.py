"""Operator-provided public keys; SSH access belongs to the target, not the image."""
import base64
import os
import pathlib
import struct
import subprocess
import tempfile


def parse_keys(body):
    """Accept plain public-key lines, deduplicating by algorithm and key blob."""
    keys, seen = [], set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split()
        if len(fields) < 2 or not fields[0].startswith(('ssh-', 'ecdsa-', 'sk-')):
            raise ValueError('Expected plain SSH public keys without authorized-key options')
        try:
            blob = base64.b64decode(fields[1], validate=True)
            length = struct.unpack('>I', blob[:4])[0]
            if blob[4:4 + length].decode() != fields[0] or len(blob) <= 4 + length:
                raise ValueError()
        except (ValueError, UnicodeError, struct.error):
            raise ValueError('Invalid SSH public key') from None
        identity = tuple(fields[:2])
        if identity not in seen:
            keys.append(line)
            seen.add(identity)
    if not keys:
        raise ValueError('SSH public-key list is empty')
    return keys


def seed_keys(connection):
    primary = parse_keys(connection['PublicKey'])
    if len(primary) != 1:
        raise ValueError('PublicKey must contain one management public key')
    source = connection.get('AuthorizedKeysFile')
    keys = primary
    if source:
        additional = parse_keys(pathlib.Path(source).expanduser().read_text(encoding='utf-8'))
        keys = parse_keys('\n'.join(primary + additional))
    with tempfile.TemporaryDirectory(prefix='home-gateway-public-keys-') as directory:
        path = pathlib.Path(directory) / 'authorized_keys'
        path.write_text('\n'.join(keys) + '\n', encoding='utf-8')
        result = subprocess.run(['ssh-keygen', '-lf', str(path)], capture_output=True, timeout=10)
        if result.returncode or len(result.stdout.splitlines()) != len(keys):
            raise ValueError('SSH public-key validation failed')
    return keys


def deployment_access(connection):
    if not connection.get('AuthorizedKeysFile'):
        return None  # Existing deployments without this option keep their SSH policy.
    return {'user': connection['User'], 'keys': seed_keys(connection)}


def target_access(access):
    """Resolve and validate the existing target account without changing access."""
    import pwd
    keys = parse_keys('\n'.join(access['keys']))
    account = pwd.getpwnam(access['user'])
    path = pathlib.Path(account.pw_dir) / '.ssh/authorized_keys'
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('Refusing to replace a symlinked SSH authorization path')
    if not pathlib.Path(account.pw_dir).is_dir():
        raise ValueError('SSH account has no home directory')
    return {'path': path, 'uid': account.pw_uid, 'gid': account.pw_gid,
            'body': ('\n'.join(keys) + '\n').encode(), 'count': len(keys)}


def snapshot_access(target):
    path = target['path']
    return {'body': path.read_bytes() if path.exists() else None,
            'mode': path.stat().st_mode & 0o777 if path.exists() else 0o600}


def write_access(target, body, mode=0o600):
    """Replace the entire explicitly managed list atomically; never restart sshd."""
    path = target['path']
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('Refusing to replace a symlinked SSH authorization path')
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    os.chown(path.parent, target['uid'], target['gid'])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, prefix='.authorized-keys-', delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.chown(temporary, target['uid'], target['gid'])
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def restore_access(target, snapshot):
    if snapshot['body'] is None:
        target['path'].unlink(missing_ok=True)
    else:
        write_access(target, snapshot['body'], snapshot['mode'])
