"""Prepare and inspect the optional remote management service over trusted SSH."""
import argparse
import json
import subprocess
import uuid
from common import ROOT, operator_deployment, remote, ssh_command

MANAGEMENT_UP = ('sudo tailscale up --accept-dns=false --accept-routes=false --ssh=false '
                 '--advertise-exit-node=false --advertise-routes= --exit-node= '
                 '--hostname=home-gateway --timeout=20s --json')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'login', 'status', 'netcheck', 'logs'))
    parser.add_argument('--config')
    args = parser.parse_args()
    config = operator_deployment(args.config)
    if args.action == 'install':
        path = '/tmp/home-gateway-tailscale-' + uuid.uuid4().hex + '.py'
        remote(config, 'umask 077; cat > ' + path, (ROOT / 'hosts/linux/tailscale.py').read_bytes())
        try:
            result = subprocess.run(ssh_command(config) + ['sudo python3 ' + path],
                                    input=(ROOT / 'hosts/tailscale-versions.json').read_bytes(),
                                    capture_output=True, timeout=900)
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors='replace').strip() or 'Tailscale preparation failed')
            print(json.dumps(json.loads(result.stdout), indent=2))
        finally:
            remote(config, 'rm -f ' + path)
        # Refresh only the thin host entry point; do not redeploy the container.
        remote(config, 'sudo tee /usr/local/bin/gatewayctl >/dev/null && sudo chmod 755 /usr/local/bin/gatewayctl',
               (ROOT / 'gatewayctl.sh').read_bytes())
        return 0
    if args.action == 'login':
        result = subprocess.run(ssh_command(config) + [MANAGEMENT_UP], capture_output=True, text=True, timeout=45)
        # Up emits one or more JSON events. Keep the URL and state, omit embedded QR data.
        body = result.stdout.strip()
        decoder = json.JSONDecoder()
        login_pending = False
        while body:
            event, offset = decoder.raw_decode(body)
            login_pending |= bool(event.get('AuthURL'))
            print(json.dumps({key: event[key] for key in ('AuthURL', 'BackendState', 'Error') if key in event}, indent=2))
            body = body[offset:].strip()
        # The bounded up operation can time out while waiting for browser authorization.
        if login_pending:
            return 0
        if result.returncode:
            print(result.stderr, end='')
        return result.returncode
    command = {'status': 'sudo gatewayctl tailscale-status', 'netcheck': 'sudo gatewayctl tailscale-netcheck',
               'logs': 'sudo gatewayctl logs-tailscale'}[args.action]
    return subprocess.run(ssh_command(config) + [command], timeout=60).returncode


if __name__ == '__main__':
    raise SystemExit(main())
