import json
import os
import pathlib
import signal
import subprocess
import time
import yaml
from network import DATA, apply_base, proxy_off, proxy_on, read_policy, run, save_json
from configuration import render_mihomo
from runtime import CONFIG, load_config
from vpn_auth import openconnect_command

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


class Component:
    def __init__(self, name, command, max_attempts, password=None):
        self.name, self.command = name, command
        self.max_attempts, self.password = max_attempts, password
        self.process, self.log = None, None
        self.attempts, self.next_attempt, self.last_exit = 0, 0, None

    def poll(self):
        if self.process and self.process.poll() is not None:
            self.last_exit = self.process.returncode
            self.process = None
            self.log.close()
            self.next_attempt = time.monotonic() + 5
            print(f'{self.name}: exited with {self.last_exit}; attempt {self.attempts}/{self.max_attempts}', flush=True)
            if self.name == 'vpn':
                policy = read_policy()
                policy['up'] = False
                save_json(DATA / 'vpn-policy.json', policy)
        if not self.process and self.attempts < self.max_attempts and time.monotonic() >= self.next_attempt:
            self.log = (LOGS / (self.name + '.log')).open('ab', buffering=0)
            self.attempts += 1
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE if self.password else subprocess.DEVNULL,
                                            stdout=self.log, stderr=self.log, start_new_session=True)
            if self.password:
                self.process.stdin.write((self.password.read_text().rstrip('\r\n') + '\n').encode())
                self.process.stdin.close()
            print(f'{self.name}: started, attempt {self.attempts}/{self.max_attempts}', flush=True)

    def status(self):
        return {'state': 'running' if self.process else ('stopped' if self.attempts >= self.max_attempts else 'retry-wait'),
                'attempts': self.attempts, 'max_attempts': self.max_attempts, 'last_exit': self.last_exit}

    def stop(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        if self.log and not self.log.closed:
            self.log.close()
        self.process = None
        if self.name == 'vpn':
            policy = read_policy()
            policy['up'] = False
            save_json(DATA / 'vpn-policy.json', policy)


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
    components.append(Component('vpn', vpn_command, vpn.get('attempts', 1), password_file))

last_network_state = None
last_rotate = 0
try:
    while not stopping:
        for component in components:
            stop_file = DATA / ('stop-' + component.name)
            if stop_file.exists():
                stop_file.unlink()
                component.stop()
                component.attempts = component.max_attempts
            retry_file = DATA / ('retry-' + component.name)
            if retry_file.exists():
                retry_file.unlink()
                component.stop()
                component.attempts = 0
                component.next_attempt = 0
            component.poll()
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
            print(f'Network updated: proxy={capture}, VPN={policy.get("up", False)}', flush=True)
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
