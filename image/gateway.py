import json
import os
import pathlib
import signal
import time
import yaml
from network import ensure_proxy_routing, proxy_off, proxy_on, run
from configuration import render_mihomo
from runtime import CONFIG, DATA, load_config, read_policy, save_json, effective_policy
from topology import component_enabled
from vpn_auth import openconnect_command
from component import Component

def ready(port):
    for filename in ('/proc/net/tcp', '/proc/net/tcp6'):
        for line in pathlib.Path(filename).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A' and int(fields[1].split(':')[1], 16) == port:
                return True
    return False


LOGS = DATA / 'logs'


def rotate_logs():
    for path in LOGS.glob('*.log'):
        if path.stat().st_size > 5 * 1024 * 1024:
            with path.open('rb') as stream:
                stream.seek(-1024 * 1024, 2)
                tail = stream.read()
            path.with_suffix('.log.1').write_bytes(tail)
            with path.open('r+b') as stream:
                stream.truncate(0)


def main():
    DATA.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    config = load_config()
    stopping = False

    def stop_signal(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    policy = effective_policy(config, read_policy(config=config))
    policy['up'] = False
    save_json(DATA / 'vpn-policy.json', policy)
    proxy_off()
    components = []
    last_network_state = None
    last_routing_check = 0
    last_rotate = 0
    try:
        if component_enabled(config, 'clash'):
            mihomo = render_mihomo(config)
            runtime = DATA / 'mihomo'
            runtime.mkdir(exist_ok=True)
            runtime_config = runtime / 'generated.yaml'
            runtime_config.write_text(yaml.safe_dump(mihomo, allow_unicode=True, sort_keys=False))
            os.chmod(runtime_config, 0o600)
            run(['mihomo', '-t', '-d', str(runtime), '-f', str(runtime_config)])
            components.append(Component('clash', ['mihomo', '-d', str(runtime), '-f', str(runtime_config)], config['clash'].get('attempts', 3)))
        if component_enabled(config, 'vpn'):
            vpn = config['vpn']
            vpn_command, password_file = openconnect_command(vpn, CONFIG.parent)
            components.append(Component('vpn', vpn_command, vpn.get('attempts', 4), password_file,
                                        retry_delay=vpn.get('retry_delay_seconds', 15),
                                        retry_max_delay=vpn.get('retry_max_delay_seconds', 60),
                                        stable_reset_seconds=vpn.get('stable_reset_seconds', 600)))
        if component_enabled(config, 'clash') and component_enabled(config, 'usage'):
            components.append(Component('usage', ['python3', '/app/usage_collect.py'], 3))

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
                component.poll(healthy=component.name == 'vpn' and read_policy(config=config).get('up', False))
            policy = effective_policy(config, read_policy(config=config))
            clash = next((c for c in components if c.name == 'clash'), None)
            capture = bool(clash and clash.process and ready(7893))
            network_state = json.dumps([capture, policy], sort_keys=True)
            if network_state != last_network_state:
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
        if any(component.name == 'usage' and component.process for component in components):
            # Stop new transparent sessions, close tracked flows while the core is
            # alive, then let the collector take its final snapshot and commit.
            from usage_collect import Controller
            controller = Controller(config)
            try:
                controller.close_connections()
                time.sleep(0.05)
            except Exception:
                print('Connection summary drain unavailable during shutdown', flush=True)
            finally:
                controller.close()
        for component in components:
            if component.name == 'usage':
                component.stop()
        for component in components:
            if component.name != 'usage':
                component.stop()
        policy = effective_policy(config, read_policy(config=config))
        policy['up'] = False
        save_json(DATA / 'vpn-policy.json', policy)


if __name__ == '__main__':
    main()
