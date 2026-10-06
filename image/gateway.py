import json
import os
import pathlib
import signal
import time
import yaml
from network import DATA, apply_base, ensure_proxy_routing, proxy_off, proxy_on, read_policy, run, save_json
from configuration import render_mihomo
from runtime import CONFIG, load_config
from vpn_auth import openconnect_command
from component import Component

DATA.mkdir(exist_ok=True)
LOGS = DATA / 'logs'
LOGS.mkdir(exist_ok=True)
config = load_config()
stopping = False


def stop_signal(*_):
    global stopping
    stopping = True


signal.signal(signal.SIGTERM, stop_signal)
signal.signal(signal.SIGINT, stop_signal)


def ready(port):
    for filename in ('/proc/net/tcp', '/proc/net/tcp6'):
        for line in pathlib.Path(filename).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A' and int(fields[1].split(':')[1], 16) == port:
                return True
    return False


def rotate_logs():
    for path in LOGS.glob('*.log'):
        if path.stat().st_size > 5 * 1024 * 1024:
            with path.open('rb') as stream:
                stream.seek(-1024 * 1024, 2)
                tail = stream.read()
            path.with_suffix('.log.1').write_bytes(tail)
            with path.open('r+b') as stream:
                stream.truncate(0)


policy = read_policy()
policy['up'] = False
save_json(DATA / 'vpn-policy.json', policy)
proxy_off()
apply_base(config, policy)
components = []
if config['clash'].get('enabled', True):
    mihomo = render_mihomo(config)
    runtime = DATA / 'mihomo'
    runtime.mkdir(exist_ok=True)
    runtime_config = runtime / 'generated.yaml'
    runtime_config.write_text(yaml.safe_dump(mihomo, allow_unicode=True, sort_keys=False))
    os.chmod(runtime_config, 0o600)
    run(['mihomo', '-t', '-d', str(runtime), '-f', str(runtime_config)])
    components.append(Component('clash', ['mihomo', '-d', str(runtime), '-f', str(runtime_config)], config['clash'].get('attempts', 3)))
if config['vpn'].get('enabled', True):
    vpn = config['vpn']
    vpn_command, password_file = openconnect_command(vpn, CONFIG.parent)
    components.append(Component('vpn', vpn_command, vpn.get('attempts', 4), password_file,
                                retry_delay=vpn.get('retry_delay_seconds', 15),
                                retry_max_delay=vpn.get('retry_max_delay_seconds', 60),
                                stable_reset_seconds=vpn.get('stable_reset_seconds', 600)))

last_network_state = None
last_routing_check = 0
last_rotate = 0
try:
    while not stopping:
        for component in components:
            stop_file = DATA / ('stop-' + component.name)
            if stop_file.exists():
                stop_file.unlink()
                component.disable()
            retry_file = DATA / ('retry-' + component.name)
            if retry_file.exists():
                retry_file.unlink()
                component.retry()
            component.poll(healthy=component.name == 'vpn' and read_policy().get('up', False))
        policy = read_policy()
        clash = next((c for c in components if c.name == 'clash'), None)
        capture = bool(clash and clash.process and ready(7893))
        network_state = json.dumps([capture, policy], sort_keys=True)
        if network_state != last_network_state:
            apply_base(config, policy)
            if capture:
                proxy_on(config, policy)
            else:
                proxy_off()
            last_network_state = network_state
            last_routing_check = time.monotonic()
            print(f'Network updated: proxy={capture}, VPN={policy.get("up", False)}', flush=True)
        elif capture and time.monotonic() - last_routing_check >= 5:
            if ensure_proxy_routing():
                print('Transparent proxy routing restored after external changes', flush=True)
            last_routing_check = time.monotonic()
        save_json(DATA / 'status.json', {'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                       'transparent_proxy': capture, 'vpn_connected': policy.get('up', False),
                                       'vpn_prefix_count': len(policy.get('prefixes', [])),
                                       'vpn_dns_count': len(policy.get('dns', [])),
                                       'components': {c.name: c.status() for c in components}})
        if time.monotonic() - last_rotate > 30:
            rotate_logs()
            last_rotate = time.monotonic()
        time.sleep(1)
finally:
    proxy_off()
    for component in components:
        component.stop()
    policy = read_policy()
    policy['up'] = False
    save_json(DATA / 'vpn-policy.json', policy)
    apply_base(config, policy)
