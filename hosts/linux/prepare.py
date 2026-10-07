"""Prepare a dedicated Ubuntu target. Standard library only, before Docker/PyYAML."""
import argparse
import ipaddress
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

DAEMON = pathlib.Path('/etc/docker/daemon.json')
SYSCTL = pathlib.Path('/etc/sysctl.d/90-home-gateway.conf')
BACKUP = pathlib.Path('/var/lib/home-gateway/host-before')
DOCKER_NETWORK = {'bridge': 'none', 'iptables': False, 'ip6tables': False, 'ip-forward': False}
TAILSCALE_RECEIPT = pathlib.Path('/var/lib/home-gateway/tailscale-managed.json')
APT = ['apt-get', '-o', 'Acquire::Retries=2', '-o', 'Acquire::http::Timeout=20',
       '-o', 'Acquire::https::Timeout=20']


def command(args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, 'LC_ALL': 'C', 'DEBIAN_FRONTEND': 'noninteractive'})


def run(args, timeout=240):
    result = command(args, timeout)
    if result.returncode:
        raise RuntimeError('Host operation failed: ' + args[0] + ' (exit ' + str(result.returncode) + ')')
    return result.stdout


def read(path, default=''):
    path = pathlib.Path(path)
    return path.read_text() if path.exists() else default


def settings(value):
    interface = value['interface']
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', interface):
        raise ValueError('Invalid network interface name')
    address = ipaddress.IPv4Interface(value['network']['address'])
    gateway = ipaddress.IPv4Address(value['network']['gateway'])
    if gateway not in address.network or gateway == address.ip:
        raise ValueError('Upstream gateway must be a different address on the LAN')
    host, separator, port = value['controller'].rpartition(':')
    controller = ipaddress.IPv4Address(host)
    if not separator or not 1 <= int(port) <= 65535 or controller not in (address.ip, ipaddress.IPv4Address('127.0.0.1')):
        raise ValueError('Controller must bind to the target LAN address or 127.0.0.1')
    return interface, address, gateway, (str(controller), int(port))


def sysctls(interface, tailscale_exit=False):
    # Slash separators also handle dotted VLAN interface names correctly.
    values = {'net/ipv4/ip_forward': '1', 'net/ipv4/conf/all/send_redirects': '0',
            'net/ipv4/conf/default/send_redirects': '0', f'net/ipv4/conf/{interface}/send_redirects': '0',
            'net/ipv4/conf/all/rp_filter': '0', 'net/ipv4/conf/default/rp_filter': '0',
            f'net/ipv4/conf/{interface}/rp_filter': '0', 'net/ipv4/conf/all/src_valid_mark': '0',
            'net/ipv4/conf/default/src_valid_mark': '0', f'net/ipv4/conf/{interface}/src_valid_mark': '0'}
    if tailscale_exit:
        # Enable routing before the nftables IPv6 rejection, preserving upstream RA.
        values.update({'net/ipv6/conf/all/forwarding': '1', f'net/ipv6/conf/{interface}/accept_ra': '2'})
    return values


def sysctl_text(interface, tailscale_exit=False):
    return ''.join(key + '=' + value + '\n' for key, value in sysctls(interface, tailscale_exit).items())


def docker_settings(existing):
    value = dict(existing)
    for key, expected in DOCKER_NETWORK.items():
        if key in value and value[key] != expected:
            raise ValueError('Existing Docker setting conflicts: ' + key)
        value[key] = expected
    value.setdefault('log-driver', 'local')
    if value['log-driver'] == 'local':
        value.setdefault('log-opts', {'max-size': '10m', 'max-file': '3'})
    return value


