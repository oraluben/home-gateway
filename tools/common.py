"""Operator-side paths and SSH transport. No operator files enter the image."""
import json
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def deployment(path=None):
    path = pathlib.Path(path or os.environ.get('HOME_GATEWAY_DEPLOYMENT', '~/.config/home-gateway/deployment.json')).expanduser()
    value = json.loads(path.read_text())
    return value


def ssh_command(config):
    connection = config['connection']
    command = [connection.get('SshExecutable', 'ssh'), '-i', connection['KeyPath'],
               '-o', 'UserKnownHostsFile=' + connection['KnownHostsPath'],
               '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
               connection['User'] + '@' + connection['Address']]
    return command


def remote(config, command, content=None, timeout=120):
    result = subprocess.run(ssh_command(config) + [command], input=content, capture_output=True, timeout=timeout)
    if result.returncode:
        # Report only the installer metadata, never arbitrary SSH/server output.
        phase = ''
        try:
            report = json.loads(result.stderr)
            if report.get('phase') in ('unpack', 'build', 'validate', 'apply', 'rollback'):
                phase = ', phase ' + report['phase']
        except (ValueError, AttributeError):
            pass
        raise RuntimeError('Remote operation failed (exit ' + str(result.returncode) + phase + ')')
    return result.stdout


def secret(entry):
    if not isinstance(entry, str) or not entry or entry.startswith('-') or '\n' in entry:
        raise ValueError('Invalid credential entry')
    result = subprocess.run(['pass', 'show', entry], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Credential could not be decrypted; unlock GPG on the operator machine')
    value = result.stdout.rstrip(b'\r\n').decode()
    if not value:
        raise ValueError('An empty credential was returned')
    return value
