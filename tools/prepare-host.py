"""Inspect or explicitly initialize a dedicated Linux gateway target over SSH."""
import argparse
import json
import subprocess
import uuid
from common import ROOT, deployment, host_inputs, remote, ssh_command


def prepare(config, apply=False):
    path = '/tmp/home-gateway-host-' + uuid.uuid4().hex + '.py'
    remote(config, 'umask 077; cat > ' + path, (ROOT / 'hosts/linux/prepare.py').read_bytes())
    try:
        result = subprocess.run(ssh_command(config) + ['sudo python3 ' + path + (' --apply' if apply else '')],
                                input=json.dumps(host_inputs(config)).encode(), capture_output=True, timeout=600)
        try:
            report = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError('Host preparation failed before producing a report (exit ' + str(result.returncode) + ')') from None
        if not isinstance(report, dict) or 'ready' not in report or 'blockers' not in report:
            raise RuntimeError('Invalid host preparation report')
        if result.returncode and not report['blockers']:
            report['blockers'].append('Host preparation did not complete')
            report['supported'] = report['ready'] = False
        return report
    finally:
        remote(config, 'rm -f ' + path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--apply', action='store_true', help='Install dependencies and configure the dedicated gateway target')
    args = parser.parse_args()
    report = prepare(deployment(args.config), args.apply)
    print(json.dumps(report, indent=2))
    return 2 if report['blockers'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
