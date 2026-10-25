"""Operator-side paths and SSH transport. No operator files enter the image."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import hashlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'image'))
from topology import deployment_topology, component_enabled
from target_platform import linux_platform


DEFAULT_CONFIG_PASS = 'home-gateway/deployment'
OPERATOR_CACHE = pathlib.Path('~/.config/home-gateway/deployment.json')


def json_object(body):
    try:
        value = json.loads(body)
    except ValueError:
        raise ValueError('Invalid configuration JSON; decrypted contents are not logged') from None
    if not isinstance(value, dict):
        raise ValueError('Configuration must be a JSON object')
    return value


def config_source(path=None):
    if path or os.environ.get('HOME_GATEWAY_DEPLOYMENT'):
        return str(path or os.environ['HOME_GATEWAY_DEPLOYMENT'])
    store = pathlib.Path(os.environ.get('PASSWORD_STORE_DIR', '~/.password-store')).expanduser()
    if (store / (DEFAULT_CONFIG_PASS + '.gpg')).exists() or not OPERATOR_CACHE.expanduser().exists():
        return 'pass:' + DEFAULT_CONFIG_PASS
    return str(OPERATOR_CACHE)


def normalize(value):
    # v0.2 operator files remain usable without changing private yadm data.
    if 'host' not in value:
        value['host'] = ({'backend': 'hyperv', 'hyperv': value['hyperv']}
                         if 'hyperv' in value else {'backend': 'linux'})
    if value['host'].get('backend') not in ('hyperv', 'linux'):
        raise ValueError('Supported host backends: hyperv, linux')
    return value


def deployment(path=None):
    source = config_source(path)
    if source.startswith('pass:'):
        value = json_object(secret(source[5:]))
    else:
        value = json_object(pathlib.Path(source).expanduser().read_text())
        if 'config_pass' in value:
            source = 'pass:' + value['config_pass']
            value = json_object(secret(value['config_pass']))
    value['_config_source'] = source
    return normalize(value)


def operator_values(config):
    """Allowlist non-secret local transport/VM metadata, never full runtime settings."""
    result = {}
    for key, fields in {'connection': ('Address', 'User', 'PublicKey', 'AuthorizedKeysFile', 'SshExecutable', 'KeyPath', 'KnownHostsPath'),
                        'operator': ('WslDistribution',)}.items():
        if key in config:
            result[key] = {name: config[key][name] for name in fields if name in config[key]}
    if 'host' in config:
        result['host'] = {key: config['host'][key] for key in ('backend', 'arch') if key in config['host']}
        if config['host'].get('hyperv'):
            fields = ('VMName', 'SwitchName', 'PhysicalAdapterGuid', 'MacAddress', 'MemoryMB', 'ProcessorCount', 'DiskSizeGB')
            result['host']['hyperv'] = {name: config['host']['hyperv'][name] for name in fields if name in config['host']['hyperv']}
    if 'timezone' in config:
        result['timezone'] = config['timezone']
    topology = deployment_topology(config)
    result['network'] = topology.as_dict()
    result['runtime'] = {'mihomo': {'external-controller': config['runtime'].get('mihomo', {}).get('external-controller', '127.0.0.1:9090')}}
    source = config.get('_config_source', '')
    if source.startswith('pass:'):
        result['config_pass'] = source[5:]
    return result


def operator_cache(source):
    if source in ('pass:' + DEFAULT_CONFIG_PASS, ''):
        return OPERATOR_CACHE.expanduser()
    key = hashlib.sha256(source.encode()).hexdigest()[:20]
    return OPERATOR_CACHE.expanduser().parent / 'targets' / (key + '.json')


def write_operator(config, path=None):
    if not config.get('_config_source', '').startswith('pass:'):
        return
    target = pathlib.Path(path).expanduser() if path else operator_cache(config['_config_source'])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=target.parent, prefix='.operator-', delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            json.dump(operator_values(config), stream, indent=2)
            stream.write('\n')
        temporary.replace(target)
        return target
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def operator_deployment(path=None):
    """Maintenance can use rendered metadata without unlocking GPG again."""
    source = config_source(path)
    targets = ([operator_cache(source), OPERATOR_CACHE.expanduser()] if source.startswith('pass:')
               else [pathlib.Path(source).expanduser()])
    for target in dict.fromkeys(targets):
        if not target.exists():
            continue
        value = json_object(target.read_text())
        matching = not source.startswith('pass:') or value.get('config_pass') == source[5:]
        if matching and all(key in value for key in ('connection', 'runtime')):
            return normalize(value)
    return deployment(path)


def ssh_settings(config):
    connection = config['connection']
    result = {'SshExecutable': 'ssh', 'KeyPath': str(pathlib.Path.home() / '.ssh/home-gateway_ed25519'),
              'KnownHostsPath': str(pathlib.Path.home() / '.ssh/home-gateway_known_hosts')}
    # Prefer native Linux keys. WSL may reuse the Windows operator's existing keys.
    kernel = pathlib.Path('/proc/sys/kernel/osrelease')
    is_wsl = kernel.exists() and 'microsoft' in kernel.read_text().lower()
    custom_transport = any(connection.get(name) for name in result)
    if not custom_transport and not pathlib.Path(result['KeyPath']).exists() and is_wsl:
        native_ssh = shutil.which('ssh.exe')
        cmd = shutil.which('cmd.exe')
        if native_ssh and cmd:
            windows_home = subprocess.run([cmd, '/c', 'echo', '%USERPROFILE%'], capture_output=True,
                                          text=True, timeout=10).stdout.strip().replace('\\', '/')
            if len(windows_home) > 3 and windows_home[1:3] == ':/':
                result = {'SshExecutable': native_ssh, 'KeyPath': windows_home + '/.ssh/home-gateway_ed25519',
                          'KnownHostsPath': windows_home + '/.ssh/home-gateway_known_hosts'}
    result.update({name: connection[name] for name in result if connection.get(name)})
    return result


def ssh_command(config):
    connection = config['connection']
    transport = ssh_settings(config)
    command = [transport['SshExecutable'], '-i', os.path.expanduser(transport['KeyPath']),
               '-o', 'UserKnownHostsFile=' + os.path.expanduser(transport['KnownHostsPath']),
               '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
               connection['User'] + '@' + connection['Address']]
    return command


def host_inputs(config):
    """Only non-secret target settings are sent to the host initializer."""
    topology = deployment_topology(config)
    # The current initializer configures one shared link. A future router
    # initializer consumes the same model, never guesses roles from NIC order.
    if topology.mode != 'one-arm':
        raise ValueError('Router host initialization is not implemented yet')
    if linux_platform(config.get('host', {}).get('arch', 'amd64')).arch != 'amd64':
        raise ValueError('Host initialization currently supports amd64 only; ARM64 artifacts can be built separately')
    controller = (config['runtime'].get('mihomo', {}).get('external-controller', '127.0.0.1:9090')
                  if component_enabled(config['runtime'], 'clash') else '127.0.0.1:9090')
    return {'network': {'address': topology.lan.address, 'gateway': topology.wan.gateway,
                        'dns': list(topology.wan.dns)}, 'interface': topology.lan.interface,
            'controller': controller,
            'tailscale_exit': topology.tailscale_exit}


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
    if not isinstance(entry, str) or not entry or entry.startswith('-') or any(c in entry for c in ('\n', '\r', '\0')):
        raise ValueError('Invalid credential entry')
    result = subprocess.run(['pass', 'show', entry], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Credential could not be decrypted; unlock GPG on the operator machine')
    value = result.stdout.rstrip(b'\r\n').decode()
    if not value:
        raise ValueError('An empty credential was returned')
    return value
