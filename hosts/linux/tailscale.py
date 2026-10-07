"""Optional management service on the gateway host, outside its container."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

RECEIPT = pathlib.Path('/var/lib/home-gateway/tailscale-managed.json')
DROPIN = pathlib.Path('/etc/systemd/system/tailscaled.service.d/home-gateway.conf')
SYSCTL = pathlib.Path('/etc/sysctl.d/91-home-gateway-tailscale.conf')
SYSCTL_BODY = 'net.ipv4.conf.all.src_valid_mark=0\n'
SERVICE = '''[Unit]
After=systemd-sysctl.service
StartLimitIntervalSec=300
StartLimitBurst=4

[Service]
Environment=TS_DEBUG_FIREWALL_MODE=nftables
BindReadOnlyPaths=/proc/sys/net/ipv4/conf/all/src_valid_mark
Restart=on-failure
RestartSec=15s
'''
LEGACY_SERVICE = SERVICE.replace('After=systemd-sysctl.service\n', '').replace(
    'BindReadOnlyPaths=/proc/sys/net/ipv4/conf/all/src_valid_mark\n', '')
APT = ['apt-get', '-o', 'Acquire::Retries=2', '-o', 'Acquire::http::Timeout=20',
       '-o', 'Acquire::https::Timeout=20']


def run(args, timeout=240):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, 'LC_ALL': 'C', 'DEBIAN_FRONTEND': 'noninteractive'})
    if result.returncode:
        raise RuntimeError('Tailscale preparation failed: ' + args[0] + ' (exit ' + str(result.returncode) + ')')
    return result.stdout.strip()


def download(url, path):
    run(['curl', '--fail', '--silent', '--show-error', '--location', '--retry', '2',
         '--connect-timeout', '10', '--max-time', '60', url, '-o', str(path)], timeout=200)
    path.chmod(0o644)


def install(version):
    if os.geteuid() != 0:
        raise ValueError('Run the host installer as root')
    if not re.fullmatch(r'1\.\d+\.\d+', version):
        raise ValueError('Invalid pinned Tailscale version')
    release = dict(line.split('=', 1) for line in pathlib.Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if (release.get('ID'), release.get('VERSION_ID', '').strip('"')) != ('ubuntu', '24.04') or run(['uname', '-m']) != 'x86_64':
        raise ValueError('Supported target: Ubuntu 24.04 amd64')
    if shutil.which('tailscale'):
        if not RECEIPT.exists():
            raise ValueError('Existing Tailscale installation is not managed by this project; inspect it manually')
        receipt = json.loads(RECEIPT.read_text())
        if (receipt.get('schema'), receipt.get('firewall'), receipt.get('version')) != (1, 'nftables', version):
            raise ValueError('Existing Tailscale installation receipt differs; inspect it manually')
        if run(['tailscale', 'version']).splitlines()[0] != version:
            raise ValueError('Existing Tailscale version differs; upgrade explicitly before re-running preparation')
        previous = DROPIN.read_text()
        if previous not in (SERVICE, LEGACY_SERVICE):
            raise ValueError('Tailscale service settings changed; inspect them manually')
        changed = previous != SERVICE or not SYSCTL.exists() or SYSCTL.read_text() != SYSCTL_BODY
        if changed:
            install_compatibility()
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'restart', 'tailscaled'])
        run(['tailscale', 'set', '--auto-update=false'])
        return {'installed': True, 'version': version, 'changed': changed}
    tables = json.loads(run(['nft', '-j', 'list', 'tables']))
    if any((item['table'].get('family'), item['table']['name']) not in (
            ('inet', 'home_gateway_base'), ('inet', 'home_gateway_proxy'))
           for item in tables.get('nftables', []) if 'table' in item):
        raise ValueError('Unmanaged firewall tables exist; inspect before installing Tailscale')
    # Install from the signed official repository; never execute a remote shell script.
    run(APT + ['update'])
    run(APT + ['install', '-y', '--no-install-recommends', 'ca-certificates', 'curl'])
    key = pathlib.Path('/usr/share/keyrings/tailscale-archive-keyring.gpg')
    key.parent.mkdir(parents=True, exist_ok=True)
    download('https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg', key)
    source = pathlib.Path('/etc/apt/sources.list.d/tailscale.list')
    body = 'deb [signed-by=' + str(key) + '] https://pkgs.tailscale.com/stable/ubuntu noble main\n'
    if source.exists() and source.read_text() != body:
        raise ValueError('Existing Tailscale package source differs')
    source.write_text(body)
    source.chmod(0o644)
    # The package may start the daemon: install our firewall choice beforehand.
    install_compatibility()
    run(['systemctl', 'daemon-reload'])
    run(APT + ['update'])
    run(APT + ['install', '-y', '--no-install-recommends', 'tailscale=' + version], timeout=360)
    run(['apt-mark', 'hold', 'tailscale'])
    run(['systemctl', 'enable', '--now', 'tailscaled'])
    run(['tailscale', 'set', '--accept-dns=false', '--accept-routes=false', '--ssh=false', '--auto-update=false',
         '--advertise-exit-node=false', '--advertise-routes=', '--exit-node='])
    RECEIPT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    RECEIPT.write_text(json.dumps({'schema': 1, 'firewall': 'nftables', 'version': version}) + '\n')
    RECEIPT.chmod(0o600)
    return {'installed': True, 'version': version, 'changed': True, 'login': 'required'}


def install_compatibility():
    # Tailscale >=1.98 globally enables src_valid_mark when setting up netfilter.
    # TPROXY needs unmarked reverse-source lookups even with rp_filter disabled.
    # A read-only bind in this service's mount namespace prevents that one write,
    # including asynchronous login/reconfiguration; it leaves host networking shared.
    if SYSCTL.exists() and SYSCTL.read_text() != SYSCTL_BODY:
        raise ValueError('Existing Tailscale compatibility sysctl file differs')
    SYSCTL.write_text(SYSCTL_BODY)
    SYSCTL.chmod(0o644)
    run(['sysctl', '-p', str(SYSCTL)])
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    DROPIN.write_text(SERVICE)
    DROPIN.chmod(0o644)


if __name__ == '__main__':
    try:
        print(json.dumps(install(json.load(sys.stdin)['linux'])))
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