def managed_tailscale_table(family, name):
    """Accept only project-installed Tailscale tables, rejecting foreign hooks."""
    if family not in ('ip', 'ip6') or name not in ('filter', 'nat', 'mangle'):
        return False
    try:
        receipt = json.loads(read(TAILSCALE_RECEIPT, '{}'))
        if receipt.get('schema') != 1 or receipt.get('firewall') != 'nftables' or not shutil.which('tailscale'):
            return False
        environment = command(['systemctl', 'show', '-p', 'Environment', '--value', 'tailscaled'])
        if environment.returncode or 'TS_DEBUG_FIREWALL_MODE=nftables' not in environment.stdout.split():
            return False
        table = command(['nft', '-j', 'list', 'table', family, name])
        if table.returncode:
            return False
        hooks = {'INPUT': ('input', 0, 'ts-input'), 'FORWARD': ('forward', 0, 'ts-forward')} if name == 'filter' else (
            {'POSTROUTING': ('postrouting', 100, 'ts-postrouting')} if name == 'nat' else {
                'PREROUTING': ('prerouting', -150, None), 'OUTPUT': ('output', -150, None)})
        for item in json.loads(table.stdout).get('nftables', []):
            if set(item) & {'metainfo', 'table'}:
                continue
            chain = item.get('chain')
            if chain is not None:
                cname = chain.get('name')
                if cname in hooks:
                    hook, priority, _ = hooks[cname]
                    if (chain.get('hook'), chain.get('prio'), chain.get('policy')) != (hook, priority, 'accept'):
                        return False
                elif cname not in {v[2] for v in hooks.values()} or 'hook' in chain:
                    return False
            elif 'rule' in item:
                rule = item['rule']
                cname = rule.get('chain')
                if cname in hooks:
                    expressions = [expression for expression in rule.get('expr', []) if 'counter' not in expression]
                    expected = tailscale_connmark_rule(cname) if name == 'mangle' else [
                        {'jump': {'target': hooks[cname][2]}}]
                    if expressions != expected:
                        return False
                elif cname not in {v[2] for v in hooks.values()}:
                    return False
            else:
                return False
        return True
    except (ValueError, TypeError, KeyError):
        return False


def tailscale_connmark_rule(chain):
    """Pinned Tailscale connmark rules, not permission for arbitrary mangle rules."""
    source, target = ('ct', 'meta') if chain == 'PREROUTING' else ('meta', 'ct')
    return [{'match': {'op': 'in', 'left': {'ct': {'key': 'state'}},
                       'right': ['established', 'related'] if chain == 'PREROUTING' else 'new'}},
            {'match': {'op': '!=', 'left': {'&': [{source: {'key': 'mark'}}, 0xff0000]}, 'right': 0}},
            {'mangle': {'key': {target: {'key': 'mark'}}, 'value': {'&': [{source: {'key': 'mark'}}, 0xff0000]}}}]


