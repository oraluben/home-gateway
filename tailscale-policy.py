"""Host adapter: publish pushed VPN destinations using the host's Tailscale CLI."""
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'image'))
os.environ['HOME_GATEWAY_DATA'] = str(ROOT / 'data')
os.environ.setdefault('HOME_GATEWAY_CONFIG', str(ROOT / 'config/gateway.yaml'))
from network import read_policy, save_json
from runtime import DATA, load_config
from tailscale_policy import desired_routes


def preferences():
    result = subprocess.run(['tailscale', 'debug', 'prefs'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError('Cannot read Tailscale preferences')
    return json.loads(result.stdout)


def main():
    if not load_config()['network'].get('tailscale_exit', False):
        return
    previous = None
    attempts = 0
    retry_at = 0
    while True:
        try:
            prefs = preferences()
            routes = desired_routes(read_policy(), prefs)
            key = tuple(routes)
            if key != previous:
                previous, attempts, retry_at = key, 0, 0
            current = [value for value in (prefs.get('AdvertiseRoutes') or []) if value not in ('0.0.0.0/0', '::/0')]
            if set(current) != set(routes) and attempts < 4 and time.monotonic() >= retry_at:
                attempts += 1
                retry_at = time.monotonic() + min(15 * 2 ** (attempts - 1), 60)
                result = subprocess.run(['tailscale', 'set', '--advertise-routes=' + ','.join(routes)],
                                        capture_output=True, timeout=15)
                if result.returncode:
                    raise RuntimeError('Tailscale route publication failed (exit ' + str(result.returncode) + ')')
                current = routes
                print(f'Tailscale destinations updated: {len(routes)} prefixes', flush=True)
            ready = set(current) == set(routes)
            save_json(DATA / 'tailscale-policy-status.json', {
                'state': 'ready' if ready else ('exhausted' if attempts == 4 else 'retrying'),
                'exit_advertised': '0.0.0.0/0' in (prefs.get('AdvertiseRoutes') or []),
                'prefix_count': len(routes), 'attempts': attempts, 'error': None if ready else 'Routes differ',
                'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(f'Tailscale policy sync failed: {type(error).__name__}; attempts {attempts}/4', flush=True)
            save_json(DATA / 'tailscale-policy-status.json', {
                'state': 'exhausted' if attempts == 4 else 'retrying', 'error': 'Tailscale policy sync failed',
                'attempts': attempts, 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
        time.sleep(3)


if __name__ == '__main__':
    main()
