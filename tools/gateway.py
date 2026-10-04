"""Shared gateway maintenance actions, independent of host OS and hypervisor."""
import argparse
import subprocess
from common import operator_deployment, ssh_command

ACTIONS = {
    'status': 'sudo python3 /opt/home-gateway/show-status.py',
    'logs-vpn': 'sudo tail -n 60 /opt/home-gateway/data/logs/vpn.log',
    'logs-clash': 'sudo tail -n 60 /opt/home-gateway/data/logs/clash.log',
    'logs-system': 'sudo journalctl -u home-gateway -u home-gateway-dns --no-pager -n 60',
    'logs-subscription': 'sudo journalctl -u home-gateway-subscription --no-pager -n 20',
    'logs-deployment': 'sudo cat /opt/home-gateway/data/deployment-error.log',
    'update-subscription': 'sudo systemctl start home-gateway-subscription; gateway_update_result=$?; sudo cat /opt/home-gateway/data/subscription-status.json; exit $gateway_update_result',
    'start': 'sudo systemctl reset-failed home-gateway home-gateway-dns && sudo systemctl start home-gateway-dns home-gateway',
    'stop': 'sudo systemctl stop home-gateway',
    'stop-vpn': 'sudo systemctl is-active --quiet home-gateway && sudo touch /opt/home-gateway/data/stop-vpn',
    'stop-clash': 'sudo systemctl is-active --quiet home-gateway && sudo touch /opt/home-gateway/data/stop-clash',
    'retry-vpn': 'sudo systemctl is-active --quiet home-gateway && sudo touch /opt/home-gateway/data/retry-vpn',
    'retry-clash': 'sudo systemctl is-active --quiet home-gateway && sudo touch /opt/home-gateway/data/retry-clash',
    'retry-dns': 'sudo systemctl reset-failed home-gateway-dns && sudo systemctl restart home-gateway-dns',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=(*ACTIONS, 'dashboard'), nargs='?', default='status')
    parser.add_argument('--config')
    args = parser.parse_args()
    config = operator_deployment(args.config)
    command = ssh_command(config)
    if args.action == 'dashboard':
        target = config['runtime']['mihomo']['external-controller']
        command = command[:-1] + ['-N', '-L', '127.0.0.1:19090:' + target,
                                  '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
                                  '-o', 'ServerAliveCountMax=2', command[-1]]
        print('Dashboard: http://127.0.0.1:19090/ui/ (Ctrl+C closes the SSH tunnel)', flush=True)
    else:
        command += [ACTIONS[args.action]]
    return subprocess.run(command).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