def inspect(value):
    interface, address, gateway, controller = settings(value)
    blockers, changes = [], []
    release = dict(line.split('=', 1) for line in read('/etc/os-release').splitlines() if '=' in line)
    if (release.get('ID', '').strip('"'), release.get('VERSION_ID', '').strip('"')) != ('ubuntu', '24.04'):
        blockers.append('Supported target: Ubuntu 24.04 amd64')
    if command(['uname', '-m']).stdout.strip() != 'x86_64':
        blockers.append('The pinned image and Mihomo binary require amd64')
    if 'microsoft' in read('/proc/sys/kernel/osrelease').lower():
        blockers.append('WSL can manage a target, but is not a supported gateway target')
    if read('/proc/1/comm').strip() != 'systemd' or pathlib.Path('/.dockerenv').exists():
        blockers.append('The target must run its own systemd and Linux network namespace')
    if not shutil.which('ip') or not shutil.which('ss'):
        blockers.append('Install iproute2 before preparing the target')
    else:
        link = command(['ip', '-j', '-4', 'address', 'show', 'dev', interface])
        addresses = json.loads(link.stdout) if link.returncode == 0 else []
        matching = [a for entry in addresses for a in entry.get('addr_info', [])
                    if a.get('local') == str(address.ip) and a.get('prefixlen') == address.network.prefixlen]
        if not matching:
            blockers.append('Configure the expected static LAN address on ' + interface + ' before deployment')
        elif any(a.get('dynamic') or a.get('valid_life_time', 4294967295) not in ('forever', 4294967295) for a in matching):
            blockers.append('The gateway address must be static, outside the upstream DHCP pool')
        routes = json.loads(command(['ip', '-j', '-4', 'route', 'show', 'default']).stdout or '[]')
        if not any(r.get('dev') == interface and r.get('gateway') == str(gateway) for r in routes):
            blockers.append('Configure the expected upstream default route before deployment')
        installed = pathlib.Path('/opt/home-gateway/config/gateway.yaml').exists()
        for line in command(['ss', '-H', '-lntup']).stdout.splitlines():
            fields = line.split()
            if len(fields) < 6:
                continue
            host, _, port = fields[4].rpartition(':')
            host = host.split('%')[0].strip('[]')
            if port == '53' and host in (str(address.ip), '*', '0.0.0.0', '::'):
                if 'systemd-resolve' not in line:
                    blockers.append('Another DNS service owns the gateway DNS port')
            if (host, port) in ((controller[0], str(controller[1])), ('127.0.0.1', '7893'), ('127.0.0.1', '7897')) or (
                    host in ('*', '0.0.0.0', '::') and port in (str(controller[1]), '7893', '7897')):
                if not (installed and 'mihomo' in line):
                    blockers.append('Another process owns a gateway proxy or controller port')
    if re.search(r'^ENABLED\s*=\s*yes\s*$', read('/etc/ufw/ufw.conf'), re.MULTILINE):
        blockers.append('UFW is enabled; use a dedicated target without another firewall manager')
    for service in ('firewalld', 'nftables'):
        if command(['systemctl', 'is-active', '--quiet', service]).returncode == 0 or command(
                ['systemctl', 'is-enabled', '--quiet', service]).returncode == 0:
            blockers.append('Another firewall manager is active or enabled: ' + service)
    if shutil.which('nft'):
        result = command(['nft', '-j', 'list', 'tables'])
        if result.returncode:
            blockers.append('Unable to inspect the current firewall')
        elif any((item['table'].get('family'), item['table']['name']) not in (
                    ('inet', 'home_gateway_base'), ('inet', 'home_gateway_proxy')) and not managed_tailscale_table(
                        item['table'].get('family'), item['table']['name'])
                 for item in json.loads(result.stdout).get('nftables', []) if 'table' in item):
            blockers.append('Unmanaged firewall tables exist; automatic initialization is limited to a dedicated target')
    else:
        changes.append('Install nftables')
    try:
        current = json.loads(read(DAEMON, '{}'))
        desired = docker_settings(current)
        if current != desired:
            changes.append('Add missing Docker gateway settings, retaining other keys')
    except (ValueError, TypeError):
        blockers.append('Existing Docker configuration is invalid or has conflicting network settings')
    if shutil.which('docker'):
        containers = command(['docker', 'ps', '-a', '--format', '{{.Names}}'])
        if containers.returncode:
            blockers.append('The existing Docker daemon is unavailable')
        elif any(name != 'home-gateway' for name in containers.stdout.splitlines()):
            blockers.append('Other Docker containers exist; use a dedicated gateway target')
        if command(['docker', 'compose', 'version']).returncode:
            blockers.append('The existing Docker installation needs the Compose v2 plugin')
    else:
        changes.append('Install Docker Engine and Compose v2')
        for package in ('docker.io', 'podman-docker', 'containerd', 'runc'):
            if command(['dpkg-query', '-W', '-f=${db:Status-Status}', package]).stdout.strip() == 'installed':
                blockers.append('Remove conflicting package before initialization: ' + package)
    if command(['python3', '-c', 'import yaml']).returncode:
        changes.append('Install python3-yaml')
    if command(['systemctl', 'is-active', '--quiet', 'systemd-resolved']).returncode:
        blockers.append('Enable systemd-resolved before preparing the gateway')
    if not pathlib.Path('/dev/net/tun').exists():
        changes.append('Load the TUN kernel module')
    tailscale_exit = value.get('tailscale_exit', False)
    if not isinstance(tailscale_exit, bool):
        blockers.append('tailscale_exit must be a boolean')
    if tailscale_exit and not TAILSCALE_RECEIPT.exists():
        blockers.append('Install project-managed Tailscale before preparing an exit node')
    if read(SYSCTL) != sysctl_text(interface, tailscale_exit):
        changes.append('Write gateway forwarding settings for ' + interface)
    if any(read('/proc/sys/' + key).strip() != expected for key, expected in sysctls(interface, tailscale_exit).items()):
        changes.append('Apply gateway forwarding settings')
    return {'supported': not blockers, 'ready': not blockers and not changes,
            'blockers': sorted(set(blockers)), 'changes': changes}


def write_owned(path, body):
    """Retain the first pre-gateway file, without overwriting unrelated options."""
    path = pathlib.Path(path)
    BACKUP.mkdir(parents=True, exist_ok=True, mode=0o700)
    saved = BACKUP / path.name
    absent = BACKUP / (path.name + '.absent')
    if not saved.exists() and not absent.exists():
        if path.exists():
            shutil.copy2(path, saved)
            saved.chmod(0o600)
        else:
            absent.touch(mode=0o600)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o644)


def apply(value):
    report = inspect(value)
    if report['blockers'] or report['ready']:
        return report
    interface, _, _, _ = settings(value)
    existing = json.loads(read(DAEMON, '{}'))
    desired = docker_settings(existing)
    docker_present = bool(shutil.which('docker'))
    # Changing a running gateway's daemon would interrupt networking. Its settings
    # must be repaired explicitly rather than restarting it during preparation.
    if docker_present and existing != desired and command(
            ['docker', 'ps', '--format', '{{.Names}}']).stdout.strip():
        report['blockers'].append('Stop the gateway before changing Docker daemon settings')
        report['supported'] = report['ready'] = False
        return report
    dependencies = []
    install_nft = not shutil.which('nft')
    if command(['python3', '-c', 'import yaml']).returncode:
        dependencies.append('python3-yaml')
    if install_nft:
        dependencies.append('nftables')
    if not shutil.which('modprobe'):
        dependencies.append('kmod')
    if not docker_present:
        dependencies += ['ca-certificates', 'curl']
    if dependencies:
        run(APT + ['update'])
        run(APT + ['install', '-y', '--no-install-recommends'] + dependencies)
        if install_nft:
            # A newly installed package must not flush our tables at the next boot.
            run(['systemctl', 'disable', '--now', 'nftables'])
    if existing != desired:
        write_owned(DAEMON, json.dumps(desired, indent=2) + '\n')
    if not docker_present:
        run(['install', '-d', '-m', '0755', '/etc/apt/keyrings'])
        run(['curl', '--fail', '--silent', '--show-error', '--location', '--retry', '2',
             '--retry-max-time', '45', '--connect-timeout', '10', '--max-time', '60',
             'https://download.docker.com/linux/ubuntu/gpg', '-o', '/etc/apt/keyrings/docker.asc'], timeout=90)
        pathlib.Path('/etc/apt/keyrings/docker.asc').chmod(0o644)
        write_owned('/etc/apt/sources.list.d/docker.sources',
                    'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\n'
                    'Components: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n')
        run(APT + ['update'])
        run(APT + ['install', '-y', 'docker-ce', 'docker-ce-cli', 'containerd.io',
                   'docker-buildx-plugin', 'docker-compose-plugin'])
    if not docker_present or existing != desired:
        run(['systemctl', 'enable', '--now', 'docker'])
        if docker_present:
            run(['systemctl', 'restart', 'docker'])
    run(['modprobe', 'tun'])
    if read(SYSCTL) != sysctl_text(interface, value.get('tailscale_exit', False)):
        write_owned(SYSCTL, sysctl_text(interface, value.get('tailscale_exit', False)))
    # Apply only this project's keys; do not reapply all host sysctl files.
    run(['sysctl', '-p', str(SYSCTL)])
    return inspect(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run the target initializer through sudo')
    value = json.load(sys.stdin)
    report = apply(value) if args.apply else inspect(value)
    print(json.dumps(report))
    return 2 if report['blockers'] or (args.apply and not report['ready']) else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'supported': False, 'ready': False, 'blockers': [str(error)], 'changes': []}))
        raise SystemExit(2)
